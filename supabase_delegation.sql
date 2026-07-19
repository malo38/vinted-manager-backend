-- Délégation : un utilisateur (le "propriétaire") délègue la préparation
-- physique de ses articles (photo, etc.) à un autre utilisateur VintControl
-- existant (le "délégué"), avec un suivi de rémunération (à l'article ou à
-- l'heure). Le délégué n'a jamais un accès direct aux tables articles/
-- finances du propriétaire — tout passe par des fonctions RPC dédiées qui ne
-- renvoient/modifient que ce qui est nécessaire à sa tâche.
--
-- Les étapes de préparation (à laver/à photographier/à publier...) sont
-- personnalisables et stockées côté client (localStorage), pas en base —
-- task_status_key / next_status_key capturent donc, au moment où le
-- propriétaire configure la délégation, quelle étape déclenche une tâche et
-- vers quelle étape elle bascule une fois faite.

CREATE TABLE IF NOT EXISTS delegations (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  owner_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  delegate_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  delegate_email TEXT,
  compensation_type TEXT NOT NULL DEFAULT 'per_item' CHECK (compensation_type IN ('per_item','hourly')),
  hourly_rate NUMERIC DEFAULT 0,
  task_status_key TEXT NOT NULL DEFAULT 'photo',
  next_status_key TEXT NOT NULL DEFAULT 'publier',
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(owner_id, delegate_id)
);
ALTER TABLE delegations ENABLE ROW LEVEL SECURITY;
-- Le propriétaire gère la ligne ; le délégué peut seulement la consulter
-- (pour voir ses propres conditions de rémunération), pas la modifier.
CREATE POLICY "Owner manages delegations" ON delegations FOR ALL USING (auth.uid() = owner_id);
CREATE POLICY "Delegate reads own delegations" ON delegations FOR SELECT USING (auth.uid() = delegate_id);

CREATE TABLE IF NOT EXISTS delegation_rate_lines (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  delegation_id UUID REFERENCES delegations(id) ON DELETE CASCADE NOT NULL,
  label TEXT NOT NULL,
  amount NUMERIC NOT NULL DEFAULT 0,
  sort_order INT DEFAULT 0
);
ALTER TABLE delegation_rate_lines ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Owner manages rate lines" ON delegation_rate_lines FOR ALL USING (
  auth.uid() IN (SELECT owner_id FROM delegations WHERE id = delegation_id)
);
CREATE POLICY "Delegate reads own rate lines" ON delegation_rate_lines FOR SELECT USING (
  auth.uid() IN (SELECT delegate_id FROM delegations WHERE id = delegation_id)
);

CREATE TABLE IF NOT EXISTS delegation_tasks (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  delegation_id UUID REFERENCES delegations(id) ON DELETE CASCADE NOT NULL,
  article_id UUID REFERENCES articles(id) ON DELETE SET NULL,
  amount NUMERIC NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE delegation_tasks ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Owner reads own delegation tasks" ON delegation_tasks FOR SELECT USING (
  auth.uid() IN (SELECT owner_id FROM delegations WHERE id = delegation_id)
);
CREATE POLICY "Delegate reads own tasks" ON delegation_tasks FOR SELECT USING (
  auth.uid() IN (SELECT delegate_id FROM delegations WHERE id = delegation_id)
);

CREATE TABLE IF NOT EXISTS delegation_time_logs (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  delegation_id UUID REFERENCES delegations(id) ON DELETE CASCADE NOT NULL,
  hours NUMERIC NOT NULL,
  note TEXT,
  log_date DATE DEFAULT CURRENT_DATE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE delegation_time_logs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Owner reads own time logs" ON delegation_time_logs FOR SELECT USING (
  auth.uid() IN (SELECT owner_id FROM delegations WHERE id = delegation_id)
);
CREATE POLICY "Delegate manages own time logs" ON delegation_time_logs FOR ALL USING (
  auth.uid() IN (SELECT delegate_id FROM delegations WHERE id = delegation_id)
);

-- Recherche d'un utilisateur existant par email, sans exposer autre chose
-- que son id (nécessaire pour lier un délégué qui a déjà un compte).
CREATE OR REPLACE FUNCTION find_user_id_by_email(p_email TEXT)
RETURNS UUID
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT id FROM auth.users WHERE lower(email) = lower(p_email) LIMIT 1;
$$;
GRANT EXECUTE ON FUNCTION find_user_id_by_email(TEXT) TO authenticated;

-- Articles du propriétaire qui attendent l'étape déléguée (task_status_key,
-- ex: "photo") — seules les colonnes utiles à la tâche physique sont
-- renvoyées, jamais les prix ni le profit.
CREATE OR REPLACE FUNCTION get_delegate_tasks(p_delegation_id UUID)
RETURNS JSON
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_owner_id UUID;
  v_status_key TEXT;
  v_result JSON;
BEGIN
  SELECT owner_id, task_status_key INTO v_owner_id, v_status_key FROM delegations
    WHERE id = p_delegation_id AND delegate_id = auth.uid() AND active = TRUE;
  IF v_owner_id IS NULL THEN
    RETURN NULL;
  END IF;

  SELECT COALESCE(json_agg(a), '[]'::json) INTO v_result FROM (
    SELECT id, name, photo_url, status, location
    FROM articles
    WHERE user_id = v_owner_id AND status = v_status_key
    ORDER BY created_at ASC
  ) a;
  RETURN v_result;
END;
$$;
GRANT EXECUTE ON FUNCTION get_delegate_tasks(UUID) TO authenticated;

-- Marque un article comme traité par le délégué : fait passer le statut à
-- next_status_key (jamais une valeur fournie par l'appelant — sinon un
-- délégué malveillant pourrait mettre n'importe quel statut, y compris
-- "vendu") et enregistre la tâche rémunérée si compensation à l'article.
CREATE OR REPLACE FUNCTION complete_delegate_task(p_delegation_id UUID, p_article_id UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_owner_id UUID;
  v_amount NUMERIC;
  v_comp_type TEXT;
  v_task_status TEXT;
  v_next_status TEXT;
BEGIN
  SELECT owner_id, compensation_type, task_status_key, next_status_key
    INTO v_owner_id, v_comp_type, v_task_status, v_next_status
    FROM delegations
    WHERE id = p_delegation_id AND delegate_id = auth.uid() AND active = TRUE;
  IF v_owner_id IS NULL THEN
    RETURN FALSE;
  END IF;

  UPDATE articles SET status = v_next_status
    WHERE id = p_article_id AND user_id = v_owner_id AND status = v_task_status;
  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;

  IF v_comp_type = 'per_item' THEN
    SELECT COALESCE(SUM(amount), 0) INTO v_amount FROM delegation_rate_lines WHERE delegation_id = p_delegation_id;
    INSERT INTO delegation_tasks (delegation_id, article_id, amount) VALUES (p_delegation_id, p_article_id, v_amount);
  END IF;

  RETURN TRUE;
END;
$$;
GRANT EXECUTE ON FUNCTION complete_delegate_task(UUID, UUID) TO authenticated;
