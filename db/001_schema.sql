CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TYPE task_status AS ENUM ('not_started','in_progress','pending_approval','completed','blocked','not_applicable');
CREATE TYPE observation_type AS ENUM ('organic','sponsored','bsr');
CREATE TYPE risk_level AS ENUM ('info','warning','critical');

CREATE TABLE departments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE
);

CREATE TABLE app_users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email text NOT NULL UNIQUE,
  display_name text NOT NULL,
  department_id uuid REFERENCES departments(id),
  role_code text NOT NULL DEFAULT 'executor',
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE products (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model text NOT NULL UNIQUE,
  name text NOT NULL,
  brand text,
  marketplace text NOT NULL,
  asin text,
  sku text,
  owner_id uuid REFERENCES app_users(id),
  target_launch_date date,
  current_stage text,
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workflow_templates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  product_type text NOT NULL,
  version integer NOT NULL DEFAULT 1,
  is_active boolean NOT NULL DEFAULT true
);

CREATE TABLE stage_templates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_template_id uuid NOT NULL REFERENCES workflow_templates(id) ON DELETE CASCADE,
  name text NOT NULL,
  sequence integer NOT NULL,
  weight numeric(6,2) NOT NULL DEFAULT 1,
  UNIQUE(workflow_template_id, sequence)
);

CREATE TABLE task_templates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stage_template_id uuid NOT NULL REFERENCES stage_templates(id) ON DELETE CASCADE,
  title text NOT NULL,
  sequence integer NOT NULL,
  offset_days integer,
  weight numeric(6,2) NOT NULL DEFAULT 1,
  evidence_required boolean NOT NULL DEFAULT false,
  approval_required boolean NOT NULL DEFAULT false,
  default_department text,
  UNIQUE(stage_template_id, sequence)
);

CREATE TABLE product_workflows (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  workflow_template_id uuid NOT NULL REFERENCES workflow_templates(id),
  template_version integer NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(product_id, workflow_template_id)
);

CREATE TABLE tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product_workflow_id uuid NOT NULL REFERENCES product_workflows(id) ON DELETE CASCADE,
  task_template_id uuid REFERENCES task_templates(id),
  stage_name text NOT NULL,
  title text NOT NULL,
  sequence integer NOT NULL,
  status task_status NOT NULL DEFAULT 'not_started',
  weight numeric(6,2) NOT NULL DEFAULT 1,
  owner_id uuid REFERENCES app_users(id),
  due_date date,
  evidence_required boolean NOT NULL DEFAULT false,
  approval_required boolean NOT NULL DEFAULT false,
  completed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE task_dependencies (
  task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  depends_on_task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  PRIMARY KEY(task_id, depends_on_task_id),
  CHECK(task_id <> depends_on_task_id)
);

CREATE TABLE task_evidence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  evidence_type text NOT NULL,
  uri text NOT NULL,
  note text,
  uploaded_by uuid REFERENCES app_users(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE approvals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  approver_id uuid REFERENCES app_users(id),
  decision text NOT NULL DEFAULT 'pending',
  comment text,
  decided_at timestamptz
);

CREATE TABLE tracking_queries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  keyword text NOT NULL,
  marketplace text NOT NULL,
  postal_code text NOT NULL,
  locale text NOT NULL DEFAULT 'en-US',
  device text NOT NULL DEFAULT 'desktop',
  max_pages integer NOT NULL DEFAULT 3,
  runs_per_day integer NOT NULL DEFAULT 4,
  is_active boolean NOT NULL DEFAULT true,
  UNIQUE(product_id, keyword, marketplace, postal_code, device)
);

CREATE TABLE rank_observations (
  id bigserial PRIMARY KEY,
  tracking_query_id uuid REFERENCES tracking_queries(id) ON DELETE CASCADE,
  product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  asin text NOT NULL,
  observation_type observation_type NOT NULL,
  observed_at timestamptz NOT NULL,
  category text,
  rank_value integer NOT NULL,
  page_number integer,
  visible_position integer,
  price numeric(12,2),
  rating numeric(3,2),
  review_count integer,
  badge text,
  screenshot_uri text,
  collector_run_id uuid
);

CREATE INDEX rank_observations_product_time_idx ON rank_observations(product_id, observed_at DESC);
CREATE INDEX rank_observations_query_time_idx ON rank_observations(tracking_query_id, observed_at DESC);

CREATE TABLE agent_findings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  risk risk_level NOT NULL,
  finding_type text NOT NULL,
  title text NOT NULL,
  explanation text NOT NULL,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  proposed_action text,
  status text NOT NULL DEFAULT 'open',
  created_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz
);

CREATE TABLE audit_logs (
  id bigserial PRIMARY KEY,
  actor_id uuid REFERENCES app_users(id),
  entity_type text NOT NULL,
  entity_id text NOT NULL,
  action text NOT NULL,
  before_data jsonb,
  after_data jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE VIEW product_progress AS
SELECT p.id AS product_id,
       COALESCE(ROUND(100 * SUM(CASE WHEN t.status = 'completed' THEN t.weight ELSE 0 END) / NULLIF(SUM(t.weight),0),1),0) AS completion_percent,
       COUNT(*) FILTER (WHERE t.due_date < CURRENT_DATE AND t.status NOT IN ('completed','not_applicable')) AS overdue_count,
       COUNT(*) FILTER (WHERE t.status = 'blocked') AS blocked_count
FROM products p
LEFT JOIN product_workflows pw ON pw.product_id = p.id
LEFT JOIN tasks t ON t.product_workflow_id = pw.id
GROUP BY p.id;

