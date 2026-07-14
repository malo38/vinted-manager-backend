-- ============================================================
-- VintControl — Vrai multicompte Vinted
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================
--
-- Ce fichier a DEUX parties à exécuter dans deux passages séparés :
--
--   PARTIE 1a (additive uniquement) : à exécuter MAINTENANT, avant tout
--   déploiement du nouveau code backend. Elle ajoute les nouvelles colonnes
--   et contraintes SANS toucher aux anciennes — l'ancien code (api/main.py
--   pas encore mis à jour) continue de fonctionner sans interruption.
--
--   PARTIE 1b (bascule) : à exécuter SEULEMENT une fois le nouveau
--   api/main.py déployé et vérifié en conditions réelles (voir le plan de
--   test). Elle supprime les anciennes contraintes 1 compte = 1 ligne, qui
--   deviendraient sinon un piège si un utilisateur connecte un 2e compte
--   Vinted avant que le nouveau code ne soit prêt à le gérer.
--
-- Ne PAS exécuter la partie 1b avant d'avoir confirmé que la partie 2
-- (backend) tourne en production sans erreur.

-- ============================================================
-- PARTIE 1a — additive, sûre à exécuter immédiatement
-- ============================================================

-- --- vinted_accounts devient une vraie table multi-lignes -----------------
-- Avant : PRIMARY KEY (user_id) => 1 seul compte Vinted par utilisateur.
-- Après : une clé de substitution `id`, et un utilisateur peut avoir
-- plusieurs lignes (une par compte Vinted connecté), chacune identifiée de
-- façon unique par (user_id, vinted_user_id).
ALTER TABLE vinted_accounts ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
UPDATE vinted_accounts SET id = gen_random_uuid() WHERE id IS NULL;
ALTER TABLE vinted_accounts ALTER COLUMN id SET NOT NULL;
-- `id` doit être UNIQUE dès maintenant : les colonnes vinted_account_id
-- ajoutées plus bas la référencent en clé étrangère, et Postgres exige une
-- contrainte unique sur la colonne référencée pour l'autoriser (la vraie
-- clé primaire, elle, n'arrive qu'en partie 1b).
ALTER TABLE vinted_accounts ADD CONSTRAINT vinted_accounts_id_unique UNIQUE (id);

-- Avant de continuer : vérifiez qu'aucune ligne n'a un vinted_user_id vide
-- (compte jamais synchronisé) — ça n'empêche rien (NULL n'entre pas en
-- conflit dans une contrainte UNIQUE Postgres), mais bon à savoir :
--   SELECT * FROM vinted_accounts WHERE vinted_user_id IS NULL OR vinted_user_id = '';
ALTER TABLE vinted_accounts ADD CONSTRAINT vinted_accounts_user_vinted_unique UNIQUE (user_id, vinted_user_id);

-- --- vinted_account_id sur chaque table dépendante -------------------------
-- Colonne ajoutée + rattrapage (backfill) : comme avant cette migration
-- chaque utilisateur avait au plus 1 ligne vinted_accounts, on peut relier
-- sans ambiguïté chaque ligne existante à ce compte via user_id.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS vinted_account_id UUID REFERENCES vinted_accounts(id) ON DELETE CASCADE;
UPDATE articles a SET vinted_account_id = va.id
  FROM vinted_accounts va
  WHERE va.user_id = a.user_id AND a.platform = 'Vinted' AND a.vinted_account_id IS NULL;

ALTER TABLE vinted_conversations ADD COLUMN IF NOT EXISTS vinted_account_id UUID REFERENCES vinted_accounts(id) ON DELETE CASCADE;
UPDATE vinted_conversations c SET vinted_account_id = va.id
  FROM vinted_accounts va
  WHERE va.user_id = c.user_id AND c.vinted_account_id IS NULL;

ALTER TABLE vinted_purchases ADD COLUMN IF NOT EXISTS vinted_account_id UUID REFERENCES vinted_accounts(id) ON DELETE CASCADE;
UPDATE vinted_purchases p SET vinted_account_id = va.id
  FROM vinted_accounts va
  WHERE va.user_id = p.user_id AND p.vinted_account_id IS NULL;

ALTER TABLE vinted_stats_history ADD COLUMN IF NOT EXISTS vinted_account_id UUID REFERENCES vinted_accounts(id) ON DELETE CASCADE;
UPDATE vinted_stats_history s SET vinted_account_id = va.id
  FROM vinted_accounts va
  WHERE va.user_id = s.user_id AND s.vinted_account_id IS NULL;

ALTER TABLE vinted_automessage_settings ADD COLUMN IF NOT EXISTS vinted_account_id UUID REFERENCES vinted_accounts(id) ON DELETE CASCADE;
UPDATE vinted_automessage_settings m SET vinted_account_id = va.id
  FROM vinted_accounts va
  WHERE va.user_id = m.user_id AND m.vinted_account_id IS NULL;

ALTER TABLE vinted_sent_messages ADD COLUMN IF NOT EXISTS vinted_account_id UUID REFERENCES vinted_accounts(id) ON DELETE CASCADE;
UPDATE vinted_sent_messages sm SET vinted_account_id = va.id
  FROM vinted_accounts va
  WHERE va.user_id = sm.user_id AND sm.vinted_account_id IS NULL;

ALTER TABLE vinted_republish_settings ADD COLUMN IF NOT EXISTS vinted_account_id UUID REFERENCES vinted_accounts(id) ON DELETE CASCADE;
UPDATE vinted_republish_settings r SET vinted_account_id = va.id
  FROM vinted_accounts va
  WHERE va.user_id = r.user_id AND r.vinted_account_id IS NULL;

ALTER TABLE vinted_republish_log ADD COLUMN IF NOT EXISTS vinted_account_id UUID REFERENCES vinted_accounts(id) ON DELETE CASCADE;
UPDATE vinted_republish_log l SET vinted_account_id = va.id
  FROM vinted_accounts va
  WHERE va.user_id = l.user_id AND l.vinted_account_id IS NULL;

-- --- Nouvelles contraintes composites (coexistent avec les anciennes) -----
-- Ces contraintes évitent les collisions entre 2 comptes Vinted différents
-- (ex: deux comptes qui auraient chacun un vinted_item_id numériquement égal
-- à un autre — improbable côté Vinted mais pas garanti).

ALTER TABLE articles ADD CONSTRAINT articles_account_item_unique UNIQUE (vinted_account_id, vinted_item_id);
ALTER TABLE vinted_stats_history ADD CONSTRAINT vinted_stats_history_account_item_date_unique UNIQUE (vinted_account_id, vinted_item_id, stat_date);

NOTIFY pgrst, 'reload schema';

-- ============================================================
-- PARTIE 1b — bascule destructive, à exécuter APRÈS avoir déployé et
-- vérifié le nouveau backend (voir plan de test). NE PAS exécuter avant.
-- ============================================================
--
-- Avant d'exécuter cette partie, retrouvez le vrai nom de la contrainte
-- UNIQUE existante sur articles.vinted_item_id (générée automatiquement,
-- son nom peut varier) :
--   SELECT conname FROM pg_constraint WHERE conrelid = 'articles'::regclass AND contype = 'u';
-- Adaptez le nom ci-dessous si besoin (articles_vinted_item_id_key est le nom
-- attendu vu comment la colonne a été créée dans supabase_vinted_sync.sql).
--
-- ALTER TABLE articles DROP CONSTRAINT IF EXISTS articles_vinted_item_id_key;
--
-- ALTER TABLE vinted_stats_history DROP CONSTRAINT IF EXISTS vinted_stats_history_vinted_item_id_stat_date_key;
--
-- -- vinted_automessage_settings et vinted_republish_settings deviennent de
-- -- vrais réglages PAR COMPTE Vinted (plus par utilisateur VintControl) :
-- -- vérifiez d'abord qu'aucune ligne n'a vinted_account_id NULL (un réglage
-- -- créé avant toute connexion Vinted, cas limite improbable vu le parcours
-- -- UI actuel — si ça arrive, supprimez la ligne orpheline plutôt que de la
-- -- migrer) :
-- --   SELECT * FROM vinted_automessage_settings WHERE vinted_account_id IS NULL;
-- --   SELECT * FROM vinted_republish_settings WHERE vinted_account_id IS NULL;
--
-- ALTER TABLE vinted_automessage_settings DROP CONSTRAINT IF EXISTS vinted_automessage_settings_pkey;
-- ALTER TABLE vinted_automessage_settings ALTER COLUMN vinted_account_id SET NOT NULL;
-- ALTER TABLE vinted_automessage_settings ADD PRIMARY KEY (vinted_account_id);
--
-- ALTER TABLE vinted_republish_settings DROP CONSTRAINT IF EXISTS vinted_republish_settings_pkey;
-- ALTER TABLE vinted_republish_settings ALTER COLUMN vinted_account_id SET NOT NULL;
-- ALTER TABLE vinted_republish_settings ADD PRIMARY KEY (vinted_account_id);
--
-- -- vinted_accounts : la clé primaire passe de user_id à id. Attention, le
-- -- nom réel de la contrainte PK peut varier — vérifiez avant si besoin :
-- --   SELECT conname FROM pg_constraint WHERE conrelid = 'vinted_accounts'::regclass AND contype = 'p';
-- ALTER TABLE vinted_accounts DROP CONSTRAINT IF EXISTS vinted_accounts_pkey;
-- ALTER TABLE vinted_accounts ADD PRIMARY KEY (id);
-- -- La contrainte UNIQUE temporaire posée en 1a devient redondante une fois
-- -- la vraie PRIMARY KEY en place (qui est elle-même UNIQUE) :
-- ALTER TABLE vinted_accounts DROP CONSTRAINT IF EXISTS vinted_accounts_id_unique;
--
-- NOTIFY pgrst, 'reload schema';

-- ============================================================
-- Note RLS : aucune politique n'a besoin de changer. Chaque table garde sa
-- colonne user_id et ses policies USING (auth.uid() = user_id) — avoir
-- plusieurs lignes vinted_account_id par utilisateur n'est pas du
-- multi-tenant, juste plusieurs lignes appartenant au même utilisateur.
-- ============================================================
