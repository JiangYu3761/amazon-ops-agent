CREATE TABLE IF NOT EXISTS workbench_triage_drafts (
    owner_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    id uuid NOT NULL,
    scope_key text NOT NULL,
    note text NOT NULL CHECK (length(note) BETWEEN 1 AND 6000),
    version integer NOT NULL CHECK (version > 0),
    archived boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, id)
);
CREATE INDEX IF NOT EXISTS workbench_triage_scope_updated
    ON workbench_triage_drafts(owner_id, scope_key, updated_at DESC, id);
CREATE TABLE IF NOT EXISTS workbench_triage_receipts (
    owner_id uuid NOT NULL,
    request_id uuid NOT NULL,
    draft_id uuid NOT NULL,
    fingerprint text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    branched boolean NOT NULL,
    PRIMARY KEY (owner_id, request_id),
    FOREIGN KEY (owner_id, draft_id) REFERENCES workbench_triage_drafts(owner_id, id) ON DELETE CASCADE
);
INSERT INTO schema_migrations(version) VALUES ('029_triage_drafts.sql') ON CONFLICT DO NOTHING;
