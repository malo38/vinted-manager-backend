-- ============================================================
-- Vinted Manager — Offres en attente sur les conversations
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================

ALTER TABLE vinted_conversations ADD COLUMN IF NOT EXISTS est_offre BOOLEAN DEFAULT false;
ALTER TABLE vinted_conversations ADD COLUMN IF NOT EXISTS offre_prix NUMERIC;
