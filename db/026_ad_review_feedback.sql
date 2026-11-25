-- Frozen plan JSON retains the decision-time scope, policy and prior signed evidence.
CREATE INDEX IF NOT EXISTS ads_review_feedback_identity
    ON ads_review_plans ((plan->'feedback'->'identity'))
    WHERE plan->'feedback'->'identity' IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ads_review_one_pilot_per_signature
    ON ads_review_plans ((plan->'feedback'->>'candidate_version'))
    WHERE plan->'feedback'->>'applied'='true';
INSERT INTO schema_migrations(version) VALUES ('026_ad_review_feedback.sql') ON CONFLICT DO NOTHING;
