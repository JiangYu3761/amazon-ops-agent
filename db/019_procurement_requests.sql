CREATE SEQUENCE IF NOT EXISTS erp_procurement_numbers;
CREATE TABLE IF NOT EXISTS erp_procurement_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    number text UNIQUE NOT NULL DEFAULT ('PR-' || nextval('erp_procurement_numbers')::text),
    scope_id uuid NOT NULL REFERENCES erp_receiving_scopes(id), owner_id uuid NOT NULL REFERENCES app_users(id),
    supplier text NOT NULL, reason text NOT NULL, version integer NOT NULL DEFAULT 1 CHECK(version>0),
    status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','pending','rejected','approved','issued')),
    order_id uuid UNIQUE REFERENCES erp_receiving_orders(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK((status='issued')=(order_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS erp_procurement_scope ON erp_procurement_requests(scope_id,updated_at DESC);
CREATE TABLE IF NOT EXISTS erp_procurement_lines (
    request_id uuid NOT NULL REFERENCES erp_procurement_requests(id), position integer NOT NULL,
    sku text NOT NULL, qty integer NOT NULL CHECK(qty>0), PRIMARY KEY(request_id,position), UNIQUE(request_id,sku)
);
CREATE TABLE IF NOT EXISTS erp_procurement_events (
    request_id uuid NOT NULL REFERENCES erp_procurement_requests(id), version integer NOT NULL,
    action text NOT NULL CHECK(action IN ('create','edit','submit','approve','reject','issue')),
    actor_id uuid NOT NULL REFERENCES app_users(id), note text NOT NULL DEFAULT '',
    snapshot jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(request_id,version)
);
CREATE TABLE IF NOT EXISTS erp_procurement_retries (
    actor_id uuid NOT NULL REFERENCES app_users(id), request_key uuid NOT NULL,
    digest text NOT NULL, request_id uuid NOT NULL REFERENCES erp_procurement_requests(id), PRIMARY KEY(actor_id,request_key)
);
INSERT INTO schema_migrations(version) VALUES ('019_procurement_requests.sql') ON CONFLICT DO NOTHING;
