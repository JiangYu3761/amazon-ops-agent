CREATE TABLE IF NOT EXISTS erp_fbm_orders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id uuid NOT NULL REFERENCES erp_receiving_scopes(id),
    number text NOT NULL, origin text NOT NULL CHECK(origin IN ('platform','manual')),
    paid boolean NOT NULL, source_ref text NOT NULL,
    created_by uuid NOT NULL REFERENCES app_users(id), created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(scope_id,number)
);
CREATE TABLE IF NOT EXISTS erp_fbm_lines (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), order_id uuid NOT NULL REFERENCES erp_fbm_orders(id),
    line_ref text NOT NULL, sku text NOT NULL, qty integer NOT NULL CHECK(qty>0),
    UNIQUE(order_id,line_ref), UNIQUE(order_id,id)
);
CREATE TABLE IF NOT EXISTS erp_fbm_packages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), order_id uuid NOT NULL REFERENCES erp_fbm_orders(id),
    package_ref text NOT NULL, status text NOT NULL DEFAULT 'review' CHECK(status IN
    ('review','released','shipped','hold','intercepting','stop_failed','after_dispatch','cancelled','closed_shipped')),
    version integer NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(order_id,package_ref), UNIQUE(order_id,id)
);
CREATE TABLE IF NOT EXISTS erp_fbm_package_lines (
    order_id uuid NOT NULL, package_id uuid NOT NULL, line_id uuid NOT NULL, qty integer NOT NULL CHECK(qty>0),
    FOREIGN KEY(order_id,package_id) REFERENCES erp_fbm_packages(order_id,id),
    FOREIGN KEY(order_id,line_id) REFERENCES erp_fbm_lines(order_id,id), PRIMARY KEY(package_id,line_id)
);
CREATE TABLE IF NOT EXISTS erp_fbm_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, order_id uuid NOT NULL REFERENCES erp_fbm_orders(id),
    package_id uuid, event text NOT NULL, evidence text NOT NULL, actor_id uuid NOT NULL REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(order_id,package_id) REFERENCES erp_fbm_packages(order_id,id)
);
CREATE TABLE IF NOT EXISTS erp_fbm_requests (
    actor_id uuid NOT NULL REFERENCES app_users(id), request_id uuid NOT NULL, digest text NOT NULL,
    order_id uuid NOT NULL REFERENCES erp_fbm_orders(id), PRIMARY KEY(actor_id,request_id)
);
CREATE INDEX IF NOT EXISTS erp_fbm_scope_date ON erp_fbm_orders(scope_id,created_at DESC);
CREATE INDEX IF NOT EXISTS erp_fbm_events_order ON erp_fbm_events(order_id,id);
INSERT INTO schema_migrations(version) VALUES ('014_fulfillment.sql') ON CONFLICT DO NOTHING;
