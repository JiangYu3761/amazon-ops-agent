ALTER TABLE products ADD COLUMN IF NOT EXISTS product_type text;
ALTER TABLE products ADD COLUMN IF NOT EXISTS lifecycle_stage text NOT NULL DEFAULT 'pre_launch';
ALTER TABLE products ADD COLUMN IF NOT EXISTS currency text NOT NULL DEFAULT 'USD';
ALTER TABLE products ADD COLUMN IF NOT EXISTS business_timezone text NOT NULL DEFAULT 'America/Los_Angeles';
ALTER TABLE products ADD COLUMN IF NOT EXISTS category text;
ALTER TABLE products ADD COLUMN IF NOT EXISTS notes text;

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS description text;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS acceptance_criteria text;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS observation_days integer;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS rollback_condition text;

INSERT INTO stage_templates(id,workflow_template_id,name,sequence,weight) VALUES
('31000000-0000-0000-0000-000000000001','30000000-0000-0000-0000-000000000001','产品与合规',1,5),
('31000000-0000-0000-0000-000000000002','30000000-0000-0000-0000-000000000001','供应链',2,5),
('31000000-0000-0000-0000-000000000003','30000000-0000-0000-0000-000000000001','成本与定价',3,4),
('31000000-0000-0000-0000-000000000004','30000000-0000-0000-0000-000000000001','Listing',4,3),
('31000000-0000-0000-0000-000000000005','30000000-0000-0000-0000-000000000001','素材',5,4),
('31000000-0000-0000-0000-000000000006','30000000-0000-0000-0000-000000000001','库存',6,5),
('31000000-0000-0000-0000-000000000007','30000000-0000-0000-0000-000000000001','广告',7,3),
('31000000-0000-0000-0000-000000000008','30000000-0000-0000-0000-000000000001','上线验收',8,5),
('31000000-0000-0000-0000-000000000009','30000000-0000-0000-0000-000000000001','上线后60天',9,3)
ON CONFLICT DO NOTHING;

INSERT INTO task_templates(id,stage_template_id,title,sequence,offset_days,weight,evidence_required,approval_required,default_department) VALUES
('32000000-0000-0000-0000-000000000001','31000000-0000-0000-0000-000000000001','确认正式产品名称、型号和能力边界',1,-79,3,true,true,'产品'),
('32000000-0000-0000-0000-000000000002','31000000-0000-0000-0000-000000000001','确认FCC、危险品及标签状态',2,-74,5,true,true,'认证'),
('32000000-0000-0000-0000-000000000003','31000000-0000-0000-0000-000000000002','确认生产、头程及入仓周期',3,-81,5,true,false,'供应链'),
('32000000-0000-0000-0000-000000000004','31000000-0000-0000-0000-000000000003','完成SKU成本卡和预算边界',4,-71,4,true,true,'财务'),
('32000000-0000-0000-0000-000000000005','31000000-0000-0000-0000-000000000004','完成标题、五点和关键词库',5,-43,3,true,true,'运营'),
('32000000-0000-0000-0000-000000000006','31000000-0000-0000-0000-000000000005','完成主图、附图、A+和视频',6,-36,4,true,true,'设计'),
('32000000-0000-0000-0000-000000000007','31000000-0000-0000-0000-000000000006','首批货件入仓并达到可售状态',7,-14,5,true,false,'供应链'),
('32000000-0000-0000-0000-000000000008','31000000-0000-0000-0000-000000000007','完成首发广告结构、预算与否词库',8,-7,3,true,true,'广告'),
('32000000-0000-0000-0000-000000000009','31000000-0000-0000-0000-000000000008','完成页面、库存、价格、促销和广告验收',9,-1,5,true,true,'运营'),
('32000000-0000-0000-0000-000000000010','31000000-0000-0000-0000-000000000009','完成上线后首轮搜索词、广告位与竞品复盘',10,7,3,true,false,'运营')
ON CONFLICT DO NOTHING;

CREATE TABLE data_imports (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  import_type text NOT NULL CHECK(import_type IN ('sales','advertising','inventory','costs')),
  filename text NOT NULL,
  status text NOT NULL DEFAULT 'processing',
  row_count integer NOT NULL DEFAULT 0,
  error_count integer NOT NULL DEFAULT 0,
  errors jsonb NOT NULL DEFAULT '[]'::jsonb,
  imported_by uuid REFERENCES app_users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);

CREATE TABLE sales_daily (
  product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  sale_date date NOT NULL,
  sku text NOT NULL,
  asin text,
  units_ordered integer NOT NULL DEFAULT 0,
  ordered_revenue numeric(14,2) NOT NULL DEFAULT 0,
  currency text NOT NULL DEFAULT 'USD',
  import_id uuid NOT NULL REFERENCES data_imports(id) ON DELETE CASCADE,
  PRIMARY KEY(product_id, sale_date, sku)
);

CREATE TABLE ad_performance_daily (
  product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  report_date date NOT NULL,
  campaign_name text NOT NULL,
  entity_name text NOT NULL DEFAULT '',
  impressions integer NOT NULL DEFAULT 0,
  clicks integer NOT NULL DEFAULT 0,
  spend numeric(14,2) NOT NULL DEFAULT 0,
  attributed_sales numeric(14,2) NOT NULL DEFAULT 0,
  attributed_orders integer NOT NULL DEFAULT 0,
  currency text NOT NULL DEFAULT 'USD',
  import_id uuid NOT NULL REFERENCES data_imports(id) ON DELETE CASCADE,
  PRIMARY KEY(product_id, report_date, campaign_name, entity_name)
);

CREATE TABLE inventory_snapshots (
  product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  snapshot_date date NOT NULL,
  sku text NOT NULL,
  asin text,
  fulfillable integer NOT NULL DEFAULT 0,
  reserved integer NOT NULL DEFAULT 0,
  inbound integer NOT NULL DEFAULT 0,
  unsellable integer NOT NULL DEFAULT 0,
  import_id uuid NOT NULL REFERENCES data_imports(id) ON DELETE CASCADE,
  PRIMARY KEY(product_id, snapshot_date, sku)
);

CREATE TABLE cost_records (
  product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  effective_date date NOT NULL,
  sku text NOT NULL,
  product_cost numeric(14,4) NOT NULL DEFAULT 0,
  first_mile_cost numeric(14,4) NOT NULL DEFAULT 0,
  fba_fee numeric(14,4) NOT NULL DEFAULT 0,
  referral_fee numeric(14,4) NOT NULL DEFAULT 0,
  other_cost numeric(14,4) NOT NULL DEFAULT 0,
  currency text NOT NULL DEFAULT 'USD',
  import_id uuid NOT NULL REFERENCES data_imports(id) ON DELETE CASCADE,
  PRIMARY KEY(product_id, effective_date, sku)
);

CREATE INDEX data_imports_product_time_idx ON data_imports(product_id, created_at DESC);
INSERT INTO schema_migrations(version) VALUES ('004_operating_data.sql') ON CONFLICT DO NOTHING;
