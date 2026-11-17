-- Authenticated, versioned collaboration over the existing SOP tasks.
CREATE TABLE workflow_accounts (
 user_id uuid PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
 password_hash text NOT NULL,
 is_admin boolean NOT NULL DEFAULT false,
 failed_attempts integer NOT NULL DEFAULT 0,
 locked_until timestamptz
);
CREATE TABLE workflow_sessions (
 token_hash text PRIMARY KEY,
 user_id uuid NOT NULL REFERENCES workflow_accounts(user_id) ON DELETE CASCADE,
 expires_at timestamptz NOT NULL
);
CREATE INDEX workflow_sessions_expiry_idx ON workflow_sessions(expires_at);
ALTER TABLE tasks ADD COLUMN started_at timestamptz;
CREATE TABLE process_configs (
 task_id uuid PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
 version integer NOT NULL DEFAULT 1,
 fields jsonb NOT NULL,
 reviewer_ids uuid[] NOT NULL,
 collaborator_ids uuid[] NOT NULL DEFAULT '{}',
 review_mode text NOT NULL DEFAULT 'all' CHECK(review_mode IN ('all','any','sequential')),
 location text NOT NULL DEFAULT '',
 planned_start date,
 updated_by uuid NOT NULL REFERENCES app_users(id),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE process_states (
 task_id uuid PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
 version integer NOT NULL DEFAULT 1,
 draft jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE process_config_history (
 task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 version integer NOT NULL,
 snapshot jsonb NOT NULL,
 actor_id uuid NOT NULL REFERENCES app_users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(task_id,version)
);
CREATE TABLE process_submissions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 task_revision integer NOT NULL,
 config_version integer NOT NULL,
 snapshot jsonb NOT NULL,
 answers jsonb NOT NULL,
 submitted_by uuid NOT NULL REFERENCES app_users(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX process_submissions_task_idx ON process_submissions(task_id,created_at DESC);
CREATE TABLE process_decisions (
 submission_id uuid NOT NULL REFERENCES process_submissions(id) ON DELETE CASCADE,
 reviewer_id uuid NOT NULL REFERENCES app_users(id),
 decision text NOT NULL CHECK(decision IN ('approved','rejected')),
 comment text NOT NULL DEFAULT '',
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(submission_id,reviewer_id)
);
CREATE TABLE process_attachments (
 id uuid PRIMARY KEY,
 task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 filename text NOT NULL,
 size integer NOT NULL CHECK(size>0 AND size<=10485760),
 sha256 text NOT NULL,
 uploaded_by uuid NOT NULL REFERENCES app_users(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version) VALUES ('007_process_collaboration.sql') ON CONFLICT DO NOTHING;
