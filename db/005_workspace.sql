ALTER TABLE products ADD COLUMN IF NOT EXISTS data_source text NOT NULL DEFAULT 'manual';
UPDATE products SET data_source='sample' WHERE id='20000000-0000-0000-0000-000000000001';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS module_code text NOT NULL DEFAULT 'review';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS phase text NOT NULL DEFAULT '上线前';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS department text;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS sop_key text;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS task_revision integer NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX IF NOT EXISTS tasks_workflow_sop_key_idx ON tasks(product_workflow_id,sop_key) WHERE sop_key IS NOT NULL;
UPDATE tasks SET module_code=CASE stage_name WHEN '产品与合规' THEN 'compliance' WHEN '供应链' THEN 'inventory' WHEN '库存' THEN 'inventory' WHEN '成本与定价' THEN 'finance' WHEN 'Listing' THEN 'listing' WHEN '素材' THEN 'listing' WHEN '广告' THEN 'ads' WHEN '上线验收' THEN 'calendar' ELSE 'review' END;
UPDATE tasks SET phase='上线后' WHERE stage_name='上线后60天';
CREATE TABLE IF NOT EXISTS operating_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 title text NOT NULL,
 event_type text NOT NULL CHECK(event_type IN ('launch','promotion','shipment','review','deadline')),
 start_date date NOT NULL,
 end_date date,
 owner_id uuid REFERENCES app_users(id),
 status text NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','in_progress','completed','cancelled')),
 notes text,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK(end_date IS NULL OR end_date>=start_date)
);
CREATE OR REPLACE VIEW product_progress AS
SELECT p.id AS product_id,
 COALESCE(ROUND(100 * SUM(CASE WHEN t.status='completed' THEN t.weight ELSE 0 END)/NULLIF(SUM(CASE WHEN t.status<>'not_applicable' THEN t.weight ELSE 0 END),0),1),0) AS completion_percent,
 COUNT(*) FILTER (WHERE t.due_date<CURRENT_DATE AND t.status NOT IN ('completed','not_applicable')) AS overdue_count,
 COUNT(*) FILTER (WHERE t.status='blocked') AS blocked_count
FROM products p LEFT JOIN product_workflows pw ON pw.product_id=p.id LEFT JOIN tasks t ON t.product_workflow_id=pw.id GROUP BY p.id;
INSERT INTO schema_migrations(version) VALUES ('005_workspace.sql') ON CONFLICT DO NOTHING;
