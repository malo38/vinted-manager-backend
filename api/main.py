"""
Vinted Manager — Backend de synchronisation Vinted
====================================================
Reçoit les données envoyées par l'extension Chrome (ventes, annonces, messages)
et les sauvegarde dans Supabase pour les afficher automatiquement dans
Vinted Manager.

Aucune donnée sensible (mot de passe) n'est jamais stockée.
"""

import os
from datetime import date, datetime
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")          # service_role (accès complet)
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")  # anon (valide les tokens utilisateurs)

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
    ventes: list = []      # articles vendus
    achats: list = []      # articles achetés (côté acheteur)
    annonces: list = []    # articles en vente (avec favoris/vues)
    messages: list = []    # conversations


@app.post("/api/extension/sync")
def extension_sync(payload: SyncPayload, user_id: str = Depends(get_current_user_id)):
    """
    Reçoit les données Vinted (ventes, annonces, messages) envoyées par
    l'extension Chrome, et les transforme en articles Vinted Manager.
    """
    sb = get_supabase()
    today = date.today().isoformat()

    articles_upserted = 0

    # ── Annonces actives → articles "stock" ─────────────────────────────────
    for a in payload.annonces:
        vinted_id = str(a.get("id") or "")
        if not vinted_id:
            continue
        try:
            sb.table("articles").upsert({
                "vinted_item_id": vinted_id,
                "user_id": user_id,
                "name": str(a.get("titre") or "")[:255],
                "sell_price": float(a.get("prix") or 0),
                "platform": "Vinted",
                "status": "stock",
                "vinted_favoris": int(a.get("favoris") or 0),
                "vinted_vues": int(a.get("vues") or 0),
                "photo_url": str(a.get("photo") or "") or None,
                "source": "Vinted",
                "synced_at": today,
            }, on_conflict="vinted_item_id").execute()
            articles_upserted += 1
            sb.table("vinted_stats_history").upsert({
                "user_id": user_id,
                "vinted_item_id": vinted_id,
                "stat_date": today,
                "vues": int(a.get("vues") or 0),
                "favoris": int(a.get("favoris") or 0),
            }, on_conflict="vinted_item_id,stat_date").execute()
        except Exception as e:
            print(f"[SYNC ERROR] annonce {vinted_id}: {e}")

    # ── Ventes → articles "vendu" ────────────────────────────────────────────
    for v in payload.ventes:
        vinted_id = str(v.get("id") or "")
        if not vinted_id:
            continue
        try:
            sb.table("articles").upsert({
                "vinted_item_id": vinted_id,
                "user_id": user_id,
                "name": str(v.get("titre") or "")[:255],
                "sell_price": float(v.get("prix") or 0),
                "platform": "Vinted",
                "status": "vendu",
                "sell_date": str(v.get("date_vente") or "")[:10] or None,
                "photo_url": str(v.get("photo") or "") or None,
                "source": "Vinted",
                "synced_at": today,
            }, on_conflict="vinted_item_id").execute()
            articles_upserted += 1
        except Exception as e:
            print(f"[SYNC ERROR] vente {vinted_id}: {e}")

    # ── Achats → table vinted_purchases (dépenses) ────────────────────────────
    purchases_upserted = 0
    for p in payload.achats:
        vinted_id = str(p.get("id") or "")
        if not vinted_id:
            continue
        try:
            sb.table("vinted_purchases").upsert({
                "id": vinted_id,
                "user_id": user_id,
                "title": str(p.get("titre") or "")[:255],
                "price": float(p.get("prix") or 0),
                "purchase_date": str(p.get("date_achat") or "")[:10] or None,
                "photo_url": str(p.get("photo") or "") or None,
                "synced_at": today,
            }, on_conflict="id").execute()
            purchases_upserted += 1
        except Exception as e:
            print(f"[SYNC ERROR] achat {vinted_id}: {e}")

    # ── Messages → table conversations ───────────────────────────────────────
    messages_upserted = 0
    for m in payload.messages:
        conv_id = str(m.get("id") or "")
        if not conv_id:
            continue
        try:
            sb.table("vinted_conversations").upsert({
                "id": conv_id,
                "user_id": user_id,
                "interlocuteur": str(m.get("interlocuteur") or "")[:100],
                "dernier_message": str(m.get("dernier_message") or "")[:500],
                "non_lu": bool(m.get("non_lu") or False),
                "updated_at": str(m.get("updated_at") or "")[:30] or None,
            }, on_conflict="id").execute()
            messages_upserted += 1
        except Exception:
            pass

    # ── Mettre à jour le statut de connexion Vinted de l'utilisateur ─────────
    if payload.vinted_login:
        try:
            rep = payload.reputation or {}
            sb.table("vinted_accounts").upsert({
                "user_id": user_id,
                "vinted_login": payload.vinted_login,
                "vinted_user_id": payload.vinted_user_id,
                "last_sync": today,
                "connected": True,
                "review_count": int(rep.get("review_count") or 0),
                "feedback_reputation": float(rep.get("feedback_reputation") or 0),
                "followers_count": int(rep.get("followers_count") or 0),
                "vinted_item_count": int(rep.get("item_count") or 0),
            }, on_conflict="user_id").execute()
        except Exception:
            pass

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
    sb = get_supabase()
    res = sb.table("vinted_accounts").select("*").eq("user_id", user_id).limit(1).execute()
    if not res.data:
        return {"connected": False}
    account = res.data[0]
    return {
        "connected": account.get("connected", False),
        "vinted_login": account.get("vinted_login", ""),
        "last_sync": account.get("last_sync", ""),
    }


# ============================================================
# ROUTE: Déconnexion Vinted
# ============================================================

@app.post("/api/extension/disconnect")
def extension_disconnect(user_id: str = Depends(get_current_user_id)):
    sb = get_supabase()
    sb.table("vinted_accounts").update({"connected": False}).eq("user_id", user_id).execute()
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


# ============================================================
# ROUTE: Synchronisation via cookie Vinted (méthode manuelle)
# ============================================================

import requests as req_lib

VINTED_BASE = "https://www.vinted.fr/api/v2"

def vinted_get(cookie: str, path: str, params: dict = None):
    """Appelle l'API Vinted avec un cookie de session."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.vinted.fr/",
        "Origin": "https://www.vinted.fr",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Cookie": f"_vinted_fr_session={cookie}",
    }
    r = req_lib.get(
        f"{VINTED_BASE}{path}",
        headers=headers,
        params=params,
        timeout=15,
    )
    print(f"[VINTED] GET {path} → {r.status_code}")
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="Cookie Vinted expiré. Copiez un nouveau cookie depuis vinted.fr.")
    if r.status_code == 403:
        raise HTTPException(status_code=403, detail="Accès refusé par Vinted. Essayez de recopier votre cookie.")
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"Erreur Vinted API: {r.status_code}")
    return r.json()


class CookieSyncPayload(BaseModel):
    cookie: str


@app.get("/api/extension/automessage-config")
def automessage_config(user_id: str = Depends(get_current_user_id)):
    """
    Réglages de l'envoi automatique de messages aux favoris, lus par l'extension
    avant chaque cycle, ainsi que le nombre déjà envoyé aujourd'hui (pour le plafond).
    """
    sb = get_supabase()
    today = date.today().isoformat()

    res = sb.table("vinted_automessage_settings").select("*").eq("user_id", user_id).limit(1).execute()
    settings = res.data[0] if res.data else {
        "enabled": False,
        "template": "",
        "delay_min_sec": 60,
        "delay_max_sec": 180,
        "daily_limit": 20,
    }

    sent_res = (
        sb.table("vinted_sent_messages")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .gte("sent_at", f"{today}T00:00:00")
        .execute()
    )
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


@app.post("/api/settings/automessage")
def save_automessage_settings(payload: AutomessageSettingsPayload, user_id: str = Depends(get_current_user_id)):
    """Enregistre les réglages de l'envoi automatique depuis le site."""
    sb = get_supabase()
    sb.table("vinted_automessage_settings").upsert({
        "user_id": user_id,
        "enabled": payload.enabled,
        "template": payload.template[:1000],
        "delay_min_sec": max(10, payload.delay_min_sec),
        "delay_max_sec": max(payload.delay_min_sec, payload.delay_max_sec),
        "daily_limit": max(0, payload.daily_limit),
        "updated_at": datetime.utcnow().isoformat(),
    }, on_conflict="user_id").execute()
    return {"ok": True}


class MarkMessagedPayload(BaseModel):
    id: str
    recipient_login: str = ""
    recipient_id: str = ""
    item_id: str = ""
    item_title: str = ""
    message: str = ""


@app.post("/api/extension/mark-messaged")
def mark_messaged(payload: MarkMessagedPayload, user_id: str = Depends(get_current_user_id)):
    """
    Enregistre un message auto-envoyé par l'extension. Upsert par id de notification
    Vinted pour être idempotent (jamais deux fois le même favori, même après réinstallation).
    """
    sb = get_supabase()
    sb.table("vinted_sent_messages").upsert({
        "id": payload.id,
        "user_id": user_id,
        "recipient_login": payload.recipient_login[:100],
        "recipient_id": payload.recipient_id,
        "item_id": payload.item_id,
        "item_title": payload.item_title[:255],
        "message": payload.message[:1000],
    }, on_conflict="id").execute()
    return {"ok": True}


@app.get("/api/extension/sent-messages")
def get_sent_messages(user_id: str = Depends(get_current_user_id)):
    """Historique des messages auto-envoyés, pour affichage sur le site."""
    sb = get_supabase()
    res = (
        sb.table("vinted_sent_messages")
        .select("*")
        .eq("user_id", user_id)
        .order("sent_at", desc=True)
        .limit(50)
        .execute()
    )
    return {"messages": res.data or []}


@app.post("/api/vinted/sync-cookie")
def sync_via_cookie(payload: CookieSyncPayload, user_id: str = Depends(get_current_user_id)):
    """
    Synchronise les données Vinted via le cookie de session.
    Appelé depuis le site quand l'utilisateur colle son cookie manuellement.
    """
    cookie = payload.cookie.strip()
    if not cookie or len(cookie) < 50:
        raise HTTPException(status_code=400, detail="Cookie invalide ou trop court.")

    sb = get_supabase()
    today = date.today().isoformat()

    # ── 1. Récupérer l'utilisateur Vinted ────────────────────────────────────
    try:
        user_raw = vinted_get(cookie, "/users/current")
    except HTTPException as e:
        raise HTTPException(status_code=e.status_code, detail=f"Test connexion Vinted: {e.detail}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Impossible de contacter Vinted: {e}")

    user = user_raw.get("user", {})
    vinted_user_id = str(user.get("id", ""))
    vinted_login = user.get("login", "")

    if not vinted_user_id:
        raise HTTPException(status_code=401, detail="Cookie invalide — impossible de récupérer l'utilisateur.")

    # ── 2. Récupérer les annonces actives (wardrobe) ──────────────────────────
    articles_upserted = 0
    try:
        wardrobe_raw = vinted_get(cookie, f"/wardrobe/{vinted_user_id}/items", {
            "per_page": "100", "order": "newest_first"
        })
        for item in wardrobe_raw.get("items", []):
            vinted_id = str(item.get("id", ""))
            if not vinted_id:
                continue
            price = item.get("price", {})
            prix = float(price.get("amount", 0) if isinstance(price, dict) else price or 0)
            try:
                sb.table("articles").upsert({
                    "vinted_item_id": vinted_id,
                    "user_id": user_id,
                    "name": str(item.get("title", ""))[:255],
                    "sell_price": prix,
                    "platform": "Vinted",
                    "status": "stock",
                    "vinted_favoris": int(item.get("favourite_count") or 0),
                    "vinted_vues": int(item.get("view_count") or 0),
                    "source": "Vinted",
                    "synced_at": today,
                }, on_conflict="vinted_item_id").execute()
                articles_upserted += 1
            except Exception as e:
                print(f"[COOKIE SYNC] annonce {vinted_id}: {e}")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[COOKIE SYNC] Erreur wardrobe: {e}")

    # ── 3. Récupérer les ventes ───────────────────────────────────────────────
    try:
        orders_raw = vinted_get(cookie, "/my_orders", {"per_page": "100", "page": "1"})
        for order in orders_raw.get("my_orders", []):
            vinted_id = str(order.get("transaction_id") or order.get("id") or "")
            if not vinted_id:
                continue
            price = order.get("price", {})
            prix = float(price.get("amount", 0) if isinstance(price, dict) else price or 0)
            photo = order.get("photo") or {}
            photo_url = photo.get("url", "") if isinstance(photo, dict) else ""
            try:
                sb.table("articles").upsert({
                    "vinted_item_id": vinted_id,
                    "user_id": user_id,
                    "name": str(order.get("title", ""))[:255],
                    "sell_price": prix,
                    "platform": "Vinted",
                    "status": "vendu",
                    "sell_date": str(order.get("date", ""))[:10] or None,
                    "photo_url": photo_url or None,
                    "source": "Vinted",
                    "synced_at": today,
                }, on_conflict="vinted_item_id").execute()
                articles_upserted += 1
            except Exception as e:
                print(f"[COOKIE SYNC] vente {vinted_id}: {e}")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[COOKIE SYNC] Erreur orders: {e}")

    # ── 4. Sauvegarder le compte Vinted ──────────────────────────────────────
    try:
        sb.table("vinted_accounts").upsert({
            "user_id": user_id,
            "vinted_login": vinted_login,
            "vinted_user_id": vinted_user_id,
            "last_sync": today,
            "connected": True,
        }, on_conflict="user_id").execute()
    except Exception as e:
        print(f"[COOKIE SYNC] Erreur compte: {e}")

    return {
        "ok": True,
        "vinted_login": vinted_login,
        "articles_upserted": articles_upserted,
        "message": f"Synchronisation réussie — {articles_upserted} articles importés depuis @{vinted_login}",
    }
