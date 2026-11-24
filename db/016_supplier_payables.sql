-- Manually confirmed supplier liabilities attached to posted local warehouse receipts.
CREATE TABLE IF NOT EXISTS erp_payable_bills (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id uuid NOT NULL REFERENCES erp_receiving_scopes(id),
    supplier text NOT NULL, document_ref text NOT NULL, currency text NOT NULL CHECK(currency ~ '^[A-Z]{3}$'),
    amount numeric(18,4) NOT NULL CHECK(amount>0), note text NOT NULL DEFAULT '',
    created_by uuid NOT NULL REFERENCES app_users(id), created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    void_by uuid REFERENCES app_users(id), void_reason text, void_at timestamptz,
    CHECK(num_nonnulls(void_by,void_reason,void_at) IN (0,3)), UNIQUE(id,scope_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS erp_payable_document ON erp_payable_bills(scope_id,supplier,document_ref) WHERE void_at IS NULL;
CREATE INDEX IF NOT EXISTS erp_payable_scope ON erp_payable_bills(scope_id,created_at DESC);
CREATE TABLE IF NOT EXISTS erp_payable_sources (
    bill_id uuid NOT NULL REFERENCES erp_payable_bills(id), event_id uuid NOT NULL, line_id uuid NOT NULL,
    active boolean NOT NULL DEFAULT true, PRIMARY KEY(bill_id,event_id,line_id),
    FOREIGN KEY(event_id,line_id) REFERENCES erp_receiving_ledger(event_id,line_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS erp_payable_active_receipt ON erp_payable_sources(event_id,line_id) WHERE active;
CREATE TABLE IF NOT EXISTS erp_payable_payments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), bill_id uuid NOT NULL, scope_id uuid NOT NULL,
    external_ref text NOT NULL, amount numeric(18,4) NOT NULL CHECK(amount>0), paid_on date NOT NULL,
    note text NOT NULL DEFAULT '', actor_id uuid NOT NULL REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(scope_id,external_ref), FOREIGN KEY(bill_id,scope_id) REFERENCES erp_payable_bills(id,scope_id)
);
CREATE INDEX IF NOT EXISTS erp_payable_payment_bill ON erp_payable_payments(bill_id);
CREATE TABLE IF NOT EXISTS erp_payable_requests (
    actor_id uuid NOT NULL REFERENCES app_users(id), request_id uuid NOT NULL, digest text NOT NULL,
    bill_id uuid NOT NULL REFERENCES erp_payable_bills(id), PRIMARY KEY(actor_id,request_id)
);
INSERT INTO schema_migrations(version) VALUES ('016_supplier_payables.sql') ON CONFLICT DO NOTHING;
