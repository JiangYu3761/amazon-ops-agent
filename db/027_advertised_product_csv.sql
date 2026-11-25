-- CSV rows at SP advertised-product grain have stable advertising identities.
-- Legacy name-grain imports remain in ad_performance_daily.
CREATE TABLE IF NOT EXISTS erp_advertised_product_rows (
    product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    business_key text NOT NULL,
    report_date date NOT NULL,
    profile_id text NOT NULL, account_key text NOT NULL, marketplace text NOT NULL,
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    attribution_days integer NOT NULL CHECK (attribution_days BETWEEN 1 AND 90),
    ad_type text NOT NULL CHECK (ad_type='SP'),
    report_grain text NOT NULL CHECK (report_grain='advertised_product'),
    campaign_id text NOT NULL, campaign_name text NOT NULL,
    ad_group_id text NOT NULL, entity_name text NOT NULL, ad_id text NOT NULL,
    sku text NOT NULL, asin text,
    impressions integer NOT NULL CHECK (impressions>=0), clicks integer NOT NULL CHECK (clicks>=0),
    spend numeric(14,2) NOT NULL CHECK (spend>=0), attributed_sales numeric(14,2) NOT NULL CHECK (attributed_sales>=0),
    attributed_orders integer NOT NULL CHECK (attributed_orders>=0),
    import_id uuid NOT NULL REFERENCES data_imports(id) ON DELETE CASCADE, source_line integer NOT NULL,
    PRIMARY KEY(product_id,account_key,marketplace,business_key)
);
CREATE INDEX IF NOT EXISTS erp_advertised_product_range ON erp_advertised_product_rows(product_id,report_date);
CREATE INDEX IF NOT EXISTS erp_advertised_product_csv_receipts ON operating_csv_artifacts(product_id,content_sha256)
    WHERE normalized_rows->0->>'report_grain'='advertised_product';
INSERT INTO schema_migrations(version) VALUES ('027_advertised_product_csv.sql') ON CONFLICT DO NOTHING;
