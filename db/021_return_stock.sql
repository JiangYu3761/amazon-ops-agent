-- A local package is posted to one actual warehouse. Its disposition evidence remains immutable.
CREATE TABLE IF NOT EXISTS erp_return_stock_warehouses (
    package_id uuid PRIMARY KEY REFERENCES erp_return_packages(id),
    scope_id uuid NOT NULL REFERENCES erp_receiving_scopes(id),
    UNIQUE(package_id,scope_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS erp_return_event_origin ON erp_return_events(id,package_id,line_id);
CREATE TABLE IF NOT EXISTS erp_return_stock_postings (
    event_id uuid PRIMARY KEY, package_id uuid NOT NULL, line_id uuid NOT NULL,
    scope_id uuid NOT NULL REFERENCES erp_receiving_scopes(id), sku text NOT NULL,
    good integer NOT NULL CHECK(good>=0), defective integer NOT NULL CHECK(defective>=0),
    destroyed integer NOT NULL CHECK(destroyed>=0), CHECK(good+defective>0),
    actor_id uuid NOT NULL REFERENCES app_users(id), posted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(event_id,package_id,line_id) REFERENCES erp_return_events(id,package_id,line_id),
    FOREIGN KEY(package_id,scope_id) REFERENCES erp_return_stock_warehouses(package_id,scope_id)
);
CREATE INDEX IF NOT EXISTS erp_return_stock_by_scope ON erp_return_stock_postings(scope_id,sku);
CREATE TABLE IF NOT EXISTS erp_return_stock_requests (
    actor_id uuid NOT NULL REFERENCES app_users(id), request_id uuid NOT NULL,
    event_id uuid NOT NULL REFERENCES erp_return_stock_postings(event_id), PRIMARY KEY(actor_id,request_id)
);
CREATE OR REPLACE VIEW erp_stock_postings AS
    SELECT o.scope_id,l.sku,g.pending_delta,g.good_delta,g.bad_delta,NULL::uuid AS transfer_id
    FROM erp_receiving_ledger g JOIN erp_receiving_order_lines l ON l.id=g.line_id
    JOIN erp_receiving_orders o ON o.id=l.order_id
    UNION ALL SELECT from_scope_id,sku,0,-qty,0,id FROM erp_stock_transfers
    UNION ALL SELECT to_scope_id,sku,0,qty,0,id FROM erp_stock_transfers
    UNION ALL SELECT scope_id,sku,0,good,defective,NULL::uuid FROM erp_return_stock_postings;
INSERT INTO schema_migrations(version) VALUES ('021_return_stock.sql') ON CONFLICT DO NOTHING;
