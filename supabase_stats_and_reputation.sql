-- ============================================================
-- Vinted Manager — Réputation vendeur + historique des vues/favoris
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================

-- 1. Réputation vendeur (avis, note, abonnés, nb d'articles) sur le compte Vinted connecté
ALTER TABLE vinted_accounts ADD COLUMN IF NOT EXISTS review_count INTEGER DEFAULT 0;
ALTER TABLE vinted_accounts ADD COLUMN IF NOT EXISTS feedback_reputation NUMERIC DEFAULT 0;
ALTER TABLE vinted_accounts ADD COLUMN IF NOT EXISTS followers_count INTEGER DEFAULT 0;
ALTER TABLE vinted_accounts ADD COLUMN IF NOT EXISTS vinted_item_count INTEGER DEFAULT 0;

-- 2. Historique quotidien des vues/favoris par annonce (pour un vrai graphique de tendance)
CREATE TABLE IF NOT EXISTS vinted_stats_history (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  vinted_item_id TEXT NOT NULL,
  stat_date DATE NOT NULL DEFAULT CURRENT_DATE,
  vues INTEGER DEFAULT 0,
  favoris INTEGER DEFAULT 0,
  UNIQUE (vinted_item_id, stat_date)
);

ALTER TABLE vinted_stats_history ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users see own stats history" ON vinted_stats_history
  FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "Users manage own stats history" ON vinted_stats_history
  FOR ALL USING (auth.uid() = user_id);
