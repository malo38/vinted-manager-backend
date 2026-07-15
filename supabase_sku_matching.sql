-- ============================================================
-- VintControl — SKU stable + table de liaison Vinted + file de
-- réconciliation, pour remplacer le matching fragile par
-- vinted_item_id (qui change selon qu'on regarde une annonce, une
-- commande d'achat ou une commande de vente — cause des doublons
-- stock/vendu et des fiches "Lot N articles" fantômes, signalé le
-- 2026-07-15).
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================

-- 1. Identifiant stable qu'on génère nous-mêmes, indépendant des ID Vinted.
ALTER TABLE articles ADD COLUMN IF NOT EXISTS sku TEXT;
UPDATE articles SET sku = substr(id::text, 1, 8) WHERE sku IS NULL;
ALTER TABLE articles ALTER COLUMN sku SET NOT NULL;
-- Un backfill peut créer des collisions improbables mais possibles (8 premiers
-- caractères d'UUID) : on ne met la contrainte UNIQUE qu'après le backfill,
-- pour voir clairement si Postgres refuse (auquel cas, relancer avec substr(id::text,1,12)).
ALTER TABLE articles ADD CONSTRAINT articles_sku_unique UNIQUE (sku);

-- 2. Table de liaison : quel id Vinted (annonce / commande vente / commande achat)
-- correspond à quel sku. Remplace vinted_item_id comme clé de matching côté sync.
CREATE TABLE IF NOT EXISTS vinted_links (
  sku TEXT REFERENCES articles(sku) ON DELETE CASCADE NOT NULL,
  context TEXT NOT NULL CHECK (context IN ('listing','order_sale','order_purchase')),
  vinted_id TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (context, vinted_id)
);
ALTER TABLE vinted_links ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users manage own links" ON vinted_links FOR ALL
  USING (sku IN (SELECT sku FROM articles WHERE user_id = auth.uid()));

-- Backfill : reconstruit les liens "listing" à partir des vinted_item_id déjà connus,
-- pour que le prochain cycle de sync reconnaisse tout de suite les annonces existantes.
INSERT INTO vinted_links (sku, context, vinted_id)
  SELECT sku, 'listing', vinted_item_id FROM articles
  WHERE vinted_item_id IS NOT NULL AND vinted_item_id != ''
  ON CONFLICT DO NOTHING;

-- 3. File d'attente des ventes détectées par Vinted qu'on n'a pas pu relier
-- avec confiance à un article existant — au lieu de fabriquer une fausse fiche
-- de stock par défaut (ancien comportement, cause des "Lot N articles" fantômes).
CREATE TABLE IF NOT EXISTS unmatched_sales (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  vinted_account_id UUID,
  vinted_order_id TEXT NOT NULL,
  name TEXT,
  sell_price NUMERIC,
  sell_date DATE,
  photo_url TEXT,
  vinted_shipping_status TEXT,
  vinted_transaction_status TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(user_id, vinted_order_id)
);
ALTER TABLE unmatched_sales ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users manage own unmatched sales" ON unmatched_sales
  FOR ALL USING (auth.uid() = user_id);

NOTIFY pgrst, 'reload schema';
