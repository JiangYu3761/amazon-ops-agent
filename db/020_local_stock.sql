-- One transfer record defines both signs; it cannot exist without its matching posting.
CREATE UNIQUE INDEX IF NOT EXISTS erp_stock_scope_identity ON erp_receiving_scopes(id,account_ref,site);
CREATE TABLE IF NOT EXISTS erp_stock_transfers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    from_scope_id uuid NOT NULL,
    to_scope_id uuid NOT NULL,
    account_ref text NOT NULL, site text NOT NULL,
    sku text NOT NULL, qty integer NOT NULL CHECK(qty > 0),
    reference text NOT NULL, evidence text NOT NULL,
    actor_id uuid NOT NULL REFERENCES app_users(id), created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(from_scope_id,account_ref,site) REFERENCES erp_receiving_scopes(id,account_ref,site),
    FOREIGN KEY(to_scope_id,account_ref,site) REFERENCES erp_receiving_scopes(id,account_ref,site),
    CHECK(from_scope_id <> to_scope_id), UNIQUE(account_ref,site,reference)
);
CREATE INDEX IF NOT EXISTS erp_stock_from ON erp_stock_transfers(from_scope_id,sku,created_at DESC);
CREATE INDEX IF NOT EXISTS erp_stock_to ON erp_stock_transfers(to_scope_id,sku,created_at DESC);
CREATE TABLE IF NOT EXISTS erp_stock_requests (
    actor_id uuid NOT NULL REFERENCES app_users(id), request_id uuid NOT NULL,
    transfer_id uuid NOT NULL REFERENCES erp_stock_transfers(id), PRIMARY KEY(actor_id,request_id)
);
CREATE INDEX IF NOT EXISTS erp_stock_receiving_line ON erp_receiving_ledger(line_id);
CREATE OR REPLACE VIEW erp_stock_postings AS
    SELECT o.scope_id,l.sku,g.pending_delta,g.good_delta,g.bad_delta,NULL::uuid AS transfer_id
    FROM erp_receiving_ledger g JOIN erp_receiving_order_lines l ON l.id=g.line_id
    JOIN erp_receiving_orders o ON o.id=l.order_id
    UNION ALL SELECT from_scope_id,sku,0,-qty,0,id FROM erp_stock_transfers
    UNION ALL SELECT to_scope_id,sku,0,qty,0,id FROM erp_stock_transfers;
INSERT INTO schema_migrations(version) VALUES ('020_local_stock.sql') ON CONFLICT DO NOTHING;
