-- Additive API ingestion; CSV records, approvals and their history stay intact.
ALTER TABLE products ADD COLUMN IF NOT EXISTS data_account_key text;

CREATE TABLE IF NOT EXISTS operating_connections (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 name text NOT NULL,
 provider text NOT NULL CHECK(provider IN ('sp_sales','sp_inventory','ads_sp')),
 account_key text NOT NULL,
 region text NOT NULL CHECK(region IN ('NA','EU','FE')),
 marketplace text NOT NULL,
 marketplace_id text NOT NULL,
 currency text NOT NULL,
 time_zone text NOT NULL,
 env_prefix text NOT NULL DEFAULT 'AMAZON',
 ads_profile_id text,
 enabled boolean NOT NULL DEFAULT false,
 sync_interval_minutes integer NOT NULL DEFAULT 60 CHECK(sync_interval_minutes BETWEEN 15 AND 1440),
 lookback_days integer NOT NULL DEFAULT 7 CHECK(lookback_days BETWEEN 1 AND 31),
 version integer NOT NULL DEFAULT 1,
 next_sync_at timestamptz,
 last_success_at timestamptz,
 last_error_code text,
 created_by uuid REFERENCES app_users(id),
 updated_by uuid REFERENCES app_users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(provider,account_key,marketplace_id)
);

CREATE TABLE IF NOT EXISTS operating_product_mappings (
 connection_id uuid NOT NULL REFERENCES operating_connections(id),
 product_id uuid NOT NULL REFERENCES products(id),
 sku text NOT NULL,
 asin text,
 PRIMARY KEY(connection_id,sku)
);
CREATE INDEX IF NOT EXISTS operating_mapping_product_idx ON operating_product_mappings(product_id);

CREATE TABLE IF NOT EXISTS operating_sync_jobs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 connection_id uuid NOT NULL REFERENCES operating_connections(id),
 connection_version integer NOT NULL,
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','waiting','retry_wait','succeeded','failed','cancelled')),
 start_date date NOT NULL,
 end_date date NOT NULL,
 checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
 warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
 attempt integer NOT NULL DEFAULT 0,
 next_attempt_at timestamptz NOT NULL DEFAULT now(),
 lease_token uuid,
 lease_expires_at timestamptz,
 error_code text,
 row_count integer NOT NULL DEFAULT 0,
 requested_by uuid REFERENCES app_users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 started_at timestamptz,
 finished_at timestamptz,
 CHECK(start_date<=end_date)
);
CREATE UNIQUE INDEX IF NOT EXISTS operating_sync_one_active_idx ON operating_sync_jobs(connection_id)
 WHERE status IN ('queued','running','waiting','retry_wait');
CREATE INDEX IF NOT EXISTS operating_sync_due_idx ON operating_sync_jobs(next_attempt_at) WHERE status IN ('queued','waiting','retry_wait');

CREATE TABLE IF NOT EXISTS operating_sync_artifacts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 job_id uuid NOT NULL REFERENCES operating_sync_jobs(id),
 connection_id uuid NOT NULL REFERENCES operating_connections(id),
 content_sha256 text NOT NULL,
 normalized_rows jsonb NOT NULL,
 coverage jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(job_id,content_sha256)
);

CREATE TABLE IF NOT EXISTS operating_api_facts (
 connection_id uuid NOT NULL REFERENCES operating_connections(id),
 product_id uuid NOT NULL REFERENCES products(id),
 dataset text NOT NULL CHECK(dataset IN ('sales','advertising','inventory')),
 fact_date date NOT NULL,
 natural_key text NOT NULL,
 payload jsonb NOT NULL,
 source_job_id uuid NOT NULL REFERENCES operating_sync_jobs(id),
 artifact_id uuid NOT NULL REFERENCES operating_sync_artifacts(id),
 fetched_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(connection_id,product_id,dataset,fact_date,natural_key)
);
CREATE INDEX IF NOT EXISTS operating_fact_product_date_idx ON operating_api_facts(product_id,dataset,fact_date);

CREATE TABLE IF NOT EXISTS operating_data_coverage (
 connection_id uuid NOT NULL REFERENCES operating_connections(id),
 product_id uuid NOT NULL REFERENCES products(id),
 dataset text NOT NULL CHECK(dataset IN ('sales','advertising','inventory')),
 fact_date date NOT NULL,
 complete boolean NOT NULL,
 source_job_id uuid NOT NULL REFERENCES operating_sync_jobs(id),
 fetched_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(connection_id,product_id,dataset,fact_date)
);

CREATE TABLE IF NOT EXISTS operating_policies (
 product_id uuid PRIMARY KEY REFERENCES products(id),
 policy jsonb NOT NULL,
 version integer NOT NULL,
 updated_by uuid REFERENCES app_users(id),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS operating_policy_history (
 id bigserial PRIMARY KEY,
 product_id uuid NOT NULL REFERENCES products(id),
 version integer NOT NULL,
 policy jsonb NOT NULL,
 actor_id uuid REFERENCES app_users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(product_id,version)
);
CREATE TABLE IF NOT EXISTS operating_worker_heartbeat (
 worker_id text PRIMARY KEY,
 updated_at timestamptz NOT NULL DEFAULT now(),
 state text NOT NULL
);

CREATE TABLE IF NOT EXISTS operating_csv_artifacts (
 import_id uuid PRIMARY KEY REFERENCES data_imports(id),
 product_id uuid NOT NULL REFERENCES products(id),
 content_sha256 text NOT NULL,
 normalized_rows jsonb NOT NULL,
 previous_rows jsonb NOT NULL,
 actor_id uuid NOT NULL REFERENCES app_users(id),
 created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations(version) VALUES('009_data_automation.sql') ON CONFLICT DO NOTHING;
