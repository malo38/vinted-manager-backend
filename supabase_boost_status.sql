-- ============================================================
-- VintControl — Statut Boost Vinted (affichage seul, aucun achat auto)
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================
-- "promoted" est renvoyé par l'API Vinted (/wardrobe/{id}/items) pour
-- indiquer qu'un article a un Boost payant actif — VintControl se contente
-- de l'afficher, il n'achète jamais de Boost automatiquement.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS vinted_boosted BOOLEAN DEFAULT FALSE;

NOTIFY pgrst, 'reload schema';
