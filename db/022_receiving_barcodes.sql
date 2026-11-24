-- Scannable FNSKU references belong to the specific local warehouse scope.
CREATE TABLE IF NOT EXISTS erp_receiving_barcodes (
    scope_id uuid NOT NULL REFERENCES erp_receiving_scopes(id), fnsku text NOT NULL, sku text NOT NULL,
    updated_by uuid NOT NULL REFERENCES app_users(id), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(scope_id,fnsku)
);
CREATE TABLE IF NOT EXISTS erp_receiving_barcode_history (
    id bigserial PRIMARY KEY, scope_id uuid NOT NULL, fnsku text NOT NULL, previous_sku text, sku text NOT NULL,
    actor_id uuid NOT NULL REFERENCES app_users(id), changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(scope_id,fnsku) REFERENCES erp_receiving_barcodes(scope_id,fnsku)
);
CREATE INDEX IF NOT EXISTS erp_receiving_waiting_notices ON erp_receiving_notices(order_id,created_at,id) WHERE status='waiting';
INSERT INTO schema_migrations(version) VALUES ('022_receiving_barcodes.sql') ON CONFLICT DO NOTHING;
