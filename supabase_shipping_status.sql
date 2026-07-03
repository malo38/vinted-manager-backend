-- ============================================================
-- Vinted Manager — Statut de commande Vinted (ventes + achats)
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================

ALTER TABLE articles ADD COLUMN IF NOT EXISTS vinted_shipping_status TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS vinted_transaction_status TEXT;

ALTER TABLE vinted_purchases ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE vinted_purchases ADD COLUMN IF NOT EXISTS transaction_status TEXT;

NOTIFY pgrst, 'reload schema';
