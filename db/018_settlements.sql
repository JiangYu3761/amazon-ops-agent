CREATE TABLE IF NOT EXISTS erp_settlements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_ref text NOT NULL,
    site text NOT NULL,
    currency text NOT NULL CHECK(currency ~ '^[A-Z]{3}$'),
    statement_ref text NOT NULL,
    payout_date date NOT NULL,
    opening_balance numeric(17,4) NOT NULL,
    closing_balance numeric(17,4) NOT NULL,
    actual_payout numeric(17,4) NOT NULL,
    payout_ref text NOT NULL,
    created_by uuid NOT NULL REFERENCES app_users(id),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(account_ref,site,currency,statement_ref)
);
CREATE INDEX IF NOT EXISTS erp_settlement_period ON erp_settlements(payout_date DESC,id DESC);

CREATE TABLE IF NOT EXISTS erp_settlement_imports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    statement_id uuid NOT NULL REFERENCES erp_settlements(id),
    filename text NOT NULL,
    sha256 text NOT NULL,
    added integer NOT NULL DEFAULT 0,
    skipped integer NOT NULL DEFAULT 0,
    actor_id uuid NOT NULL REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(statement_id,sha256)
);

CREATE TABLE IF NOT EXISTS erp_settlement_entries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    statement_id uuid NOT NULL REFERENCES erp_settlements(id),
    account_ref text NOT NULL,
    site text NOT NULL,
    currency text NOT NULL,
    entry_ref text NOT NULL,
    kind text NOT NULL CHECK(kind IN ('sale','fee','refund','adjustment','reversal')),
    occurred_on date NOT NULL,
    original_order_date date,
    amount numeric(17,4) NOT NULL CHECK(amount<>0),
    import_id uuid REFERENCES erp_settlement_imports(id),
    reverses_id uuid UNIQUE REFERENCES erp_settlement_entries(id),
    reason text,
    actor_id uuid NOT NULL REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((kind='reversal' AND reverses_id IS NOT NULL AND import_id IS NULL AND reason IS NOT NULL)
        OR (kind<>'reversal' AND reverses_id IS NULL AND import_id IS NOT NULL)),
    UNIQUE(account_ref,site,currency,entry_ref)
);
CREATE INDEX IF NOT EXISTS erp_settlement_entries_by_statement ON erp_settlement_entries(statement_id,created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS erp_settlement_revisions (
    statement_id uuid NOT NULL REFERENCES erp_settlements(id),
    version integer NOT NULL,
    snapshot jsonb NOT NULL,
    reason text NOT NULL,
    actor_id uuid NOT NULL REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(statement_id,version)
);
INSERT INTO schema_migrations(version) VALUES ('018_settlements.sql') ON CONFLICT DO NOTHING;
