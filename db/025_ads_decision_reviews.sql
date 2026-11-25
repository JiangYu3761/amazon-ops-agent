-- Each approved plan and the evidence of each later decision stay available after corrections.
CREATE TABLE IF NOT EXISTS ads_review_plans (
    source_id uuid PRIMARY KEY REFERENCES workbench_items(id) ON DELETE CASCADE,
    basis text NOT NULL CHECK (basis IN ('prospective','retrospective')),
    plan jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS ads_decision_reviews (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id uuid NOT NULL UNIQUE REFERENCES ads_review_plans(source_id) ON DELETE CASCADE,
    execution jsonb NOT NULL,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    version integer NOT NULL DEFAULT 1 CHECK (version>=1),
    assessment jsonb,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((status='closed') = (assessment IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS ads_review_events (
    review_id uuid NOT NULL REFERENCES ads_decision_reviews(id) ON DELETE CASCADE,
    version integer NOT NULL,
    actor_id uuid NOT NULL REFERENCES app_users(id),
    action text NOT NULL CHECK (action IN ('start','assess','reopen','link')),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(review_id,version)
);
CREATE TABLE IF NOT EXISTS ads_review_links (
    review_id uuid NOT NULL REFERENCES ads_decision_reviews(id) ON DELETE CASCADE,
    item_id uuid NOT NULL REFERENCES workbench_items(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('follow_up','strategy_candidate')),
    PRIMARY KEY(review_id,item_id)
);
CREATE TABLE IF NOT EXISTS ads_review_requests (
    actor_id uuid NOT NULL REFERENCES app_users(id),
    request_id uuid NOT NULL,
    source_id uuid NOT NULL REFERENCES workbench_items(id) ON DELETE CASCADE,
    fingerprint text NOT NULL,
    PRIMARY KEY(actor_id,request_id)
);
INSERT INTO schema_migrations(version) VALUES ('025_ads_decision_reviews.sql') ON CONFLICT DO NOTHING;
