-- Local evidence ledger. Receipt and classification records do not update stock or settlement.
CREATE TABLE IF NOT EXISTS erp_return_limits (
    line_id uuid PRIMARY KEY REFERENCES erp_refund_lines(id),
    ordered_units integer NOT NULL CHECK (ordered_units>0),
    order_evidence text NOT NULL,
    created_by uuid NOT NULL REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS erp_return_packages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    line_id uuid NOT NULL REFERENCES erp_return_limits(line_id),
    package_ref text NOT NULL,
    expected_units integer NOT NULL CHECK (expected_units>0),
    channel text NOT NULL CHECK (channel IN ('local','third_party','fba')),
    created_by uuid NOT NULL REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(line_id,package_ref),
    UNIQUE(id,line_id)
);
CREATE TABLE IF NOT EXISTS erp_return_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    package_id uuid NOT NULL,
    line_id uuid NOT NULL,
    kind text NOT NULL CHECK(kind IN ('receive','dispose')),
    event_ref text NOT NULL,
    evidence_ref text NOT NULL,
    evidence_type text NOT NULL CHECK(evidence_type IN ('local_record','warehouse_report','fba_report')),
    occurred_on date NOT NULL,
    received integer NOT NULL DEFAULT 0,
    good integer NOT NULL DEFAULT 0,
    defective integer NOT NULL DEFAULT 0,
    destroyed integer NOT NULL DEFAULT 0,
    recorded_by uuid NOT NULL REFERENCES app_users(id),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(package_id,line_id) REFERENCES erp_return_packages(id,line_id),
    UNIQUE(line_id,kind,event_ref),
    CHECK (received>=0 AND good>=0 AND defective>=0 AND destroyed>=0),
    CHECK ((kind='receive' AND received>0 AND good+defective+destroyed=0)
        OR (kind='dispose' AND received=0 AND good+defective+destroyed>0))
);
CREATE INDEX IF NOT EXISTS erp_return_events_by_package ON erp_return_events(package_id,recorded_at DESC,id DESC);
INSERT INTO schema_migrations(version) VALUES ('015_returns.sql') ON CONFLICT DO NOTHING;
