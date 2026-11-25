CREATE TABLE IF NOT EXISTS erp_refund_lines (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id uuid NOT NULL REFERENCES products(id),
    account_ref text NOT NULL,
    site text NOT NULL,
    order_ref text NOT NULL,
    line_ref text NOT NULL,
    sku text NOT NULL,
    currency text NOT NULL CHECK(currency ~ '^[A-Z]{3}$'),
    refundable_amount numeric(18,4) NOT NULL CHECK(refundable_amount > 0),
    marked boolean NOT NULL DEFAULT false,
    version integer NOT NULL DEFAULT 1 CHECK(version > 0),
    created_by uuid NOT NULL REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(account_ref,site,order_ref,line_ref),
    UNIQUE(id,account_ref,site,currency)
);

CREATE TABLE IF NOT EXISTS erp_refund_entries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    line_id uuid NOT NULL,
    account_ref text NOT NULL,
    site text NOT NULL,
    currency text NOT NULL,
    entry_ref text NOT NULL,
    statement_ref text NOT NULL,
    posted_on date NOT NULL,
    amount numeric(18,4) NOT NULL CHECK(amount > 0),
    recorded_by uuid NOT NULL REFERENCES app_users(id),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(line_id,account_ref,site,currency) REFERENCES erp_refund_lines(id,account_ref,site,currency),
    UNIQUE(account_ref,site,entry_ref)
);
CREATE INDEX IF NOT EXISTS erp_refund_entries_by_line ON erp_refund_entries(line_id,posted_on DESC,id DESC);
CREATE INDEX IF NOT EXISTS erp_refund_line_queue ON erp_refund_lines(product_id,created_at DESC,id DESC);

CREATE TABLE IF NOT EXISTS erp_refund_marks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    line_id uuid NOT NULL REFERENCES erp_refund_lines(id),
    marked boolean NOT NULL,
    reason text NOT NULL,
    actor_id uuid NOT NULL REFERENCES app_users(id),
    version integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(line_id,version)
);
INSERT INTO schema_migrations(version) VALUES ('013_refunds.sql') ON CONFLICT DO NOTHING;
