-- Abonnements Web Push (notifications navigateur/téléphone "nouvelle vente"/
-- "nouveau favori"). Contrairement à vinted_session_credentials (cookie de
-- session Vinted, très sensible), une souscription push ne permet que de
-- recevoir des notifications — RLS classique (auth.uid()=user_id) suffit,
-- le site peut lire/écrire directement via supabase-js sans passer par le
-- backend. Seul l'ENVOI (POST /api/push/notify) nécessite la clé privée
-- VAPID, jamais exposée au navigateur.
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  endpoint TEXT UNIQUE NOT NULL,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE push_subscriptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users manage their own push subscriptions" ON push_subscriptions
  FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
NOTIFY pgrst, 'reload schema';
