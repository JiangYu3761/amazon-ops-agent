-- Workspace members share product records. A workbench event never changes a linked SOP task.
CREATE SEQUENCE IF NOT EXISTS workbench_item_numbers;
CREATE TABLE IF NOT EXISTS workbench_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    number text NOT NULL UNIQUE DEFAULT ('WB-' || nextval('workbench_item_numbers')::text),
    product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    page_id text NOT NULL,
    section_id text NOT NULL,
    site text NOT NULL,
    object_ref text NOT NULL,
    signal_key text,
    source_note text NOT NULL DEFAULT '',
    title text NOT NULL,
    owner_id uuid NOT NULL REFERENCES app_users(id),
    reviewer_id uuid REFERENCES app_users(id),
    task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,
    values jsonb NOT NULL DEFAULT '{}'::jsonb,
    definition jsonb NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','pending_review','approved','executed','closed')),
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (owner_id <> reviewer_id)
);
-- The source is fixed when a follow-up is created. Both cases then proceed independently.
ALTER TABLE workbench_items ADD COLUMN IF NOT EXISTS parent_id uuid
    REFERENCES workbench_items(id) CHECK (parent_id <> id);
CREATE UNIQUE INDEX IF NOT EXISTS workbench_open_followup
    ON workbench_items(parent_id,page_id,section_id,object_ref)
    WHERE parent_id IS NOT NULL AND status <> 'closed';
CREATE INDEX IF NOT EXISTS workbench_followup_history ON workbench_items(parent_id,created_at)
    WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS workbench_product_page ON workbench_items(product_id,page_id,updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS workbench_open_signal ON workbench_items(product_id,signal_key,page_id,section_id,object_ref)
    WHERE signal_key IS NOT NULL AND status <> 'closed';
CREATE TABLE IF NOT EXISTS workbench_events (
    id bigserial PRIMARY KEY,
    item_id uuid NOT NULL REFERENCES workbench_items(id) ON DELETE CASCADE,
    actor_id uuid NOT NULL REFERENCES app_users(id),
    version integer NOT NULL,
    action text NOT NULL,
    note text NOT NULL DEFAULT '',
    evidence text NOT NULL DEFAULT '',
    snapshot jsonb NOT NULL,
    request_id uuid,
    request_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (actor_id, request_id),
    UNIQUE (item_id, version)
);
CREATE TABLE IF NOT EXISTS workbench_request_receipts (
    actor_id uuid NOT NULL REFERENCES app_users(id),
    request_id uuid NOT NULL,
    item_id uuid NOT NULL REFERENCES workbench_items(id) ON DELETE CASCADE,
    request_hash text NOT NULL,
    PRIMARY KEY (actor_id,request_id)
);
INSERT INTO schema_migrations(version) VALUES ('010_workbench.sql') ON CONFLICT DO NOTHING;
