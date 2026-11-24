CREATE TABLE IF NOT EXISTS erp_fee_previews (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entry_id uuid NOT NULL REFERENCES erp_settlement_entries(id),
    actor_id uuid NOT NULL REFERENCES app_users(id),
    statement_version integer NOT NULL,
    base_version integer NOT NULL,
    payout_date date NOT NULL,
    currency text NOT NULL,
    source_amount numeric(17,4) NOT NULL CHECK(source_amount<0),
    basis text NOT NULL CHECK(basis IN ('product','sku')),
    reason text NOT NULL,
    targets jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS erp_fee_allocations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entry_id uuid NOT NULL REFERENCES erp_settlement_entries(id),
    version integer NOT NULL CHECK(version>0),
    action text NOT NULL CHECK(action IN ('post','void')),
    preview_id uuid UNIQUE REFERENCES erp_fee_previews(id),
    reverses_id uuid UNIQUE REFERENCES erp_fee_allocations(id),
    actor_id uuid NOT NULL REFERENCES app_users(id),
    request_id uuid,
    payout_date date NOT NULL,
    currency text NOT NULL,
    expense numeric(17,4) NOT NULL,
    basis text NOT NULL CHECK(basis IN ('product','sku')),
    reason text NOT NULL,
    targets jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((action='post' AND expense>0 AND preview_id IS NOT NULL AND reverses_id IS NULL) OR
           (action='void' AND expense<0 AND preview_id IS NULL AND reverses_id IS NOT NULL)),
    UNIQUE(entry_id,version),
    UNIQUE(entry_id,request_id)
);
INSERT INTO schema_migrations(version) VALUES ('024_fee_allocations.sql') ON CONFLICT DO NOTHING;
