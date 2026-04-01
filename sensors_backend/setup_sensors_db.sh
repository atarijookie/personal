#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

if [[ -f ".env" ]]; then
  # Load app settings (created role/db) from .env
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

# Admin connection used for all psql commands (always postgres/postgres).
# App role/db to create is taken from .env below.
ADMIN_PGHOST="${PGHOST:-localhost}"
ADMIN_PGPORT="${PGPORT:-5432}"
ADMIN_PGUSER="postgres"
ADMIN_PGDATABASE="postgres"
ADMIN_PGPASSWORD="postgres"

# App role/db (from .env). Defaults match earlier examples.
ROLE_NAME="${PGUSER:-sensors}"
ROLE_PASS="${PGPASSWORD:-srosnes}"
DB_NAME="${PGDATABASE:-sensors}"

psql_maint() {
  PGPASSWORD="$ADMIN_PGPASSWORD" psql -v ON_ERROR_STOP=1 -X -q \
    -h "$ADMIN_PGHOST" -p "$ADMIN_PGPORT" -U "$ADMIN_PGUSER" -d "$ADMIN_PGDATABASE" \
    "$@"
}

psql_db() {
  PGPASSWORD="$ADMIN_PGPASSWORD" psql -v ON_ERROR_STOP=1 -X -q \
    -h "$ADMIN_PGHOST" -p "$ADMIN_PGPORT" -U "$ADMIN_PGUSER" -d "$DB_NAME" \
    "$@"
}

# 1) Check/create role, ensure password, grant "all privileges" (superuser-like is not granted)
psql_maint <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${ROLE_NAME}') THEN
    CREATE ROLE ${ROLE_NAME} LOGIN PASSWORD '${ROLE_PASS}';
  ELSE
    -- Keep it idempotent: ensure password is as requested
    ALTER ROLE ${ROLE_NAME} WITH LOGIN PASSWORD '${ROLE_PASS}';
  END IF;
END
\$\$;

-- These are safe to re-run
ALTER ROLE ${ROLE_NAME} CREATEDB;
SQL

# 2) Check/create database (owned by sensors)
psql_maint <<SQL
SELECT format('CREATE DATABASE %I OWNER %I', '${DB_NAME}', '${ROLE_NAME}')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}')
\gexec

GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${ROLE_NAME};
SQL

# 3) Create tables if missing
psql_db <<SQL
-- Ensure role can use public schema
GRANT USAGE, CREATE ON SCHEMA public TO ${ROLE_NAME};

CREATE TABLE IF NOT EXISTS temps_raw (
  id         BIGSERIAL PRIMARY KEY,
  datetime   TIMESTAMPTZ NOT NULL DEFAULT now(),
  sensor_id  INTEGER NOT NULL,
  temp       DOUBLE PRECISION,
  humidity   INTEGER,
  battery    DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS temps_aggr (
  id          BIGSERIAL PRIMARY KEY,
  day         DATE NOT NULL,
  sensor_id   INTEGER NOT NULL,
  t_min       DOUBLE PRECISION,
  t_max       DOUBLE PRECISION,
  t_avg       DOUBLE PRECISION,
  h_min       DOUBLE PRECISION,
  h_max       DOUBLE PRECISION,
  h_avg       DOUBLE PRECISION,
  battery     DOUBLE PRECISION,
  temps       TEXT,
  humidities  TEXT
);

-- Backfill/migrate existing schema (idempotent)
ALTER TABLE temps_aggr ADD COLUMN IF NOT EXISTS day DATE;
ALTER TABLE temps_aggr ADD COLUMN IF NOT EXISTS t_min DOUBLE PRECISION;
ALTER TABLE temps_aggr ADD COLUMN IF NOT EXISTS t_max DOUBLE PRECISION;
ALTER TABLE temps_aggr ADD COLUMN IF NOT EXISTS t_avg DOUBLE PRECISION;
ALTER TABLE temps_aggr ADD COLUMN IF NOT EXISTS h_min DOUBLE PRECISION;
ALTER TABLE temps_aggr ADD COLUMN IF NOT EXISTS h_max DOUBLE PRECISION;
ALTER TABLE temps_aggr ADD COLUMN IF NOT EXISTS h_avg DOUBLE PRECISION;
ALTER TABLE temps_aggr ADD COLUMN IF NOT EXISTS battery DOUBLE PRECISION;
ALTER TABLE temps_raw ADD COLUMN IF NOT EXISTS battery DOUBLE PRECISION;
UPDATE temps_aggr SET day = CURRENT_DATE WHERE day IS NULL;
ALTER TABLE temps_aggr ALTER COLUMN day SET NOT NULL;

-- Needed for UPSERT by (day, sensor_id)
CREATE UNIQUE INDEX IF NOT EXISTS temps_aggr_day_sensor_id_uq ON temps_aggr(day, sensor_id);

CREATE TABLE IF NOT EXISTS sensors (
  id    BIGSERIAL PRIMARY KEY,
  name  TEXT
);

-- Grant privileges (idempotent)
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO ${ROLE_NAME};
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO ${ROLE_NAME};

-- Ensure future tables/sequences get privileges too
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES TO ${ROLE_NAME};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO ${ROLE_NAME};
SQL

echo "Done. Role '${ROLE_NAME}', database '${DB_NAME}', and tables are ready."

