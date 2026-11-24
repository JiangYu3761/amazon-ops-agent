CREATE TABLE IF NOT EXISTS erp_shop_sales_imports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content_sha256 text NOT NULL UNIQUE,
    filename text NOT NULL,
    imported_by uuid NOT NULL REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS erp_shop_sales_rows (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    import_id uuid NOT NULL REFERENCES erp_shop_sales_imports(id),
    source_line integer NOT NULL,
    account_ref text NOT NULL, site text NOT NULL, currency text NOT NULL,
    business_timezone text NOT NULL, business_day date NOT NULL,
    coverage text NOT NULL CHECK(coverage IN ('complete','partial')),
    ordered_revenue numeric(18,4) CHECK(ordered_revenue>=0),
    units_ordered integer CHECK(units_ordered>=0), orders integer CHECK(orders>=0),
    UNIQUE(import_id,source_line),
    CHECK (num_nonnulls(ordered_revenue,units_ordered,orders)>0)
);
CREATE TABLE IF NOT EXISTS erp_shop_sales_days (
    account_ref text NOT NULL, site text NOT NULL, currency text NOT NULL, business_day date NOT NULL,
    row_id uuid NOT NULL UNIQUE REFERENCES erp_shop_sales_rows(id),
    PRIMARY KEY(account_ref,site,currency,business_day)
);
INSERT INTO schema_migrations(version) VALUES ('023_shop_sales.sql') ON CONFLICT DO NOTHING;
