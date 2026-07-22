"""
Vinted Manager — Backend de synchronisation Vinted
====================================================
Reçoit les données envoyées par l'extension Chrome (ventes, annonces, messages)
et les sauvegarde dans Supabase pour les afficher automatiquement dans
Vinted Manager.

Aucun mot de passe Vinted/VintControl n'est jamais stocké. Exception opt-in :
la fonctionnalité bêta "automatisation serveur" stocke un cookie de session
Vinted (avec consentement explicite), verrouillé par RLS sans policy — voir
vinted_session_credentials dans supabase_server_automation.sql.
"""

import os
import uuid
from typing import Optional
from datetime import date, datetime, timedelta
from fastapi import FastAPI, HTTPException, Header, Depends, Response
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
    # nb total d'annonces vues (toutes pages confondues, voir reconciliation
    # suppressions) — Optional plutôt que 0 par défaut : un dressing Vinted
    # devenu totalement vide après suppression de tout le stock envoie
    # légitimement 0, à ne pas confondre avec une ancienne extension qui
    # n'envoie pas du tout ce champ (absent => None => reconciliation ignorée
    # par sécurité, un vrai 0 => reconciliation appliquée normalement).
    wardrobe_total: Optional[int] = None
    messages: list = []    # conversations


def dedupe_by_key(rows: list, key: str) -> list:
    """Garde la dernière occurrence de chaque valeur de `key`.
    Nécessaire pour les upserts en lot : Postgres refuse qu'un même
    ON CONFLICT touche deux fois la même ligne dans un seul appel."""
    seen = {}
    for row in rows:
        seen[row[key]] = row
    return list(seen.values())


def resolve_sku(sb, user_id, vinted_account_id, context, vinted_id, name=None, create_defaults=None):
    """
    Traduit un id Vinted (annonce / commande de vente / commande d'achat) en
    SKU stable — l'identifiant qu'ON génère et qui ne change jamais, contrairement
    aux id Vinted qui sont DIFFÉRENTS selon qu'on regarde le même article physique
    comme annonce, comme vente ou comme achat (cause des doublons stock/vendu et
    des fiches "Lot N articles" fantômes, signalé le 2026-07-15).

    1. Cherche un lien déjà connu (vinted_links) pour cet id précis.
    2. Sinon, tente un rapprochement par nom exact parmi les articles pas
       encore vendus du même compte (typiquement : un achat déjà importé en
       stock, qu'on vient de retrouver publié sous un nouvel id d'annonce, ou
       une vente d'un article déjà en stock).
    3. Sinon, si `create_defaults` est fourni, crée un nouvel article + son
       sku + le lien. Sinon retourne None (appelant : mettre en file d'attente
       de réconciliation plutôt que fabriquer une fausse fiche de stock).
    """
    if not vinted_id:
        return None

    link_res = sb.table("vinted_links").select("sku").eq("context", context).eq("vinted_id", vinted_id).execute()
    if link_res.data:
        return link_res.data[0]["sku"]

    no_name_collision_at_all = True
    if name:
        norm_name = name.strip().lower()
        if norm_name:
            # Pour une annonce ou un achat, un article déjà "vendu" est exclu
            # des candidats (on ne veut pas rouvrir un item déjà écoulé comme
            # s'il était encore en stock). Pour une VENTE en revanche, on les
            # inclut aussi — mais en dernier recours seulement (voir tri
            # ci-dessous) : les exclure totalement empêchait de reconnaître
            # qu'une vente déjà traitée (mais sans lien enregistré, ex:
            # articles antérieurs à la migration SKU) était la même que celle
            # qu'on resynchronise, créant un doublon au lieu de la retrouver
            # (signalé le 2026-07-15). Mais les inclure sans les mettre en
            # dernier faisait l'inverse : avec plusieurs exemplaires identiques
            # (ex: 5x le même article en stock, achat-revente en gros), la 2e
            # vente d'un même nom se rattachait au premier exemplaire déjà
            # vendu au lieu d'un des 4 encore en stock — silencieusement
            # ignorée par le garde-fou "jamais régresser un vendu" plus loin
            # (signalé le 2026-07-17).
            query = sb.table("articles").select("sku,name,status") \
                .eq("user_id", user_id).eq("vinted_account_id", vinted_account_id)
            if context != "order_sale":
                query = query.neq("status", "vendu")
            candidates = query.execute()
            matches = [c for c in (candidates.data or []) if (c.get("name") or "").strip().lower() == norm_name]
            # Aucune fiche du même nom trouvée du tout, avant tout filtrage :
            # pas d'ambiguïté possible, une vente dans ce cas peut être créée
            # directement sans passer par la réconciliation manuelle (voir
            # plus bas, create_defaults pour order_sale). Un nom déjà présent
            # (même vendu) reste en revanche envoyé en réconciliation — cas où
            # une vraie confusion entre exemplaires identiques est possible.
            no_name_collision_at_all = not matches
            # Pour une ANNONCE : un sku déjà lié à une autre annonce active
            # (contexte "listing") est exclu des candidats — sinon 5 annonces
            # identiques (même nom, id différents, ex: achat-revente en gros)
            # fusionnaient toutes vers la même fiche dès la 1ère synchro, avant
            # même toute vente, laissant les 4 autres exemplaires physiques
            # invisibles pour toujours (signalé le 2026-07-17, juste après le
            # même bug détecté côté ventes). Un sku déjà lié à CET id précis
            # aurait déjà été trouvé par le lookup vinted_links plus haut, donc
            # ne peut pas être exclu à tort ici.
            if context == "listing" and matches:
                linked = sb.table("vinted_links").select("sku").eq("context", "listing") \
                    .in_("sku", [m["sku"] for m in matches]).execute()
                claimed_skus = {r["sku"] for r in (linked.data or [])}
                matches = [m for m in matches if m["sku"] not in claimed_skus]
            # Exemplaire encore en stock en priorité ; un déjà vendu seulement
            # si aucun autre candidat n'est disponible.
            matches.sort(key=lambda c: 1 if c.get("status") == "vendu" else 0)
            # Pour une VENTE : si le seul candidat restant est déjà vendu, ce
            # n'est pas une resynchro de cette même vente (aucun exemplaire
            # disponible à rattacher) — mieux vaut la mettre en file de
            # réconciliation manuelle que de la rattacher à un article déjà
            # écoulé et la voir ignorée en silence par le garde-fou "jamais
            # régresser un vendu" plus loin (signalé le 2026-07-17 : 4 ventes
            # sur 5 d'un même article en stock x5 disparaissaient ainsi).
            if context == "order_sale" and matches and matches[0].get("status") == "vendu":
                matches = []
            if matches:
                sku = matches[0]["sku"]
                sb.table("vinted_links").upsert(
                    {"sku": sku, "context": context, "vinted_id": vinted_id},
                    on_conflict="context,vinted_id",
                ).execute()
                return sku

    # Pour une VENTE, ne créer automatiquement que si le nom n'entrait en
    # collision avec RIEN du tout (aucune ambiguïté possible) — sinon un
    # exemplaire déjà vendu du même nom part en réconciliation manuelle
    # (voir plus haut) plutôt que de risquer de créer un doublon à tort.
    if create_defaults is not None and (context != "order_sale" or no_name_collision_at_all):
        new_sku = uuid.uuid4().hex[:8]
        sb.table("articles").insert({**create_defaults, "sku": new_sku}).execute()
        sb.table("vinted_links").insert({"sku": new_sku, "context": context, "vinted_id": vinted_id}).execute()
        return new_sku

    return None


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
    # force_resync (bouton "Réinitialiser depuis Vinted" sur le site) fait
    # exception au garde-fou ci-dessous, une seule fois : un article modifié
    # manuellement dans VintControl mais dont l'état réel diverge de Vinted
    # doit pouvoir être totalement réécrasé par la vraie donnée du moment.
    # Résolution par SKU (voir resolve_sku) plutôt que par vinted_item_id brut :
    # une annonce peut correspondre à un article déjà connu sous un tout autre
    # id Vinted (ex: acheté via VintControl puis republié — l'id d'annonce n'a
    # aucun rapport avec l'id de la commande d'achat d'origine).
    resync_ids = set()   # skus avec force_resync actif
    vendu_skus = set()   # skus déjà vendus, à ne jamais faire régresser

    annonce_links = {}
    for a in payload.annonces:
        vinted_id = str(a.get("id") or "")
        if not vinted_id:
            continue
        try:
            sku = resolve_sku(sb, user_id, vinted_account_id, "listing", vinted_id, name=str(a.get("titre") or ""))
        except Exception as e:
            print(f"[SYNC ERROR] resolve_sku annonce {vinted_id}: {e}")
            capture_error(e)
            continue
        if sku:
            annonce_links[vinted_id] = sku

    if annonce_links:
        try:
            skus = list(set(annonce_links.values()))
            existing = sb.table("articles").select("sku,status,force_resync").in_("sku", skus).execute()
            for r in (existing.data or []):
                if r["status"] == "vendu" and not r.get("force_resync"):
                    vendu_skus.add(r["sku"])
                if r.get("force_resync"):
                    resync_ids.add(r["sku"])
        except Exception as e:
            print(f"[SYNC ERROR] check statut vendu (annonces): {e}")
            capture_error(e)

    annonce_rows, stats_rows = [], []
    for a in payload.annonces:
        vinted_id = str(a.get("id") or "")
        if not vinted_id:
            continue
        sku = annonce_links.get(vinted_id)
        if sku and sku in vendu_skus:
            continue
        name = str(a.get("titre") or "")[:255]
        try:
            if not sku:
                # Jamais vu ni rapproché : nouvel article, créé directement par
                # resolve_sku avec ces valeurs par défaut — rien à upserter en plus.
                resolve_sku(sb, user_id, vinted_account_id, "listing", vinted_id, name=name, create_defaults={
                    "user_id": user_id,
                    "vinted_account_id": vinted_account_id,
                    "name": name,
                    "sell_price": float(a.get("prix") or 0),
                    "platform": "Vinted",
                    "status": "stock",
                    "vinted_item_id": vinted_id,
                    "vinted_favoris": int(a.get("favoris") or 0),
                    "vinted_vues": int(a.get("vues") or 0),
                    "vinted_boosted": bool(a.get("boost")),
                    "photo_url": str(a.get("photo") or "") or None,
                    "source": "Vinted",
                    "synced_at": today,
                })
                articles_upserted += 1
                continue
            annonce_rows.append({
                "sku": sku,
                "vinted_item_id": vinted_id,
                "user_id": user_id,
                "vinted_account_id": vinted_account_id,
                "name": name,
                "sell_price": float(a.get("prix") or 0),
                "platform": "Vinted",
                "status": "stock",
                "vinted_favoris": int(a.get("favoris") or 0),
                "vinted_vues": int(a.get("vues") or 0),
                "vinted_boosted": bool(a.get("boost")),
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
        annonce_rows = dedupe_by_key(annonce_rows, "sku")
        try:
            sb.table("articles").upsert(annonce_rows, on_conflict="sku").execute()
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

    # ── Annonces supprimées manuellement sur Vinted (bouton "Supprimer") ─────
    # /wardrobe/{userId}/items ne renvoie plus du tout ces annonces (contrairement
    # à une vente, où is_closed passe à true mais l'annonce reste listée) : sans
    # ce nettoyage, un article resterait indéfiniment "en stock" chez VintControl
    # après sa suppression sur Vinted (signalé le 2026-07-16). wardrobe_total=0
    # est un cas légitime (dressing devenu totalement vide) — seule son absence
    # (extension trop ancienne pour l'envoyer) doit désactiver ce nettoyage,
    # d'où le test "is not None" plutôt que "> 0" (bug trouvé le 2026-07-16 :
    # un compte n'ayant plus que les 2 annonces supprimées restait bloqué,
    # wardrobe_total valant alors 0 après leur suppression).
    if payload.wardrobe_total is not None:
        seen_ids = {str(a.get("id")) for a in payload.annonces if a.get("id")}
        try:
            live = sb.table("articles").select("sku,vinted_item_id").eq("vinted_account_id", vinted_account_id) \
                .eq("platform", "Vinted").eq("status", "stock").not_.is_("vinted_item_id", "null").execute()
            gone_skus = [r["sku"] for r in (live.data or []) if r["vinted_item_id"] not in seen_ids]
            if gone_skus:
                sb.table("articles").delete().in_("sku", gone_skus).execute()
        except Exception as e:
            print(f"[SYNC ERROR] reconciliation annonces supprimées: {e}")
            capture_error(e)

    # ── Ventes → articles "à expédier" puis "vendu" une fois expédié ─────────
    # Une vente fraîchement détectée doit atterrir en "à expédier" (l'acheteur
    # a payé mais le colis n'est pas encore parti), pas directement en
    # "vendu" — sinon l'étape d'expédition est silencieusement sautée. Ne
    # jamais faire régresser un article déjà marqué "vendu" (expédié) : même
    # garde-fou que pour le statut "stock" des annonces plus haut.
    #
    # Résolution par SKU (resolve_sku, contexte "order_sale") : on essaie
    # l'item_id résolu par l'extension (même id que l'annonce d'origine),
    # puis un rapprochement par nom. Si rien ne correspond, on NE FABRIQUE
    # PLUS de fausse fiche de stock (ancienne source des "Lot N articles"
    # fantômes) — la vente part en file d'attente `unmatched_sales` pour
    # réconciliation manuelle sur le site (signalé le 2026-07-15).
    def vente_key(v):
        return str(v.get("item_id") or v.get("id") or "")

    FAILED_TRANSACTION_STATUSES = {"failed", "cancelled", "canceled"}
    COMPLETED_TRANSACTION_STATUSES = {"completed"}

    vente_links = {}
    for v in payload.ventes:
        vinted_id = vente_key(v)
        if not vinted_id:
            continue
        statut_code = str(v.get("statut_code") or "").strip().lower()
        # Vente annulée/remboursée : elle n'a jamais réellement abouti, on ne
        # crée ni article ni entrée de réconciliation.
        if statut_code in FAILED_TRANSACTION_STATUSES:
            continue
        try:
            name = str(v.get("titre") or "")[:255]
            # create_defaults n'est utilisé par resolve_sku que si le nom ne
            # collisionne avec RIEN du tout (aucun article existant du même
            # nom, même vendu) — sinon la vente part quand même en
            # réconciliation manuelle, voir resolve_sku.
            sku = resolve_sku(sb, user_id, vinted_account_id, "order_sale", vinted_id, name=name, create_defaults={
                "user_id": user_id,
                "vinted_account_id": vinted_account_id,
                "name": name,
                "vinted_item_id": vinted_id,
                "sell_price": float(v.get("prix") or 0),
                "platform": "Vinted",
                "status": "vendu" if statut_code in COMPLETED_TRANSACTION_STATUSES else "expedition",
                "sell_date": str(v.get("date_vente") or "")[:10] or None,
                "photo_url": str(v.get("photo") or "") or None,
                "vinted_shipping_status": str(v.get("statut") or "")[:255],
                "vinted_transaction_status": str(v.get("statut_code") or "")[:50],
                "source": "Vinted",
                "synced_at": today,
            })
        except Exception as e:
            print(f"[SYNC ERROR] resolve_sku vente {vinted_id}: {e}")
            capture_error(e)
            continue
        if sku:
            vente_links[vinted_id] = sku

    vendu_skus_ventes = set()
    if vente_links:
        try:
            skus = list(set(vente_links.values()))
            existing_ventes = sb.table("articles").select("sku,status,force_resync").in_("sku", skus).execute()
            for r in (existing_ventes.data or []):
                if r["status"] == "vendu" and not r.get("force_resync"):
                    vendu_skus_ventes.add(r["sku"])
                if r.get("force_resync"):
                    resync_ids.add(r["sku"])
        except Exception as e:
            print(f"[SYNC ERROR] check statut déjà vendu (ventes): {e}")
            capture_error(e)

    vente_rows, unmatched_rows = [], []
    for v in payload.ventes:
        vinted_id = vente_key(v)
        if not vinted_id:
            continue
        statut_code = str(v.get("statut_code") or "").strip().lower()
        if statut_code in FAILED_TRANSACTION_STATUSES:
            continue
        sku = vente_links.get(vinted_id)
        name = str(v.get("titre") or "")[:255]
        try:
            if not sku:
                # Aucune correspondance fiable : file de réconciliation plutôt
                # qu'une fiche de stock fantôme.
                unmatched_rows.append({
                    "user_id": user_id,
                    "vinted_account_id": vinted_account_id,
                    "vinted_order_id": vinted_id,
                    "name": name,
                    "sell_price": float(v.get("prix") or 0),
                    "sell_date": str(v.get("date_vente") or "")[:10] or None,
                    "photo_url": str(v.get("photo") or "") or None,
                    "vinted_shipping_status": str(v.get("statut") or "")[:255],
                    "vinted_transaction_status": str(v.get("statut_code") or "")[:50],
                })
                continue
            if sku in vendu_skus_ventes:
                continue
            status = "vendu" if statut_code in COMPLETED_TRANSACTION_STATUSES else "expedition"
            vente_rows.append({
                "sku": sku,
                "vinted_item_id": vinted_id,
                "user_id": user_id,
                "vinted_account_id": vinted_account_id,
                "name": name,
                "sell_price": float(v.get("prix") or 0),
                "platform": "Vinted",
                "status": status,
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
        vente_rows = dedupe_by_key(vente_rows, "sku")
        try:
            sb.table("articles").upsert(vente_rows, on_conflict="sku").execute()
            articles_upserted += len(vente_rows)
        except Exception as e:
            print(f"[SYNC ERROR] ventes batch ({len(vente_rows)} lignes): {e}")
            capture_error(e)
    if unmatched_rows:
        unmatched_rows = dedupe_by_key(unmatched_rows, "vinted_order_id")
        try:
            sb.table("unmatched_sales").upsert(unmatched_rows, on_conflict="user_id,vinted_order_id").execute()
        except Exception as e:
            print(f"[SYNC ERROR] unmatched_sales batch ({len(unmatched_rows)} lignes): {e}")
            capture_error(e)

    # Le passe-droit force_resync ne vaut que pour ce cycle : une fois la
    # vraie donnée Vinted réappliquée ci-dessus, on remet le garde-fou normal
    # en place pour la prochaine synchro.
    if resync_ids:
        try:
            sb.table("articles").update({"force_resync": False}) \
                .eq("user_id", user_id).eq("vinted_account_id", vinted_account_id).in_("sku", list(resync_ids)).execute()
        except Exception as e:
            print(f"[SYNC ERROR] clear force_resync: {e}")
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
                "pickup_location": str(p.get("pickup_location") or "")[:500] or None,
                "pickup_since": str(p.get("pickup_since") or "")[:10] or None,
                "pickup_carrier": str(p.get("pickup_carrier") or "")[:50] or None,
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

            # Filet anti "phantom achat" : Vinted liste parfois une vraie vente
            # aussi dans les achats (même titre, prix et date) — surtout pour un
            # gros catalogue où l'extension n'a pas encore fini de résoudre le
            # current_user_side de chaque conversation (voir resolveOrderSides,
            # plafonné par cycle côté extension) et retombe sur une heuristique
            # non fiable. Avant, on se contentait de lier le sku sans upserter la
            # ligne dans vinted_purchases — mais l'upsert avait déjà lieu AVANT ce
            # test, donc la vente polluait quand même l'onglet Achats et les stats
            # "achats du mois" (signalé le 2026-07-22). Désormais : toute ligne
            # correspondant exactement à un article déjà "vendu" est exclue de
            # l'upsert, et supprimée si elle traînait déjà d'une synchro précédente.
            real_rows = []
            phantom_ids = []
            for r in achat_rows:
                if r["transaction_status"] == "failed":
                    real_rows.append(r)
                    continue
                try:
                    phantom = sb.table("articles").select("sku").eq("user_id", user_id) \
                        .eq("vinted_account_id", vinted_account_id).eq("status", "vendu") \
                        .eq("name", r["title"][:255]).eq("sell_date", r["purchase_date"]) \
                        .eq("sell_price", r["price"]).limit(1).execute()
                except Exception as e:
                    print(f"[SYNC ERROR] check phantom achat {r['id']}: {e}")
                    capture_error(e)
                    real_rows.append(r)
                    continue
                if phantom.data:
                    phantom_ids.append(r["id"])
                    try:
                        sb.table("vinted_links").upsert(
                            {"sku": phantom.data[0]["sku"], "context": "order_purchase", "vinted_id": r["id"]},
                            on_conflict="context,vinted_id",
                        ).execute()
                    except Exception as e:
                        print(f"[SYNC ERROR] link phantom achat {r['id']}: {e}")
                        capture_error(e)
                else:
                    real_rows.append(r)

            if phantom_ids:
                sb.table("vinted_purchases").delete().eq("user_id", user_id) \
                    .eq("vinted_account_id", vinted_account_id).in_("id", phantom_ids).execute()

            if real_rows:
                sb.table("vinted_purchases").upsert(real_rows, on_conflict="id").execute()
                purchases_upserted += len(real_rows)

            # Conversion auto en stock (étape "à laver") : uniquement les tout
            # nouveaux achats (réels, non-phantom), pour ne pas transformer
            # rétroactivement tout l'historique en stock au moment de la mise en
            # prod de cette fonctionnalité (voir supabase_purchase_stock_link.sql).
            # Un achat déjà remboursé/annulé ("failed") n'est jamais arrivé : pas
            # d'article créé.
            new_rows = [r for r in real_rows if r["id"] not in existing_ids]
            if new_rows:
                for r in new_rows:
                    if r["transaction_status"] == "failed":
                        continue
                    try:
                        resolve_sku(sb, user_id, vinted_account_id, "order_purchase", r["id"], name=r["title"], create_defaults={
                            "user_id": user_id,
                            "vinted_account_id": vinted_account_id,
                            "name": r["title"][:255],
                            "buy_price": r["price"],
                            "buy_date": r["purchase_date"],
                            "platform": "Vinted",
                            "status": "laver",
                            "vinted_item_id": r["id"],
                            "photo_url": r["photo_url"],
                            "source": "Vinted",
                            "synced_at": today,
                        })
                    except Exception as e:
                        print(f"[SYNC ERROR] resolve_sku achat {r['id']}: {e}")
                        capture_error(e)
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
        "batch_size": 1,
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
        "batch_size": max(1, int(settings.get("batch_size") or 1)),
        "sent_today": sent_today,
        "server_automation_enabled": bool(settings.get("server_automation_enabled")),
    }


class AutomessageSettingsPayload(BaseModel):
    enabled: bool = False
    template: str = ""
    delay_min_sec: int = 60
    delay_max_sec: int = 180
    daily_limit: int = 20
    batch_size: int = 1
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
        "batch_size": max(1, min(10, payload.batch_size)),
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


# ── AUTOMATISATION SERVEUR (bêta, opt-in) ───────────────────────────────
# Permet à un utilisateur d'accepter que le backend rejoue sa session Vinted
# pour continuer les messages aux favoris même ordinateur éteint. Le cookie
# de session est une donnée aussi sensible qu'un mot de passe : jamais logué,
# stocké uniquement dans vinted_session_credentials (RLS sans policy, voir
# supabase_server_automation.sql — seule la service_role key y accède).

class ServerAutomationCredentialsPayload(BaseModel):
    session_cookie: str
    anon_id: str = ""
    csrf_token: str = ""
    user_agent: str = ""
    vinted_user_id: str = ""


@app.post("/api/extension/server-automation-credentials")
def save_server_automation_credentials(payload: ServerAutomationCredentialsPayload, user_id: str = Depends(get_current_user_id)):
    """
    Reçoit le cookie de session Vinted capturé par l'extension, seulement
    envoyé quand l'utilisateur a explicitement activé l'opt-in (voir
    /api/settings/server-automation-optin). Efface tout état "invalidé"
    précédent : un nouveau cookie reçu signifie une session fraîche.
    """
    sb = get_supabase()
    account_id = resolve_vinted_account_id(sb, user_id, vinted_user_id=payload.vinted_user_id)
    if not account_id:
        raise HTTPException(status_code=400, detail="Aucun compte Vinted connecté.")
    sb.table("vinted_session_credentials").upsert({
        "vinted_account_id": account_id,
        "user_id": user_id,
        "session_cookie": payload.session_cookie,
        "anon_id": payload.anon_id,
        "csrf_token": payload.csrf_token,
        "user_agent": payload.user_agent,
        "captured_at": datetime.utcnow().isoformat(),
        "invalidated_at": None,
        "last_error": None,
    }, on_conflict="vinted_account_id").execute()
    return {"ok": True}


@app.get("/api/extension/server-automation-status")
def get_server_automation_status(vinted_user_id: str = "", vinted_account_id: str = "", user_id: str = Depends(get_current_user_id)):
    """
    Métadonnées non-sensibles sur l'état de l'automatisation serveur (jamais
    le cookie/csrf lui-même) — pour diagnostiquer sans exposer de credentials.
    """
    sb = get_supabase()
    account_id = resolve_vinted_account_id(sb, user_id, vinted_user_id, vinted_account_id)
    if not account_id:
        return {"has_credentials": False}
    res = sb.table("vinted_session_credentials").select(
        "captured_at,last_used_at,last_error,invalidated_at"
    ).eq("vinted_account_id", account_id).limit(1).execute()
    if not res.data:
        return {"has_credentials": False}
    row = res.data[0]
    return {
        "has_credentials": True,
        "captured_at": row.get("captured_at"),
        "last_used_at": row.get("last_used_at"),
        "last_error": row.get("last_error"),
        "invalidated_at": row.get("invalidated_at"),
    }


class ServerAutomationOptInPayload(BaseModel):
    enabled: bool
    vinted_account_id: str
    consent_text: str = ""


@app.post("/api/settings/server-automation-optin")
def set_server_automation_optin(payload: ServerAutomationOptInPayload, user_id: str = Depends(get_current_user_id)):
    """
    Active/désactive l'automatisation serveur pour un compte Vinted précis.
    À l'activation, enregistre le texte de consentement affiché à
    l'utilisateur (traçabilité) et l'horodatage.
    """
    sb = get_supabase()
    account_id = resolve_vinted_account_id(sb, user_id, vinted_account_id=payload.vinted_account_id)
    if not account_id:
        raise HTTPException(status_code=400, detail="Aucun compte Vinted connecté.")

    existing = sb.table("vinted_automessage_settings").select("*").eq("vinted_account_id", account_id).limit(1).execute()
    row = existing.data[0] if existing.data else {
        "enabled": False, "template": "", "delay_min_sec": 60, "delay_max_sec": 180,
        "daily_limit": 20, "batch_size": 1,
    }
    update = {
        "user_id": user_id,
        "vinted_account_id": account_id,
        "enabled": row.get("enabled", False),
        "template": row.get("template", ""),
        "delay_min_sec": row.get("delay_min_sec", 60),
        "delay_max_sec": row.get("delay_max_sec", 180),
        "daily_limit": row.get("daily_limit", 20),
        "batch_size": row.get("batch_size", 1),
        "server_automation_enabled": payload.enabled,
        "updated_at": datetime.utcnow().isoformat(),
    }
    if payload.enabled:
        update["server_automation_consented_at"] = datetime.utcnow().isoformat()
        update["server_automation_consent_text"] = payload.consent_text[:2000]
    sb.table("vinted_automessage_settings").upsert(update, on_conflict="vinted_account_id").execute()
    return {"ok": True}

# ── Job planifié : rejoue runAutoMessageFavoris() côté serveur ──
# Ne tourne que pour les comptes ayant explicitement activé l'opt-in (voir
# set_server_automation_optin ci-dessus). Port direct de la logique
# extension/background.js (runAutoMessageFavoris, ~ligne 563-720).

import asyncio
import re
import random
import requests

def _record_sent_message(sb: Client, user_id: str, account_id: str, notif_id: str, recipient_login: str, message: str):
    """Partagée entre mark_messaged() (extension) et le job serveur — même
    upsert idempotent par id de notification Vinted."""
    sb.table("vinted_sent_messages").upsert({
        "id": notif_id,
        "user_id": user_id,
        "vinted_account_id": account_id,
        "recipient_login": recipient_login[:100],
        "message": message[:1000],
    }, on_conflict="id").execute()


def _invalidate_server_automation(sb: Client, account_id: str, error: str):
    sb.table("vinted_session_credentials").update({
        "invalidated_at": datetime.utcnow().isoformat(),
        "last_error": error,
    }).eq("vinted_account_id", account_id).execute()
    sb.table("vinted_automessage_settings").update({
        "server_automation_enabled": False,
    }).eq("vinted_account_id", account_id).execute()


def _vinted_headers(creds: dict, csrf: str) -> dict:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "x-csrf-token": csrf,
        "Cookie": creds["session_cookie"],
        "User-Agent": creds.get("user_agent") or "Mozilla/5.0",
    }
    if creds.get("anon_id"):
        headers["x-anon-id"] = creds["anon_id"]
    return headers


def _fetch_fresh_csrf(session_cookie: str, user_agent: str) -> str | None:
    r = requests.get(
        "https://www.vinted.fr/items/new",
        headers={"Cookie": session_cookie, "User-Agent": user_agent or "Mozilla/5.0"},
        timeout=15,
    )
    print(f"[server_automation] csrf fetch: status={r.status_code} url_final={r.url} taille_reponse={len(r.text)} debut={r.text[:150]!r}")
    if r.status_code in (401, 403):
        raise PermissionError(str(r.status_code))
    m = re.search(r'CSRF_TOKEN\\*"\s*:\s*\\*"([a-f0-9-]{20,})', r.text, re.IGNORECASE)
    return m.group(1) if m else None


_FAVORITE_LINK_RE = re.compile(r"\?offering_id=|messaging\?item_id=\d+&user_id=\d+|want_it\?receiver_id=\d+&item_id=\d+")


def _find_one_eligible_sync(headers: dict, exclude_notif_ids: set) -> tuple[dict | None, list]:
    debug_trace = []
    r = requests.get(
        "https://api.vinted.fr/inbox-notifications/v1/notifications?page=1&per_page=20",
        headers=headers, timeout=15,
    )
    if r.status_code in (401, 403):
        raise PermissionError(str(r.status_code))
    if not r.ok:
        return None, [{"skip": f"HTTP {r.status_code}"}]
    notifications = r.json().get("notifications", [])
    favorite_notifs = [n for n in notifications if n.get("link") and _FAVORITE_LINK_RE.search(n["link"])]

    for n in favorite_notifs:
        notif_id = str(n.get("id"))
        if notif_id in exclude_notif_ids:
            continue
        link = n["link"]
        messaging_match = re.search(r"messaging\?item_id=(\d+)&user_id=(\d+)", link)
        want_it_match = re.search(r"want_it\?receiver_id=(\d+)&item_id=(\d+)", link)
        offering_match = re.search(r"offering_id=(\d+)", link)
        item_id = messaging_match.group(1) if messaging_match else (want_it_match.group(2) if want_it_match else str(n.get("subject_id")))
        opposite_user_id = (
            messaging_match.group(2) if messaging_match
            else want_it_match.group(1) if want_it_match
            else offering_match.group(1) if offering_match else None
        )
        if not opposite_user_id:
            debug_trace.append({"id": notif_id, "skip": "no_user_id_resolved"})
            continue
        conv_r = requests.post(
            "https://www.vinted.fr/api/v2/conversations",
            headers={**headers, "Content-Type": "application/json"},
            json={"initiator": "seller_enters_notification", "item_id": item_id, "opposite_user_id": opposite_user_id},
            timeout=15,
        )
        if conv_r.status_code in (401, 403):
            raise PermissionError(str(conv_r.status_code))
        if not conv_r.ok:
            debug_trace.append({"id": notif_id, "skip": f"HTTP {conv_r.status_code}"})
            continue
        conv = conv_r.json().get("conversation")
        if not conv or conv.get("messages"):
            debug_trace.append({"id": notif_id, "skip": "no_conversation_or_already_replied"})
            continue
        name_match = re.match(r"^(.+?)\s+a marqué", n.get("body") or "")
        return {
            "conversation_id": str(conv["id"]), "notif_id": notif_id,
            "name": (conv.get("opposite_user") or {}).get("login") or (name_match.group(1) if name_match else ""),
        }, debug_trace
    return None, debug_trace


async def run_server_automation_cycle():
    sb = get_supabase()
    accounts = sb.table("vinted_automessage_settings").select("*") \
        .eq("enabled", True).eq("server_automation_enabled", True).execute()
    print(f"[server_automation] cycle start, {len(accounts.data or [])} compte(s) éligible(s)")

    for settings in (accounts.data or []):
        account_id = settings["vinted_account_id"]
        user_id = settings["user_id"]
        creds_res = sb.table("vinted_session_credentials").select("*").eq("vinted_account_id", account_id).limit(1).execute()
        if not creds_res.data or creds_res.data[0].get("invalidated_at"):
            print(f"[server_automation] {account_id}: pas de credentials valides, skip")
            continue
        creds = creds_res.data[0]
        print(f"[server_automation] {account_id}: credentials trouvés, cookie non-vide={bool(creds.get('session_cookie'))}")

        today = date.today().isoformat()
        sent_res = sb.table("vinted_sent_messages").select("id", count="exact") \
            .eq("vinted_account_id", account_id).gte("sent_at", f"{today}T00:00:00").execute()
        sent_today = sent_res.count or 0
        daily_limit = int(settings.get("daily_limit") or 20)
        if sent_today >= daily_limit:
            continue
        batch_size = max(1, min(int(settings.get("batch_size") or 1), daily_limit - sent_today))

        try:
            csrf = await asyncio.to_thread(_fetch_fresh_csrf, creds["session_cookie"], creds.get("user_agent") or "")
            print(f"[server_automation] {account_id}: csrf trouvé={bool(csrf)}")
            if not csrf:
                continue
            headers = _vinted_headers(creds, csrf)

            exclude_notif_ids = set()
            for i in range(batch_size):
                if i > 0:
                    await asyncio.sleep(random.uniform(
                        int(settings.get("delay_min_sec") or 60), int(settings.get("delay_max_sec") or 180)
                    ))
                found, _debug = await asyncio.to_thread(_find_one_eligible_sync, headers, exclude_notif_ids)
                if not found:
                    break
                exclude_notif_ids.add(found["notif_id"])
                message = (settings.get("template") or "").replace("{item}", found["name"] or "cet article")
                if not message.strip():
                    break
                reply_r = await asyncio.to_thread(
                    requests.post,
                    f"https://www.vinted.fr/api/v2/conversations/{found['conversation_id']}/replies",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"reply": {"body": message, "photo_temp_uuids": None}},
                    timeout=15,
                )
                if reply_r.status_code in (401, 403):
                    raise PermissionError(str(reply_r.status_code))
                if reply_r.ok:
                    _record_sent_message(sb, user_id, account_id, found["notif_id"], found["name"], message)

            sb.table("vinted_session_credentials").update({"last_used_at": datetime.utcnow().isoformat()}).eq("vinted_account_id", account_id).execute()
        except PermissionError as exc:
            print(f"[server_automation] {account_id}: session invalidée ({exc})")
            _invalidate_server_automation(sb, account_id, str(exc))
        except Exception as exc:
            print(f"[server_automation] {account_id}: exception {type(exc).__name__}: {exc}")
            capture_error(exc)


@app.on_event("startup")
async def start_server_automation_worker():
    print("[server_automation] worker démarré")
    async def loop():
        while True:
            try:
                await run_server_automation_cycle()
            except Exception as exc:
                print(f"[server_automation] exception au niveau boucle: {type(exc).__name__}: {exc}")
                capture_error(exc)
            await asyncio.sleep(300)
    asyncio.create_task(loop())


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
        "batch_size": 1,
    }
    enabled = bool(settings.get("enabled"))
    frequency_days = int(settings.get("frequency_days") or 3)
    daily_limit = int(settings.get("daily_limit") or 5)
    batch_size = max(1, int(settings.get("batch_size") or 1))

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

    # Republication prioritaire (clic manuel "Republier maintenant" sur le
    # site) : consommée une seule fois, indépendamment de enabled/daily_limit
    # puisque c'est une action explicite de l'utilisateur, pas le cycle auto.
    priority_item_id = settings.get("priority_item_id")
    if priority_item_id and account_id:
        sb.table("vinted_republish_settings").update({"priority_item_id": None}) \
            .eq("vinted_account_id", account_id).execute()

    return {
        "enabled": enabled,
        "frequency_days": frequency_days,
        "daily_limit": daily_limit,
        "batch_size": batch_size,
        "republished_today": republished_today,
        "eligible_vinted_item_ids": eligible_vinted_item_ids,
        "priority_vinted_item_id": priority_item_id,
    }


class RepublishNowPayload(BaseModel):
    vinted_item_id: str
    vinted_account_id: str = ""


@app.post("/api/settings/republish-now")
def republish_now(payload: RepublishNowPayload, user_id: str = Depends(get_current_user_id)):
    """Marque un article pour republication prioritaire au tout prochain
    cycle de synchro de l'extension (≤5 min), déclenché par le bouton
    "Republier maintenant" du site — indépendant du réglage d'automatisation."""
    sb = get_supabase()
    account_id = resolve_vinted_account_id(sb, user_id, vinted_account_id=payload.vinted_account_id)
    if not account_id:
        raise HTTPException(status_code=400, detail="Aucun compte Vinted connecté.")
    # Une erreur Postgrest ici (ex: colonne manquante — vécu le 2026-07-16,
    # migration supabase_republish_priority.sql jamais exécutée) plantait la
    # requête sans réponse HTTP propre, ce qui remontait côté navigateur comme
    # un simple "Failed to fetch" impossible à diagnostiquer depuis le site.
    try:
        sb.table("vinted_republish_settings").upsert({
            "vinted_account_id": account_id,
            "user_id": user_id,
            "priority_item_id": payload.vinted_item_id,
        }, on_conflict="vinted_account_id").execute()
    except Exception as e:
        capture_error(e)
        raise HTTPException(status_code=500, detail=f"Échec de la programmation : {e}")
    return {"ok": True}


class RepublishSettingsPayload(BaseModel):
    enabled: bool = False
    frequency_days: int = 3
    daily_limit: int = 5
    batch_size: int = 1
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
        "batch_size": max(1, min(5, payload.batch_size)),
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
        .select("id,sku")
        .eq("user_id", user_id)
        .eq("vinted_item_id", payload.old_vinted_item_id)
    )
    existing_query = existing_query.eq("vinted_account_id", account_id) if account_id else existing_query
    existing = existing_query.limit(1).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Article introuvable pour cet ancien vinted_item_id.")
    article_id = existing.data[0]["id"]
    sku = existing.data[0].get("sku")

    sb.table("articles").update({
        "vinted_item_id": payload.new_vinted_item_id,
        "synced_at": date.today().isoformat(),
    }).eq("id", article_id).execute()

    # Le lien "listing" doit suivre le nouvel id — sinon le prochain sync ne
    # reconnaît plus cet article via vinted_links et retombe sur le
    # rapprochement par nom (voir resolve_sku), moins fiable.
    if sku:
        sb.table("vinted_links").delete().eq("context", "listing").eq("vinted_id", payload.old_vinted_item_id).execute()
        sb.table("vinted_links").upsert(
            {"sku": sku, "context": "listing", "vinted_id": payload.new_vinted_item_id},
            on_conflict="context,vinted_id",
        ).execute()

    # Nettoyage d'un cas limite rare mais réel : si une synchro s'est
    # déclenchée pile entre la création de la nouvelle annonce et la
    # suppression de l'ancienne (delete+recreate volontairement dans cet
    # ordre pour la sécurité), Vinted renvoyait encore l'ancien id comme
    # actif à ce moment précis — une ligne orpheline a pu être créée pour
    # cet ancien id, jamais nettoyée depuis puisque Vinted ne le renvoie
    # plus jamais dans les synchros suivantes. On la supprime ici, juste
    # après avoir renommé la ligne d'origine, pour ne jamais la laisser
    # traîner en double dans le stock de l'utilisateur.
    delete_query = (
        sb.table("articles")
        .delete()
        .eq("user_id", user_id)
        .eq("vinted_item_id", payload.old_vinted_item_id)
        .neq("id", article_id)
    )
    delete_query = delete_query.eq("vinted_account_id", account_id) if account_id else delete_query
    delete_query.execute()

    sb.table("vinted_republish_log").upsert({
        "article_id": article_id,
        "user_id": user_id,
        "vinted_account_id": account_id,
        "last_republished_at": datetime.utcnow().isoformat(),
    }, on_conflict="article_id").execute()

    return {"ok": True}


class ResyncArticlePayload(BaseModel):
    id: str
    vinted_account_id: str = ""


@app.post("/api/settings/resync-article")
def resync_article(payload: ResyncArticlePayload, user_id: str = Depends(get_current_user_id)):
    """
    Marque un article pour être totalement réécrasé par la vraie donnée
    Vinted au prochain cycle de synchro (≤5 min) — utilisé par le bouton
    "Réinitialiser depuis Vinted" quand un changement manuel dans VintControl
    a fait diverger le statut affiché de l'état réel sur Vinted.
    """
    sb = get_supabase()
    res = sb.table("articles").update({"force_resync": True}) \
        .eq("id", payload.id).eq("user_id", user_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Article introuvable.")
    return {"ok": True}


class ResyncAllPayload(BaseModel):
    vinted_account_id: str = ""


@app.post("/api/settings/resync-all")
def resync_all(payload: ResyncAllPayload, user_id: str = Depends(get_current_user_id)):
    """
    Même principe que resync-article, mais pour tous les articles Vinted du
    compte à la fois — chacun sera réécrasé par la vraie donnée Vinted à sa
    prochaine apparition dans annonces/ventes (≤5 min pour ceux encore actifs
    ou vendus ; ceux qui ont disparu de Vinted entièrement, ni annonce ni
    vente, ne seront pas touchés par ce mécanisme).
    """
    sb = get_supabase()
    query = sb.table("articles").update({"force_resync": True}) \
        .eq("user_id", user_id).eq("platform", "Vinted").not_.is_("vinted_item_id", "null")
    if payload.vinted_account_id:
        query = query.eq("vinted_account_id", payload.vinted_account_id)
    res = query.execute()
    return {"ok": True, "count": len(res.data or [])}


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


# ============================================================
# ROUTE: Proxy image (détection de doublons par photo)
# ============================================================

from urllib.parse import urlparse

_IMAGE_PROXY_ALLOWED_SUFFIXES = (".vinted.net", ".vinted.fr", ".vinted.com")


@app.get("/api/image-proxy")
def image_proxy(url: str, user_id: str = Depends(get_current_user_id)):
    """
    Rejoue une photo d'article (CDN Vinted ou stockage Supabase) avec un
    Access-Control-Allow-Origin permissif : les CDN d'origine n'en envoient
    pas, ce qui "taint" le canvas côté navigateur et empêche d'en lire les
    pixels. Nécessaire pour le hash perceptuel du détecteur de doublons par
    photo (js/app.js, computePhotoHash côté site). Liste blanche stricte de
    domaines pour ne pas devenir un proxy HTTP ouvert (SSRF).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="URL invalide.")
    host = (parsed.hostname or "").lower()
    supabase_host = urlparse(SUPABASE_URL).hostname if SUPABASE_URL else None
    allowed = host.endswith(_IMAGE_PROXY_ALLOWED_SUFFIXES) or (supabase_host and host == supabase_host)
    if not allowed:
        raise HTTPException(status_code=400, detail="Domaine non autorisé.")
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
    except Exception:
        raise HTTPException(status_code=502, detail="Impossible de récupérer l'image.")
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/jpeg"),
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"},
    )


# ============================================================
# ROUTE: Notifications Web Push (nouvelle vente / nouveau favori)
# ============================================================
# Limite connue, pas un bug : la détection (nouvelle vente, nouveau favori)
# est faite par l'extension Chrome pendant sa synchro périodique — donc SUR
# L'ORDINATEUR de l'utilisateur. Si l'ordinateur est éteint, l'extension ne
# tourne pas et rien n'est détecté : la notif arrive au prochain rallumage/
# synchro, pas en temps réel. Un vrai temps réel 24/7 demanderait un
# navigateur headless tournant en continu côté serveur (voir la même
# limite documentée pour l'automatisation serveur des messages favoris,
# supabase_server_automation.sql) — hors scope ici, accepté en connaissance
# de cause avec l'utilisateur le 2026-07-21.
#
# Abonnement (subscribe/unsubscribe) géré directement par le site via
# supabase-js (RLS auth.uid()=user_id, voir supabase_push_subscriptions.sql) :
# seul l'ENVOI a besoin de la clé privée VAPID, jamais exposée au navigateur.

from pywebpush import webpush, WebPushException
import json as json_lib

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_CLAIMS_EMAIL = os.getenv("VAPID_CLAIMS_EMAIL", "mailto:contact@vintcontrol.com")


def send_push_to_user(sb: Client, user_id: str, title: str, body: str, url: str = "/") -> None:
    if not VAPID_PRIVATE_KEY:
        return
    subs = sb.table("push_subscriptions").select("*").eq("user_id", user_id).execute()
    for sub in (subs.data or []):
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=json_lib.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIMS_EMAIL},
                timeout=10,
            )
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                # Abonnement expiré/révoqué côté navigateur (désinstallation,
                # nettoyage des données...) : on le retire pour ne pas réessayer
                # indéfiniment dans le vide.
                sb.table("push_subscriptions").delete().eq("id", sub["id"]).execute()
        except Exception as e:
            capture_error(e)


class PushNotifyPayload(BaseModel):
    title: str
    body: str
    url: str = "/"


@app.post("/api/push/notify")
def push_notify(payload: PushNotifyPayload, user_id: str = Depends(get_current_user_id)):
    """Appelé par l'extension Chrome quand elle détecte une nouvelle vente ou
    un nouveau favori pendant sa synchro (voir notifyNewSalesAndFavorites
    dans background.js)."""
    send_push_to_user(get_supabase(), user_id, payload.title, payload.body, payload.url)
    return {"ok": True}


