CREATE TABLE IF NOT EXISTS workbench_feishu_sync (
    item_id uuid NOT NULL REFERENCES workbench_items(id) ON DELETE CASCADE,
    destination text NOT NULL,
    record_id text,
    confirmed_version integer CHECK (confirmed_version > 0),
    confirmed_at timestamptz,
    last_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_actor uuid NOT NULL REFERENCES app_users(id),
    last_error text,
    PRIMARY KEY (item_id, destination),
    UNIQUE (destination, record_id)
);
INSERT INTO schema_migrations(version) VALUES ('030_feishu_workbench_sync.sql') ON CONFLICT DO NOTHING;
