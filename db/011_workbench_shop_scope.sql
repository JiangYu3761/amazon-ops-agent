CREATE TABLE IF NOT EXISTS workbench_shop_scopes (
    account_ref text NOT NULL,
    site text NOT NULL,
    business_timezone text NOT NULL,
    registered_by uuid NOT NULL REFERENCES app_users(id),
    registered_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_ref,site)
);

ALTER TABLE workbench_items ALTER COLUMN product_id DROP NOT NULL;
ALTER TABLE workbench_items ADD COLUMN IF NOT EXISTS account_ref text;
ALTER TABLE workbench_items ADD COLUMN IF NOT EXISTS business_timezone text;
ALTER TABLE workbench_items ADD COLUMN IF NOT EXISTS connection_id uuid REFERENCES operating_connections(id) ON DELETE SET NULL;
UPDATE workbench_items w SET business_timezone=p.business_timezone
    FROM products p WHERE w.product_id=p.id AND w.business_timezone IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='workbench_items'::regclass
                   AND conname='workbench_item_scope') THEN
        ALTER TABLE workbench_items ADD CONSTRAINT workbench_item_scope CHECK (
            (product_id IS NOT NULL AND account_ref IS NULL AND connection_id IS NULL)
            OR (product_id IS NULL AND page_id='shop' AND account_ref IS NOT NULL
                AND length(trim(account_ref))>0 AND length(trim(site))>0
                AND business_timezone IS NOT NULL AND length(trim(business_timezone))>0 AND task_id IS NULL)
        );
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS workbench_open_shop_signal
    ON workbench_items(account_ref,site,signal_key,page_id,section_id,object_ref)
    WHERE product_id IS NULL AND signal_key IS NOT NULL AND status <> 'closed';
CREATE INDEX IF NOT EXISTS workbench_shop_queue ON workbench_items(account_ref,site,updated_at DESC)
    WHERE product_id IS NULL;
INSERT INTO schema_migrations(version) VALUES ('011_workbench_shop_scope.sql') ON CONFLICT DO NOTHING;
