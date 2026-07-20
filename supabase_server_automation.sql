-- ============================================================
-- VintControl — Automatisation "messages aux favoris" côté serveur (opt-in)
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================
--
-- Fonctionnalité bêta, opt-in explicite : permet à un utilisateur d'accepter
-- que le backend rejoue sa session Vinted depuis son propre serveur pour
-- continuer d'envoyer les messages aux favoris même ordinateur éteint.
-- Risque connu et accepté par l'utilisateur au moment de l'activation (voir
-- consent_text) : le compte Vinted peut être flaggé/restreint par leur
-- anti-fraude, les requêtes ne partant plus de l'IP habituelle de
-- l'utilisateur.

-- --- Table de credentials, séparée et jamais exposée au client ------------
-- IMPORTANT : cette table ne doit JAMAIS avoir de policy RLS pour anon/
-- authenticated. Contrairement à vinted_accounts (qui a une policy
-- "FOR ALL USING (auth.uid() = user_id)", voir supabase_vinted_sync.sql),
-- ici on veut un deny-by-default : RLS activé mais SANS AUCUNE policy, ce
-- qui bloque tout accès sauf pour la clé service_role (utilisée uniquement
-- par le backend), qui bypass RLS nativement sous Supabase. Le site fait des
-- requêtes Supabase directes avec la clé anon (js/app.js) — une policy ici
-- rendrait le cookie de session Vinted lisible depuis le navigateur de
-- n'importe quel visiteur du site.
CREATE TABLE IF NOT EXISTS vinted_session_credentials (
  vinted_account_id UUID REFERENCES vinted_accounts(id) ON DELETE CASCADE PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  session_cookie TEXT NOT NULL,          -- cookies bruts sérialisés "nom=val; nom2=val2",
                                          -- prêts à réinjecter en header Cookie. Donnée aussi
                                          -- sensible qu'un mot de passe : ne jamais logger,
                                          -- ne jamais exposer à un client.
  anon_id TEXT,
  user_agent TEXT,                       -- rejoué tel quel pour ressembler à la requête
                                          -- originale du navigateur de l'utilisateur
  csrf_token TEXT,                       -- dernier CSRF connu, best-effort (se périme vite,
                                          -- le job serveur le re-scrape avant chaque usage)
  captured_at TIMESTAMPTZ DEFAULT NOW(),
  last_used_at TIMESTAMPTZ,
  last_error TEXT,                       -- dernier code d'erreur rencontré par le job
                                          -- (ex: "401", "403") pour diagnostic
  invalidated_at TIMESTAMPTZ             -- non-NULL dès que le job détecte une session morte ;
                                          -- efface automatiquement au prochain cookie reçu
);

ALTER TABLE vinted_session_credentials ENABLE ROW LEVEL SECURITY;
-- Aucune policy créée intentionnellement : voir commentaire ci-dessus.

-- --- Colonnes opt-in sur la table de réglages existante --------------------
-- Réutilise vinted_automessage_settings (déjà la table de réglages par
-- compte, PK vinted_account_id) plutôt que d'ajouter une nouvelle table pour
-- un simple flag + trace de consentement.
ALTER TABLE vinted_automessage_settings ADD COLUMN IF NOT EXISTS server_automation_enabled BOOLEAN DEFAULT false;
ALTER TABLE vinted_automessage_settings ADD COLUMN IF NOT EXISTS server_automation_consented_at TIMESTAMPTZ;
ALTER TABLE vinted_automessage_settings ADD COLUMN IF NOT EXISTS server_automation_consent_text TEXT;

NOTIFY pgrst, 'reload schema';
