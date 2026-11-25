ALTER TABLE workbench_items ADD COLUMN IF NOT EXISTS focus_date date;
CREATE INDEX IF NOT EXISTS workbench_items_focus ON workbench_items(focus_date, product_id)
    WHERE focus_date IS NOT NULL;
INSERT INTO schema_migrations(version) VALUES ('031_workbench_focus_date.sql') ON CONFLICT DO NOTHING;
