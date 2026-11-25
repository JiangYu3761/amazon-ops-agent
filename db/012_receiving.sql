-- Local warehouse stock ledger. Amazon inventory snapshots remain an independent source.
CREATE TABLE IF NOT EXISTS erp_receiving_scopes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_ref text NOT NULL, site text NOT NULL, business_timezone text NOT NULL, warehouse text NOT NULL,
    UNIQUE (account_ref,site,warehouse)
);
CREATE TABLE IF NOT EXISTS erp_receiving_members (
    scope_id uuid NOT NULL REFERENCES erp_receiving_scopes(id), user_id uuid NOT NULL REFERENCES workflow_accounts(user_id),
    PRIMARY KEY (scope_id,user_id)
);
CREATE TABLE IF NOT EXISTS erp_receiving_orders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), scope_id uuid NOT NULL REFERENCES erp_receiving_scopes(id),
    number text NOT NULL, supplier text NOT NULL, created_by uuid NOT NULL REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(scope_id,number)
);
CREATE TABLE IF NOT EXISTS erp_receiving_order_lines (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), order_id uuid NOT NULL REFERENCES erp_receiving_orders(id),
    position integer NOT NULL, sku text NOT NULL, qty integer NOT NULL CHECK(qty>0),
    UNIQUE(order_id,sku), UNIQUE(order_id,id)
);
CREATE SEQUENCE IF NOT EXISTS erp_receiving_notice_numbers;
CREATE TABLE IF NOT EXISTS erp_receiving_notices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), order_id uuid NOT NULL REFERENCES erp_receiving_orders(id),
    number text NOT NULL UNIQUE DEFAULT ('RC-' || nextval('erp_receiving_notice_numbers')::text),
    status text NOT NULL DEFAULT 'waiting' CHECK(status IN ('waiting','received','cancelled')),
    created_by uuid NOT NULL REFERENCES app_users(id), created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(order_id,id)
);
CREATE TABLE IF NOT EXISTS erp_receiving_notice_lines (
    order_id uuid NOT NULL, notice_id uuid NOT NULL, line_id uuid NOT NULL, qty integer NOT NULL CHECK(qty>0),
    PRIMARY KEY(notice_id,line_id),
    FOREIGN KEY(order_id,notice_id) REFERENCES erp_receiving_notices(order_id,id),
    FOREIGN KEY(order_id,line_id) REFERENCES erp_receiving_order_lines(order_id,id)
);
CREATE TABLE IF NOT EXISTS erp_receiving_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), seq bigserial NOT NULL UNIQUE,
    notice_id uuid NOT NULL REFERENCES erp_receiving_notices(id), actor_id uuid NOT NULL REFERENCES app_users(id),
    action text NOT NULL CHECK(action IN ('receive_normal','receive_quick','inspect','waive','cancel','reverse')),
    note text NOT NULL DEFAULT '', created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reverses uuid UNIQUE, UNIQUE(notice_id,id),
    FOREIGN KEY(notice_id,reverses) REFERENCES erp_receiving_events(notice_id,id),
    CHECK((action='reverse')=(reverses IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS erp_receiving_ledger (
    event_id uuid NOT NULL, notice_id uuid NOT NULL, line_id uuid NOT NULL,
    pending_delta integer NOT NULL, good_delta integer NOT NULL, bad_delta integer NOT NULL,
    PRIMARY KEY(event_id,line_id),
    FOREIGN KEY(notice_id,event_id) REFERENCES erp_receiving_events(notice_id,id),
    FOREIGN KEY(notice_id,line_id) REFERENCES erp_receiving_notice_lines(notice_id,line_id),
    CHECK(pending_delta<>0 OR good_delta<>0 OR bad_delta<>0)
);
CREATE INDEX IF NOT EXISTS erp_receiving_ledger_notice ON erp_receiving_ledger(notice_id,line_id);
CREATE INDEX IF NOT EXISTS erp_receiving_orders_scope ON erp_receiving_orders(scope_id,created_at DESC);
CREATE TABLE IF NOT EXISTS erp_receiving_requests (
    actor_id uuid NOT NULL REFERENCES app_users(id), request_id uuid NOT NULL, digest text NOT NULL,
    order_id uuid REFERENCES erp_receiving_orders(id), scope_id uuid REFERENCES erp_receiving_scopes(id),
    CHECK(num_nonnulls(order_id,scope_id)=1), PRIMARY KEY(actor_id,request_id)
);
INSERT INTO schema_migrations(version) VALUES ('012_receiving.sql') ON CONFLICT DO NOTHING;
