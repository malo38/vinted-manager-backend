-- ============================================================
-- Vinted Manager — Solde du porte-monnaie Vinted
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================

ALTER TABLE vinted_accounts ADD COLUMN IF NOT EXISTS wallet_balance NUMERIC DEFAULT 0;
ALTER TABLE vinted_accounts ADD COLUMN IF NOT EXISTS wallet_pending_balance NUMERIC DEFAULT 0;

NOTIFY pgrst, 'reload schema';
