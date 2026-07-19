-- Lien de partage en lecture seule pour un comptable : un token aléatoire,
-- révocable, qui donne accès (via une fonction RPC restreinte, pas un accès
-- direct aux tables) aux seules données de vente/dépenses nécessaires à la
-- compta — jamais l'email, l'id utilisateur ou les infos de connexion Vinted.

CREATE TABLE IF NOT EXISTS accountant_links (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  token UUID DEFAULT gen_random_uuid() NOT NULL UNIQUE,
  revoked BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE accountant_links ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users manage own accountant links" ON accountant_links
  FOR ALL USING (auth.uid() = user_id);

-- SECURITY DEFINER : contourne volontairement les RLS des tables articles/
-- expenses, mais seulement pour retourner les colonnes nécessaires à la
-- compta d'un token valide et non révoqué — jamais un accès table libre.
CREATE OR REPLACE FUNCTION get_accountant_data(p_token UUID)
RETURNS JSON
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_user_id UUID;
  v_result JSON;
BEGIN
  SELECT user_id INTO v_user_id FROM accountant_links WHERE token = p_token AND revoked = FALSE;
  IF v_user_id IS NULL THEN
    RETURN NULL;
  END IF;

  SELECT json_build_object(
    'articles', COALESCE((
      SELECT json_agg(a) FROM (
        SELECT name, sell_price, buy_price, extra_costs, sell_date, created_at, vinted_transaction_status
        FROM articles
        WHERE user_id = v_user_id AND status = 'vendu'
      ) a
    ), '[]'::json),
    'expenses', COALESCE((
      SELECT json_agg(e) FROM (
        SELECT label, amount, expense_date FROM expenses WHERE user_id = v_user_id
      ) e
    ), '[]'::json)
  ) INTO v_result;

  RETURN v_result;
END;
$$;

GRANT EXECUTE ON FUNCTION get_accountant_data(UUID) TO anon;
