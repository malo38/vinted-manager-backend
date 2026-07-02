-- ============================================================
-- Vinted Manager — Frais annexes + retours utilisateurs
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================

-- 1. Frais annexes par article (emballage, essence, frais divers) — inclus dans le profit net
ALTER TABLE articles ADD COLUMN IF NOT EXISTS extra_costs NUMERIC DEFAULT 0;

-- 2. Retours utilisateurs (bugs / suggestions) envoyés depuis l'app
CREATE TABLE IF NOT EXISTS feedback (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  message TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users insert own feedback" ON feedback
  FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users see own feedback" ON feedback
  FOR SELECT USING (auth.uid() = user_id);
