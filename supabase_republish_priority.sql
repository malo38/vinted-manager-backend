-- ============================================================
-- VintControl — Republication prioritaire en un clic
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================
-- Colonne consommée une seule fois : le site la pose via
-- POST /api/settings/republish-now, l'extension la lit et la consomme
-- (remise à NULL) au prochain cycle de synchro, qu'elle passe outre
-- enabled/daily_limit puisque c'est un clic explicite de l'utilisateur.

ALTER TABLE vinted_republish_settings ADD COLUMN IF NOT EXISTS priority_item_id TEXT;

NOTIFY pgrst, 'reload schema';
