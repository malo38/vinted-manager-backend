-- ============================================================
-- VintControl — Réinitialisation forcée d'un article depuis Vinted
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================
-- Un article marqué force_resync=true échappe temporairement au garde-fou
-- "ne jamais faire régresser un article déjà vendu" lors de la prochaine
-- synchro : la vraie donnée Vinted du moment (annonces/ventes) l'écrase
-- normalement, une seule fois, puis le drapeau est remis à false.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS force_resync BOOLEAN DEFAULT false;

NOTIFY pgrst, 'reload schema';
