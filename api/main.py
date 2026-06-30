"""
Vinted Manager — Backend de synchronisation Vinted
====================================================
Reçoit les données envoyées par l'extension Chrome (ventes, annonces, messages)
et les sauvegarde dans Supabase pour les afficher automatiquement dans
Vinted Manager.

Aucune donnée sensible (mot de passe) n'est jamais stockée.
"""

import os
from datetime import date
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
    ventes: list = []      # articles vendus
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
                "source": "Vinted",
                "synced_at": today,
            }, on_conflict="vinted_item_id").execute()
            articles_upserted += 1
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
            sb.table("vinted_accounts").upsert({
                "user_id": user_id,
                "vinted_login": payload.vinted_login,
                "vinted_user_id": payload.vinted_user_id,
                "last_sync": today,
                "connected": True,
            }, on_conflict="user_id").execute()
        except Exception:
            pass

    return {
        "ok": True,
        "articles_upserted": articles_upserted,
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

