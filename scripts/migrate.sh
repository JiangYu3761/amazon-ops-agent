#!/usr/bin/env sh
set -eu

compose="docker compose"
$compose exec -T db psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-amazon_agent}" -d "${POSTGRES_DB:-amazon_agent}" <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);
DO $$
BEGIN
  IF to_regclass('public.products') IS NOT NULL AND NOT EXISTS (SELECT 1 FROM schema_migrations) THEN
    INSERT INTO schema_migrations(version) VALUES ('001_schema.sql'),('002_seed.sql') ON CONFLICT DO NOTHING;
  END IF;
END $$;
SQL

for migration in db/*.sql; do
  version=$(basename "$migration")
  applied=$($compose exec -T db psql -At -U "${POSTGRES_USER:-amazon_agent}" -d "${POSTGRES_DB:-amazon_agent}" -c "SELECT 1 FROM schema_migrations WHERE version='$version'")
  if [ "$applied" != "1" ]; then
    $compose exec -T db psql --single-transaction -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-amazon_agent}" -d "${POSTGRES_DB:-amazon_agent}" < "$migration"
    $compose exec -T db psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-amazon_agent}" -d "${POSTGRES_DB:-amazon_agent}" -c "INSERT INTO schema_migrations(version) VALUES ('$version') ON CONFLICT DO NOTHING"
  fi
done

