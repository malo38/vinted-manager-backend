"""
Vinted Manager — Backend de synchronisation Vinted
====================================================
Reçoit les données envoyées par l'extension Chrome (ventes, annonces, messages)
et les sauvegarde dans Supabase pour les afficher automatiquement dans
Vinted Manager.

Aucune donnée sensible (mot de passe) n'est jamais stockée.
"""

import os
from datetime import date, datetime, timedelta
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")          # service_role (accès complet)
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")  # anon (valide les tokens utilisateurs)

# ── Suivi d'erreurs (Sentry) ──────────────────────────────────────────────
# Inactif tant que SENTRY_DSN n'est pas défini (créez un compte gratuit sur
# sentry.io, projet FastAPI/Python, et collez le DSN dans les variables
# d'environnement Railway). Sans ça, aucun changement de comportement.
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.1, send_default_pii=False)

def capture_error(exc: Exception):
    """Envoie l'erreur à Sentry si configuré (no-op sinon)."""
    if SENTRY_DSN:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)

app = FastAPI(title="Vinted Manager — Sync Backend", version="1.0.0")

# Autorise les appels depuis le site Vercel et l'extension Chrome
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # à restreindre à votre domaine Vercel en prod si besoin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=503, detail="Supabase non configuré côté serveur.")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ============================================================
# AUTH — valide le token Supabase envoyé par le site / extension
# ============================================================

def get_current_user_id(authorization: str = Header(None)) -> str:
    """Extrait et valide le JWT Supabase du header Authorization: Bearer <token>."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token d'authentification manquant.")

    token = authorization.removeprefix("Bearer ").strip()

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=503, detail="SUPABASE_ANON_KEY manquant côté serveur.")

    try:
        client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        response = client.auth.get_user(token)
        if response is None or response.user is None:
            raise HTTPException(status_code=401, detail="Token invalide ou expiré.")
        return response.user.id
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Authentification échouée : {exc}") from exc


# ============================================================
# ROUTE: Health check
# ============================================================

@app.get("/")
def root():
    return {"status": "ok", "app": "Vinted Manager Sync Backend"}


# ============================================================
# ROUTE: Synchronisation depuis l'extension Chrome
# ============================================================

class SyncPayload(BaseModel):
    vinted_user_id: str = ""
    vinted_login: str = ""
    reputation: dict = {}  # avis, note, abonnés, nb d'articles
    wallet: dict = {}      # solde porte-monnaie Vinted
    ventes: list = []      # articles vendus
    achats: list = []      # articles achetés (côté acheteur)
    annonces: list = []    # articles en vente (avec favoris/vues)
    messages: list = []    # conversations


def dedupe_by_key(rows: list, key: str) -> list:
    """Garde la dernière occurrence de chaque valeur de `key`.
    Nécessaire pour les upserts en lot : Postgres refuse qu'un même
    ON CONFLICT touche deux fois la même ligne dans un seul appel."""
    seen = {}
    for row in rows:
        seen[row[key]] = row
    return list(seen.values())


def resolve_vinted_account_id(sb: Client, user_id: str, vinted_user_id: str = "", vinted_account_id: str = "") -> str | None:
    """
    Résout la ligne `vinted_accounts` concernée par un appel (un utilisateur
    VintControl peut avoir plusieurs comptes Vinted connectés en parallèle).
    Priorité : `vinted_account_id` explicite (le site le connaît déjà via
    /api/extension/accounts) > `vinted_user_id` (l'extension le connaît via
    la session Vinted active) > repli sur le compte le plus récemment
    synchronisé de cet utilisateur (compatibilité avec un ancien
    site/extension qui n'envoie encore ni l'un ni l'autre).
    """
    if vinted_account_id:
        return vinted_account_id
    if vinted_user_id:
        res = sb.table("vinted_accounts").select("id").eq("user_id", user_id) \
            .eq("vinted_user_id", vinted_user_id).limit(1).execute()
        if res.data:
            return res.data[0]["id"]
    res = sb.table("vinted_accounts").select("id").eq("user_id", user_id) \
        .order("last_sync", desc=True).limit(1).execute()
    return res.data[0]["id"] if res.data else None


@app.post("/api/extension/sync")
def extension_sync(payload: SyncPayload, user_id: str = Depends(get_current_user_id)):
    """
    Reçoit les données Vinted (ventes, annonces, messages) envoyées par
    l'extension Chrome, et les transforme en articles Vinted Manager.
    """
    sb = get_supabase()
    today = date.today().isoformat()

    # ── Résout (ou crée) le compte Vinted concerné, en tout premier ──────────
    # Un utilisateur VintControl peut avoir plusieurs comptes Vinted connectés
    # en parallèle désormais : toutes les tables ci-dessous doivent être
    # rattachées explicitement à CE compte, plus au seul user_id.
    if not payload.vinted_login or not payload.vinted_user_id:
        raise HTTPException(status_code=400, detail="vinted_login/vinted_user_id manquant : impossible d'identifier le compte Vinted à synchroniser.")

    rep = payload.reputation or {}
    wallet = payload.wallet or {}
    try:
        account_res = sb.table("vinted_accounts").upsert({
            "user_id": user_id,
            "vinted_login": payload.vinted_login,
            "vinted_user_id": payload.vinted_user_id,
            "last_sync": today,
            "connected": True,
            "review_count": int(rep.get("review_count") or 0),
            "feedback_reputation": float(rep.get("feedback_reputation") or 0),
            "followers_count": int(rep.get("followers_count") or 0),
            "vinted_item_count": int(rep.get("item_count") or 0),
            "wallet_balance": float(wallet.get("balance") or 0),
            "wallet_pending_balance": float(wallet.get("pending_balance") or 0),
        }, on_conflict="user_id,vinted_user_id").execute()
        vinted_account_id = account_res.data[0]["id"]
    except Exception as e:
        print(f"[SYNC ERROR] vinted_accounts {user_id}: {e}")
        capture_error(e)
        raise HTTPException(status_code=502, detail="Échec de la résolution du compte Vinted, synchro annulée.")

    articles_upserted = 0
    purchases_upserted = 0
    messages_upserted = 0

    # ── Annonces actives → articles "stock" (upsert en un seul appel) ────────
    # /wardrobe/{userId}/items (voir background.js) peut encore lister un
    # article après sa vente (et /my_orders?order_type=sold ne renvoie que les
    # ~100 ventes les plus récentes, sans pagination) : sans ce garde-fou, une
    # synchro ultérieure qui ne voit plus la vente dans "ventes" mais voit
    # toujours l'article dans "annonces" repasserait son statut à "stock" —
    # ce qui faussait le stock affiché ET le chiffre d'affaires/bénéfice
    # (signalé par un utilisateur le 2026-07-11).
    annonce_ids = [str(a.get("id") or "") for a in payload.annonces if a.get("id")]
    vendu_ids = set()
    if annonce_ids:
        try:
            existing = sb.table("articles").select("vinted_item_id,status") \
                .eq("user_id", user_id).eq("vinted_account_id", vinted_account_id).in_("vinted_item_id", annonce_ids).execute()
            vendu_ids = {r["vinted_item_id"] for r in (existing.data or []) if r["status"] == "vendu"}
        except Exception as e:
            print(f"[SYNC ERROR] check statut vendu: {e}")
            capture_error(e)

    annonce_rows, stats_rows = [], []
    for a in payload.annonces:
        vinted_id = str(a.get("id") or "")
        if not vinted_id or vinted_id in vendu_ids:
            continue
        try:
            annonce_rows.append({
                "vinted_item_id": vinted_id,
                "user_id": user_id,
                "vinted_account_id": vinted_account_id,
                "name": str(a.get("titre") or "")[:255],
                "sell_price": float(a.get("prix") or 0),
                "platform": "Vinted",
                "status": "stock",
                "vinted_favoris": int(a.get("favoris") or 0),
                "vinted_vues": int(a.get("vues") or 0),
                "photo_url": str(a.get("photo") or "") or None,
                "source": "Vinted",
                "synced_at": today,
            })
            stats_rows.append({
                "user_id": user_id,
                "vinted_account_id": vinted_account_id,
                "vinted_item_id": vinted_id,
                "stat_date": today,
                "vues": int(a.get("vues") or 0),
                "favoris": int(a.get("favoris") or 0),
            })
        except Exception as e:
            print(f"[SYNC ERROR] annonce {vinted_id} (construction): {e}")
            capture_error(e)
    if annonce_rows:
        annonce_rows = dedupe_by_key(annonce_rows, "vinted_item_id")
        try:
            sb.table("articles").upsert(annonce_rows, on_conflict="vinted_account_id,vinted_item_id").execute()
            articles_upserted += len(annonce_rows)
        except Exception as e:
            print(f"[SYNC ERROR] annonces batch ({len(annonce_rows)} lignes): {e}")
            capture_error(e)
    if stats_rows:
        stats_rows = dedupe_by_key(stats_rows, "vinted_item_id")
        try:
            sb.table("vinted_stats_history").upsert(stats_rows, on_conflict="vinted_account_id,vinted_item_id,stat_date").execute()
        except Exception as e:
            print(f"[SYNC ERROR] stats_history batch ({len(stats_rows)} lignes): {e}")
            capture_error(e)

    # ── Ventes → articles "vendu" ─────────────────────────────────────────────
    vente_rows = []
    for v in payload.ventes:
        vinted_id = str(v.get("id") or "")
        if not vinted_id:
            continue
        try:
            vente_rows.append({
                "vinted_item_id": vinted_id,
                "user_id": user_id,
                "vinted_account_id": vinted_account_id,
                "name": str(v.get("titre") or "")[:255],
                "sell_price": float(v.get("prix") or 0),
                "platform": "Vinted",
                "status": "vendu",
                "sell_date": str(v.get("date_vente") or "")[:10] or None,
                "photo_url": str(v.get("photo") or "") or None,
                "source": "Vinted",
                "synced_at": today,
                "vinted_shipping_status": str(v.get("statut") or "")[:255],
                "vinted_transaction_status": str(v.get("statut_code") or "")[:50],
            })
        except Exception as e:
            print(f"[SYNC ERROR] vente {vinted_id} (construction): {e}")
            capture_error(e)
    if vente_rows:
        vente_rows = dedupe_by_key(vente_rows, "vinted_item_id")
        try:
            sb.table("articles").upsert(vente_rows, on_conflict="vinted_account_id,vinted_item_id").execute()
            articles_upserted += len(vente_rows)
        except Exception as e:
            print(f"[SYNC ERROR] ventes batch ({len(vente_rows)} lignes): {e}")
            capture_error(e)

    # ── Achats → table vinted_purchases (dépenses) ────────────────────────────
    achat_rows = []
    for p in payload.achats:
        vinted_id = str(p.get("id") or "")
        if not vinted_id:
            continue
        try:
            achat_rows.append({
                "id": vinted_id,
                "user_id": user_id,
                "vinted_account_id": vinted_account_id,
                "title": str(p.get("titre") or "")[:255],
                "price": float(p.get("prix") or 0),
                "purchase_date": str(p.get("date_achat") or "")[:10] or None,
                "photo_url": str(p.get("photo") or "") or None,
                "synced_at": today,
                "status": str(p.get("statut") or "")[:255],
                "transaction_status": str(p.get("statut_code") or "")[:50],
            })
        except Exception as e:
            print(f"[SYNC ERROR] achat {vinted_id} (construction): {e}")
            capture_error(e)
    if achat_rows:
        achat_rows = dedupe_by_key(achat_rows, "id")
        try:
            # On repère les achats qui n'existaient pas encore avant cette synchro
            # (donc jamais vus) pour créer automatiquement leur article de stock —
            # sans ça, chaque resynchro re-upserterait les mêmes lignes et on ne
            # pourrait plus distinguer un achat historique d'un nouveau.
            existing = sb.table("vinted_purchases").select("id").eq("user_id", user_id) \
                .eq("vinted_account_id", vinted_account_id).in_("id", [r["id"] for r in achat_rows]).execute()
            existing_ids = {row["id"] for row in (existing.data or [])}
            new_rows = [r for r in achat_rows if r["id"] not in existing_ids]

            sb.table("vinted_purchases").upsert(achat_rows, on_conflict="id").execute()
            purchases_upserted += len(achat_rows)

            # Conversion auto en stock (étape "à laver") : uniquement les tout
            # nouveaux achats, pour ne pas transformer rétroactivement tout
            # l'historique en stock au moment de la mise en prod de cette
            # fonctionnalité (voir supabase_purchase_stock_link.sql). Un achat déjà
            # remboursé/annulé ("failed") n'est jamais arrivé : pas d'article créé.
            if new_rows:
                article_rows = [{
                    "vinted_item_id": r["id"],
                    "user_id": user_id,
                    "vinted_account_id": vinted_account_id,
                    "name": r["title"][:255],
                    "buy_price": r["price"],
                    "buy_date": r["purchase_date"],
                    "platform": "Vinted",
                    "status": "laver",
                    "photo_url": r["photo_url"],
                    "source": "Vinted",
                    "synced_at": today,
                } for r in new_rows if r["transaction_status"] != "failed"]
                if article_rows:
                    sb.table("articles").upsert(article_rows, on_conflict="vinted_account_id,vinted_item_id").execute()
                # On marque tous les nouveaux achats comme traités (même les remboursés),
                # pour ne plus jamais les revérifier lors des prochaines synchros.
                sb.table("vinted_purchases").update({"stock_created": True}).eq("user_id", user_id) \
                    .in_("id", [r["id"] for r in new_rows]).execute()
        except Exception as e:
            print(f"[SYNC ERROR] achats batch ({len(achat_rows)} lignes): {e}")
            capture_error(e)

    # ── Messages → table conversations ───────────────────────────────────────
    message_rows = []
    for m in payload.messages:
        conv_id = str(m.get("id") or "")
        if not conv_id:
            continue
        try:
            message_rows.append({
                "id": conv_id,
                "user_id": user_id,
                "vinted_account_id": vinted_account_id,
                "interlocuteur": str(m.get("interlocuteur") or "")[:100],
                "dernier_message": str(m.get("dernier_message") or "")[:500],
                "non_lu": bool(m.get("non_lu") or False),
                "updated_at": str(m.get("updated_at") or "")[:30] or None,
                "est_offre": bool(m.get("est_offre") or False),
                "offre_prix": float(m["offre_prix"]) if m.get("offre_prix") is not None else None,
                "article_titre": str(m.get("article_titre") or "")[:255],
            })
        except Exception as e:
            print(f"[SYNC ERROR] message {conv_id} (construction): {e}")
            capture_error(e)
    if message_rows:
        message_rows = dedupe_by_key(message_rows, "id")
        try:
            sb.table("vinted_conversations").upsert(message_rows, on_conflict="id").execute()
            messages_upserted += len(message_rows)
        except Exception as e:
            print(f"[SYNC ERROR] messages batch ({len(message_rows)} lignes): {e}")
            capture_error(e)

    return {
        "ok": True,
        "articles_upserted": articles_upserted,
        "purchases_upserted": purchases_upserted,
        "messages_upserted": messages_upserted,
    }


# ============================================================
# ROUTE: Statut de connexion Vinted
# ============================================================

@app.get("/api/extension/status")
def extension_status(user_id: str = Depends(get_current_user_id)):
    """Conservé pour compatibilité avec un ancien popup d'extension : renvoie
    le compte le plus récemment synchronisé. Pour la vraie liste multicompte,
    voir GET /api/extension/accounts."""
    sb = get_supabase()
    res = sb.table("vinted_accounts").select("*").eq("user_id", user_id) \
        .order("last_sync", desc=True).limit(1).execute()
    if not res.data:
        return {"connected": False}
    account = res.data[0]
    return {
        "connected": account.get("connected", False),
        "vinted_login": account.get("vinted_login", ""),
        "last_sync": account.get("last_sync", ""),
    }


# ============================================================
# ROUTE: Liste des comptes Vinted connectés (multicompte)
# ============================================================

@app.get("/api/extension/accounts")
def list_vinted_accounts(user_id: str = Depends(get_current_user_id)):
    sb = get_supabase()
    res = sb.table("vinted_accounts").select("*").eq("user_id", user_id) \
        .order("last_sync", desc=True).execute()
    return {"accounts": res.data or []}


# ============================================================
# ROUTE: Déconnexion Vinted
# ============================================================

class DisconnectPayload(BaseModel):
    vinted_account_id: str = ""


@app.post("/api/extension/disconnect")
def extension_disconnect(payload: DisconnectPayload = DisconnectPayload(), user_id: str = Depends(get_current_user_id)):
    """
    Déconnecte un compte Vinted (soft — ne supprime aucune donnée historique,
    juste connected=False, comme avant). Si `vinted_account_id` n'est pas
    fourni (ancien extension/site pas encore à jour), déconnecte le compte le
    plus récemment synchronisé — préserve exactement le comportement d'avant
    pour un utilisateur avec un seul compte connecté.
    """
    sb = get_supabase()
    account_id = payload.vinted_account_id or resolve_vinted_account_id(sb, user_id)
    if not account_id:
        return {"ok": True}
    sb.table("vinted_accounts").update({"connected": False}) \
        .eq("id", account_id).eq("user_id", user_id).execute()
    return {"ok": True}


@app.post("/api/extension/accounts/{account_id}/disconnect")
def disconnect_account(account_id: str, user_id: str = Depends(get_current_user_id)):
    """Déconnecte UN compte Vinted précis par son id interne, sans affecter
    les autres comptes connectés de cet utilisateur."""
    sb = get_supabase()
    res = sb.table("vinted_accounts").update({"connected": False}) \
        .eq("id", account_id).eq("user_id", user_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Compte introuvable.")
    return {"ok": True}


# ============================================================
# ROUTE DEBUG: Stocke les notifications brutes envoyées par l'extension
# (temporaire, pour explorer le format des favoris Vinted)
# ============================================================

_LAST_NOTIFICATIONS_DEBUG: dict = {}

@app.post("/api/extension/debug-notifications")
def debug_notifications(payload: dict, user_id: str = Depends(get_current_user_id)):
    """Reçoit et stocke temporairement les notifications brutes pour inspection."""
    global _LAST_NOTIFICATIONS_DEBUG
    _LAST_NOTIFICATIONS_DEBUG[user_id] = payload
    print(f"[DEBUG NOTIFICATIONS] user={user_id} payload={payload}")
    return {"ok": True}


@app.get("/api/extension/debug-notifications")
def get_debug_notifications(user_id: str = Depends(get_current_user_id)):
    """Retourne les dernières notifications brutes reçues pour inspection."""
    return _LAST_NOTIFICATIONS_DEBUG.get(user_id, {"message": "Aucune donnée reçue pour le moment."})


@app.get("/api/extension/automessage-config")
def automessage_config(vinted_user_id: str = "", vinted_account_id: str = "", user_id: str = Depends(get_current_user_id)):
    """
    Réglages de l'envoi automatique de messages aux favoris, lus par l'extension
    avant chaque cycle, ainsi que le nombre déjà envoyé aujourd'hui (pour le plafond).
    Réglages désormais PAR COMPTE Vinted : `vinted_user_id` (connu de
    l'extension via la session live) ou `vinted_account_id` (connu du site)
    identifie le compte concerné.
    """
    sb = get_supabase()
    today = date.today().isoformat()
    account_id = resolve_vinted_account_id(sb, user_id, vinted_user_id, vinted_account_id)

    settings_query = sb.table("vinted_automessage_settings").select("*")
    settings_query = settings_query.eq("vinted_account_id", account_id) if account_id else settings_query.eq("user_id", user_id)
    res = settings_query.limit(1).execute()
    settings = res.data[0] if res.data else {
        "enabled": False,
        "template": "",
        "delay_min_sec": 60,
        "delay_max_sec": 180,
        "daily_limit": 20,
    }

    sent_query = (
        sb.table("vinted_sent_messages")
        .select("id", count="exact")
        .gte("sent_at", f"{today}T00:00:00")
    )
    sent_query = sent_query.eq("vinted_account_id", account_id) if account_id else sent_query.eq("user_id", user_id)
    sent_res = sent_query.execute()
    sent_today = sent_res.count or 0

    return {
        "enabled": bool(settings.get("enabled")),
        "template": settings.get("template") or "",
        "delay_min_sec": int(settings.get("delay_min_sec") or 60),
        "delay_max_sec": int(settings.get("delay_max_sec") or 180),
        "daily_limit": int(settings.get("daily_limit") or 20),
        "sent_today": sent_today,
    }


class AutomessageSettingsPayload(BaseModel):
    enabled: bool = False
    template: str = ""
    delay_min_sec: int = 60
    delay_max_sec: int = 180
    daily_limit: int = 20
    vinted_account_id: str = ""


@app.post("/api/settings/automessage")
def save_automessage_settings(payload: AutomessageSettingsPayload, user_id: str = Depends(get_current_user_id)):
    """Enregistre les réglages de l'envoi automatique depuis le site, pour le
    compte Vinted sélectionné (`vinted_account_id`, envoyé par le
    sélecteur de compte du site)."""
    sb = get_supabase()
    account_id = resolve_vinted_account_id(sb, user_id, vinted_account_id=payload.vinted_account_id)
    if not account_id:
        raise HTTPException(status_code=400, detail="Aucun compte Vinted connecté à configurer.")
    sb.table("vinted_automessage_settings").upsert({
        "user_id": user_id,
        "vinted_account_id": account_id,
        "enabled": payload.enabled,
        "template": payload.template[:1000],
        "delay_min_sec": max(10, payload.delay_min_sec),
        "delay_max_sec": max(payload.delay_min_sec, payload.delay_max_sec),
        "daily_limit": max(0, payload.daily_limit),
        "updated_at": datetime.utcnow().isoformat(),
    }, on_conflict="vinted_account_id").execute()
    return {"ok": True}


class MarkMessagedPayload(BaseModel):
    id: str
    recipient_login: str = ""
    recipient_id: str = ""
    item_id: str = ""
    item_title: str = ""
    message: str = ""
    vinted_user_id: str = ""


@app.post("/api/extension/mark-messaged")
def mark_messaged(payload: MarkMessagedPayload, user_id: str = Depends(get_current_user_id)):
    """
    Enregistre un message auto-envoyé par l'extension. Upsert par id de notification
    Vinted pour être idempotent (jamais deux fois le même favori, même après réinstallation).
    """
    sb = get_supabase()
    account_id = resolve_vinted_account_id(sb, user_id, vinted_user_id=payload.vinted_user_id)
    sb.table("vinted_sent_messages").upsert({
        "id": payload.id,
        "user_id": user_id,
        "vinted_account_id": account_id,
        "recipient_login": payload.recipient_login[:100],
        "recipient_id": payload.recipient_id,
        "item_id": payload.item_id,
        "item_title": payload.item_title[:255],
        "message": payload.message[:1000],
    }, on_conflict="id").execute()
    return {"ok": True}


@app.get("/api/extension/sent-messages")
def get_sent_messages(vinted_account_id: str = "", user_id: str = Depends(get_current_user_id)):
    """Historique des messages auto-envoyés, pour affichage sur le site.
    Filtré par compte si `vinted_account_id` est fourni (sélecteur de compte
    du site), sinon renvoie l'historique de tous les comptes de l'utilisateur."""
    sb = get_supabase()
    query = sb.table("vinted_sent_messages").select("*").eq("user_id", user_id)
    if vinted_account_id:
        query = query.eq("vinted_account_id", vinted_account_id)
    res = query.order("sent_at", desc=True).limit(50).execute()
    return {"messages": res.data or []}


# ============================================================
# ROUTE: Republication automatique des annonces
# ============================================================

@app.get("/api/extension/republish-config")
def republish_config(vinted_user_id: str = "", vinted_account_id: str = "", user_id: str = Depends(get_current_user_id)):
    """
    Réglages de la republication automatique, lus par l'extension avant
    chaque cycle, avec la liste des articles Vinted éligibles (pas republiés
    depuis au moins `frequency_days`) et le nombre déjà republié aujourd'hui.
    Réglages PAR COMPTE Vinted, comme automessage-config.
    """
    sb = get_supabase()
    today = date.today().isoformat()
    account_id = resolve_vinted_account_id(sb, user_id, vinted_user_id, vinted_account_id)

    settings_query = sb.table("vinted_republish_settings").select("*")
    settings_query = settings_query.eq("vinted_account_id", account_id) if account_id else settings_query.eq("user_id", user_id)
    res = settings_query.limit(1).execute()
    settings = res.data[0] if res.data else {
        "enabled": False,
        "frequency_days": 3,
        "daily_limit": 5,
    }
    enabled = bool(settings.get("enabled"))
    frequency_days = int(settings.get("frequency_days") or 3)
    daily_limit = int(settings.get("daily_limit") or 5)

    done_today_query = (
        sb.table("vinted_republish_log")
        .select("article_id", count="exact")
        .gte("last_republished_at", f"{today}T00:00:00")
    )
    done_today_query = done_today_query.eq("vinted_account_id", account_id) if account_id else done_today_query.eq("user_id", user_id)
    done_today_res = done_today_query.execute()
    republished_today = done_today_res.count or 0

    eligible_vinted_item_ids = []
    if enabled and republished_today < daily_limit:
        articles_query = (
            sb.table("articles")
            .select("id,vinted_item_id")
            .eq("user_id", user_id)
            .eq("status", "stock")
            .eq("platform", "Vinted")
            .not_.is_("vinted_item_id", "null")
        )
        articles_query = articles_query.eq("vinted_account_id", account_id) if account_id else articles_query
        articles_res = articles_query.execute()
        candidates = articles_res.data or []
        article_ids = [a["id"] for a in candidates]
        log_res = (
            sb.table("vinted_republish_log")
            .select("article_id,last_republished_at")
            .in_("article_id", article_ids)
            .execute()
            if article_ids else None
        )
        cutoff = datetime.utcnow() - timedelta(days=frequency_days)
        last_republished = {r["article_id"]: r["last_republished_at"] for r in (log_res.data if log_res else [])}
        for a in candidates:
            last = last_republished.get(a["id"])
            if last is None or datetime.fromisoformat(last.replace("Z", "+00:00")).replace(tzinfo=None) < cutoff:
                eligible_vinted_item_ids.append(a["vinted_item_id"])

    return {
        "enabled": enabled,
        "frequency_days": frequency_days,
        "daily_limit": daily_limit,
        "republished_today": republished_today,
        "eligible_vinted_item_ids": eligible_vinted_item_ids,
    }


class RepublishSettingsPayload(BaseModel):
    enabled: bool = False
    frequency_days: int = 3
    daily_limit: int = 5
    vinted_account_id: str = ""


@app.post("/api/settings/republish")
def save_republish_settings(payload: RepublishSettingsPayload, user_id: str = Depends(get_current_user_id)):
    """Enregistre les réglages de republication automatique depuis le site,
    pour le compte Vinted sélectionné."""
    sb = get_supabase()
    account_id = resolve_vinted_account_id(sb, user_id, vinted_account_id=payload.vinted_account_id)
    if not account_id:
        raise HTTPException(status_code=400, detail="Aucun compte Vinted connecté à configurer.")
    sb.table("vinted_republish_settings").upsert({
        "user_id": user_id,
        "vinted_account_id": account_id,
        "enabled": payload.enabled,
        "frequency_days": max(1, payload.frequency_days),
        "daily_limit": max(0, payload.daily_limit),
        "updated_at": datetime.utcnow().isoformat(),
    }, on_conflict="vinted_account_id").execute()
    return {"ok": True}


class MarkRepublishedPayload(BaseModel):
    old_vinted_item_id: str
    new_vinted_item_id: str
    vinted_user_id: str = ""


@app.post("/api/extension/mark-republished")
def mark_republished(payload: MarkRepublishedPayload, user_id: str = Depends(get_current_user_id)):
    """
    Après une republication réussie (delete + recreate), met à jour l'article
    interne pour pointer vers le nouvel id Vinted (l'ancien n'existe plus) et
    enregistre la date de republication pour respecter frequency_days ensuite.
    """
    sb = get_supabase()
    account_id = resolve_vinted_account_id(sb, user_id, vinted_user_id=payload.vinted_user_id)
    existing_query = (
        sb.table("articles")
        .select("id")
        .eq("user_id", user_id)
        .eq("vinted_item_id", payload.old_vinted_item_id)
    )
    existing_query = existing_query.eq("vinted_account_id", account_id) if account_id else existing_query
    existing = existing_query.limit(1).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Article introuvable pour cet ancien vinted_item_id.")
    article_id = existing.data[0]["id"]

    sb.table("articles").update({
        "vinted_item_id": payload.new_vinted_item_id,
        "synced_at": date.today().isoformat(),
    }).eq("id", article_id).execute()

    sb.table("vinted_republish_log").upsert({
        "article_id": article_id,
        "user_id": user_id,
        "vinted_account_id": account_id,
        "last_republished_at": datetime.utcnow().isoformat(),
    }, on_conflict="article_id").execute()

    return {"ok": True}


# ============================================================
# ROUTE: Prix du marché (recherche publique Vinted)
# ============================================================

import requests as req_lib

_VINTED_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


@app.get("/api/vinted/market-price")
def market_price(query: str, user_id: str = Depends(get_current_user_id)):
    """
    Recherche publique sur Vinted (comme n'importe quel visiteur non connecté)
    pour donner une idée du prix du marché sur un mot-clé donné.
    Confirmé le 2026-07-04 : GET /api/v2/catalog/items fonctionne avec une
    simple session anonyme (visite de la page d'accueil pour les cookies).
    """
    query = query.strip()
    if not query or len(query) < 2:
        raise HTTPException(status_code=400, detail="Recherche trop courte.")

    headers = {"User-Agent": _VINTED_UA, "Accept": "application/json"}
    session = req_lib.Session()
    try:
        session.get("https://www.vinted.fr/", headers=headers, timeout=10)
        r = session.get(
            "https://www.vinted.fr/api/v2/catalog/items",
            params={"search_text": query, "per_page": "50"},
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        capture_error(e)
        raise HTTPException(status_code=502, detail="Impossible de contacter Vinted pour le moment.")

    prices = []
    for item in data.get("items", []):
        try:
            prices.append(float(item["price"]["amount"]))
        except (KeyError, TypeError, ValueError):
            continue

    if not prices:
        return {"count": 0}

    prices.sort()
    n = len(prices)
    mid = n // 2
    median = prices[mid] if n % 2 else (prices[mid - 1] + prices[mid]) / 2

    return {
        "count": n,
        "average": round(sum(prices) / n, 2),
        "median": round(median, 2),
        "min": prices[0],
        "max": prices[-1],
    }


