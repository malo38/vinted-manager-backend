-- ============================================================
-- Vinted Manager — Table des achats Vinted (détection automatique)
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================

CREATE TABLE IF NOT EXISTS vinted_purchases (
  id TEXT PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  title TEXT,
  price NUMERIC DEFAULT 0,
  purchase_date DATE,
  photo_url TEXT,
  synced_at DATE DEFAULT CURRENT_DATE
);

ALTER TABLE vinted_purchases ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users see own purchases" ON vinted_purchases
  FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "Users manage own purchases" ON vinted_purchases
  FOR ALL USING (auth.uid() = user_id);
