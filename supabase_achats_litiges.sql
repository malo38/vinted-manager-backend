-- ============================================================
-- VintControl — Litiges/remboursements/compensations sur les achats
-- + coordonnées géocodées des points relais (demandé le 2026-07-23,
-- inspiré de Vinteer : "statut litige/remboursement/compensation
-- distincts" + "carte des points relais").
--
-- Le statut litige est géré manuellement (un clic dans l'app) : Vinted
-- ne renvoie nulle part un champ exploitable distinguant "en litige" de
-- "remboursé"/"compensé" dans les données déjà synchronisées
-- (transaction_user_status ne connaît que completed/failed/cancelled).
--
-- pickup_lat/pickup_lon : résultat mis en cache d'un géocodage côté
-- site (API Adresse — api-adresse.data.gouv.fr, gratuite, sans clé)
-- à partir du texte d'adresse déjà présent dans pickup_location — pour
-- ne géocoder chaque point relais qu'une seule fois, jamais à chaque
-- affichage de la page Achats.
--
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================

ALTER TABLE vinted_purchases ADD COLUMN IF NOT EXISTS dispute_status TEXT; -- null | 'litige' | 'rembourse' | 'compense'
ALTER TABLE vinted_purchases ADD COLUMN IF NOT EXISTS dispute_amount NUMERIC;
ALTER TABLE vinted_purchases ADD COLUMN IF NOT EXISTS dispute_note TEXT;
ALTER TABLE vinted_purchases ADD COLUMN IF NOT EXISTS dispute_updated_at TIMESTAMPTZ;
ALTER TABLE vinted_purchases ADD COLUMN IF NOT EXISTS pickup_lat NUMERIC;
ALTER TABLE vinted_purchases ADD COLUMN IF NOT EXISTS pickup_lon NUMERIC;

NOTIFY pgrst, 'reload schema';
