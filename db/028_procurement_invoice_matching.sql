-- A null contract is a genuinely unpriced quantity-only line from the original workflow.
ALTER TABLE erp_procurement_lines
    ADD COLUMN IF NOT EXISTS contract_currency text CHECK (contract_currency ~ '^[A-Z]{3}$'),
    ADD COLUMN IF NOT EXISTS net_unit_price numeric(18,4) CHECK (net_unit_price>0),
    ADD COLUMN IF NOT EXISTS unit_tax numeric(18,4) CHECK (
        unit_tax>=0 AND num_nonnulls(contract_currency,net_unit_price,unit_tax) IN (0,3));
ALTER TABLE erp_receiving_order_lines
    ADD COLUMN IF NOT EXISTS contract_currency text CHECK (contract_currency ~ '^[A-Z]{3}$'),
    ADD COLUMN IF NOT EXISTS net_unit_price numeric(18,4) CHECK (net_unit_price>0),
    ADD COLUMN IF NOT EXISTS unit_tax numeric(18,4) CHECK (
        unit_tax>=0 AND num_nonnulls(contract_currency,net_unit_price,unit_tax) IN (0,3));
ALTER TABLE erp_payable_bills ADD COLUMN IF NOT EXISTS amount_source text NOT NULL DEFAULT 'manual'
    CHECK (amount_source IN ('manual','po_receipt_invoice'));
ALTER TABLE erp_payable_sources ADD COLUMN IF NOT EXISTS invoice jsonb
    CHECK (jsonb_typeof(invoice)='object');
INSERT INTO schema_migrations(version) VALUES ('028_procurement_invoice_matching.sql') ON CONFLICT DO NOTHING;
