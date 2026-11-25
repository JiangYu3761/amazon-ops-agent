#!/usr/bin/env sh
set -eu

# A single psql transaction holds the same lock as API startup, including the version checks.
# Reading scripts in the current checkout also supports upgrades from a previous API image.
program=$(mktemp)
trap 'rm -f "$program"' EXIT
{
  cat <<'SQL'
BEGIN;
SELECT pg_advisory_xact_lock(487021012);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version)
SELECT version FROM (VALUES ('001_schema.sql'),('002_seed.sql')) AS baseline(version)
WHERE to_regclass('public.products') IS NOT NULL AND NOT EXISTS (SELECT 1 FROM schema_migrations)
ON CONFLICT DO NOTHING;
SQL
  for migration in db/[0-9][0-9][0-9]_*.sql; do
    version=$(basename "$migration")
    printf "SELECT NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version='%s') AS needed \\\\gset\n" "$version"
    printf '%s\n' '\if :needed'
    cat "$migration"
    printf "\nINSERT INTO schema_migrations(version) VALUES ('%s') ON CONFLICT DO NOTHING;\n" "$version"
    printf '%s\n' '\endif'
  done
  printf '%s\n' 'COMMIT;'
} > "$program"
docker compose exec -T db psql -X -f - -v ON_ERROR_STOP=1 \
    -U "${POSTGRES_USER:-amazon_agent}" -d "${POSTGRES_DB:-amazon_agent}" < "$program"
