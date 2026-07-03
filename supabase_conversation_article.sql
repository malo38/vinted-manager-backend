-- ============================================================
-- Vinted Manager — Titre de l'article lié à chaque conversation
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================

ALTER TABLE vinted_conversations ADD COLUMN IF NOT EXISTS article_titre TEXT;

NOTIFY pgrst, 'reload schema';
