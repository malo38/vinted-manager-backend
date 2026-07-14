-- ============================================================
-- Vinted Manager — Republication automatique des annonces
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================

-- Réglages (1 par utilisateur), même schéma que vinted_automessage_settings
CREATE TABLE IF NOT EXISTS vinted_republish_settings (
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE PRIMARY KEY,
  enabled BOOLEAN DEFAULT false,
  frequency_days INTEGER DEFAULT 3,
  daily_limit INTEGER DEFAULT 5,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE vinted_republish_settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users see own republish settings" ON vinted_republish_settings
  FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "Users manage own republish settings" ON vinted_republish_settings
  FOR ALL USING (auth.uid() = user_id);

-- Historique des republications, par article interne (articles.id) et non par
-- vinted_item_id : une republication change l'id Vinted de l'article (delete +
-- recreate), donc suivre l'article interne est le seul moyen fiable de savoir
-- depuis quand il n'a plus été republié.
CREATE TABLE IF NOT EXISTS vinted_republish_log (
  article_id UUID REFERENCES articles(id) ON DELETE CASCADE PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  last_republished_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE vinted_republish_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users see own republish log" ON vinted_republish_log
  FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "Users manage own republish log" ON vinted_republish_log
  FOR ALL USING (auth.uid() = user_id);

NOTIFY pgrst, 'reload schema';
