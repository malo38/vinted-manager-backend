-- ============================================================
-- VintControl — Décomposition multi-SKU d'une annonce/vente-lot
-- (une seule annonce/vente Vinted regroupant plusieurs articles
-- physiques distincts, ex: "Lot 4 articles" — signalé le 2026-07-23,
-- inspiré de Vinteer). Jusqu'ici, vinted_links n'autorisait qu'un
-- seul sku par (context, vinted_id) via sa clé primaire — impossible
-- de lier plusieurs pièces à la même annonce/vente Vinted.
-- Copiez-collez dans Supabase SQL Editor
-- ============================================================

ALTER TABLE vinted_links DROP CONSTRAINT IF EXISTS vinted_links_pkey;
ALTER TABLE vinted_links ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
UPDATE vinted_links SET id = gen_random_uuid() WHERE id IS NULL;
ALTER TABLE vinted_links ALTER COLUMN id SET NOT NULL;
ALTER TABLE vinted_links ADD PRIMARY KEY (id);

-- Remplace l'ancienne unicité (context, vinted_id) — un même sku ne peut
-- toujours pas être lié deux fois au même id, mais plusieurs sku différents
-- peuvent désormais l'être (un par pièce du lot).
ALTER TABLE vinted_links ADD CONSTRAINT vinted_links_context_vinted_sku_unique UNIQUE (context, vinted_id, sku);

NOTIFY pgrst, 'reload schema';
