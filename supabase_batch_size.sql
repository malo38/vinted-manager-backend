-- ============================================================
-- VintControl — Nombre d'actions par cycle de synchro (favoris/republication)
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================
-- Par défaut, 1 seul message/republication par cycle (~5 min), pour rester
-- discret vis-à-vis de Vinted. batch_size permet aux utilisateurs avertis
-- d'en traiter plusieurs par cycle — le site avertit clairement du risque
-- quand la valeur dépasse 1.

ALTER TABLE vinted_automessage_settings ADD COLUMN IF NOT EXISTS batch_size INT DEFAULT 1;
ALTER TABLE vinted_republish_settings ADD COLUMN IF NOT EXISTS batch_size INT DEFAULT 1;

NOTIFY pgrst, 'reload schema';
