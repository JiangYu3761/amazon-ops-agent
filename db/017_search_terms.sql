CREATE TABLE IF NOT EXISTS erp_search_term_rows (
    product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    business_key text NOT NULL,
    report_date date NOT NULL,
    profile_id text NOT NULL,
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    attribution_days integer NOT NULL CHECK (attribution_days BETWEEN 1 AND 90),
    ad_type text NOT NULL CHECK (ad_type IN ('SP','SB')),
    campaign_id text NOT NULL, campaign_name text NOT NULL, ad_group_id text NOT NULL,
    keyword_id text NOT NULL, match_type text NOT NULL CHECK (match_type IN ('EXACT','PHRASE','BROAD')),
    search_term text NOT NULL,
    impressions integer NOT NULL CHECK (impressions>=0), clicks integer NOT NULL CHECK (clicks>=0),
    spend numeric(14,2) NOT NULL CHECK (spend>=0), attributed_sales numeric(14,2) NOT NULL CHECK (attributed_sales>=0),
    attributed_orders integer NOT NULL CHECK (attributed_orders>=0),
    import_id uuid NOT NULL REFERENCES data_imports(id), source_line integer NOT NULL,
    PRIMARY KEY (product_id,business_key)
);
CREATE INDEX IF NOT EXISTS erp_search_term_range ON erp_search_term_rows(product_id,report_date);
INSERT INTO schema_migrations(version) VALUES ('017_search_terms.sql') ON CONFLICT DO NOTHING;
