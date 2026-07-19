-- Factures PDF conformes : profil vendeur (mentions légales) + numérotation
-- séquentielle sans trou (obligation légale), générées côté client (jsPDF)
-- à partir de ces données.

CREATE TABLE IF NOT EXISTS seller_profile (
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE PRIMARY KEY,
  business_name TEXT,
  siret TEXT,
  address TEXT,
  regime TEXT DEFAULT 'micro_vente' CHECK (regime IN ('micro_vente','micro_service','tva_marge')),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE seller_profile ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users manage own seller profile" ON seller_profile
  FOR ALL USING (auth.uid() = user_id);

CREATE TABLE IF NOT EXISTS invoices (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  article_id UUID REFERENCES articles(id) ON DELETE SET NULL,
  invoice_number INT NOT NULL,
  invoice_type TEXT DEFAULT 'facture' CHECK (invoice_type IN ('facture','avoir')),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(user_id, invoice_number)
);
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users manage own invoices" ON invoices
  FOR ALL USING (auth.uid() = user_id);

-- Un article ne doit avoir qu'une seule facture (mais peut avoir en plus un
-- avoir si annulé) — évite de regénérer un numéro différent en re-cliquant.
CREATE UNIQUE INDEX IF NOT EXISTS invoices_article_type_unique
  ON invoices(article_id, invoice_type) WHERE article_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS invoice_counters (
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE PRIMARY KEY,
  next_number INT DEFAULT 1 NOT NULL
);
ALTER TABLE invoice_counters ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users manage own invoice counter" ON invoice_counters
  FOR ALL USING (auth.uid() = user_id);

-- Attribution atomique du prochain numéro : l'UPSERT verrouille la ligne du
-- user le temps de la transaction, donc deux appels concurrents ne peuvent
-- pas recevoir le même numéro (indispensable pour une numérotation légale
-- sans doublon ni trou).
CREATE OR REPLACE FUNCTION next_invoice_number()
RETURNS INT
LANGUAGE plpgsql
AS $$
DECLARE
  v_next INT;
BEGIN
  INSERT INTO invoice_counters (user_id, next_number) VALUES (auth.uid(), 2)
    ON CONFLICT (user_id) DO UPDATE SET next_number = invoice_counters.next_number + 1
    RETURNING next_number - 1 INTO v_next;
  RETURN v_next;
END;
$$;
GRANT EXECUTE ON FUNCTION next_invoice_number() TO authenticated;
