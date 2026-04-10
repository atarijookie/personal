#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <backup-file.sql>"
  exit 1
fi

dump_file="$1"
if [[ ! -f "$dump_file" ]]; then
  echo "Backup file not found: $dump_file"
  exit 1
fi

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

# Admin connection for database creation (matches existing setup script defaults)
ADMIN_PGHOST="${PGHOST:-localhost}"
ADMIN_PGPORT="${PGPORT:-5432}"
ADMIN_PGUSER="postgres"
ADMIN_PGDATABASE="postgres"
ADMIN_PGPASSWORD="postgres"

# App database connection (from .env or defaults)
DB_HOST="${PGHOST:-localhost}"
DB_PORT="${PGPORT:-5432}"
DB_USER="${PGUSER:-sensors}"
DB_PASS="${PGPASSWORD:-srosnes}"
DB_NAME="${PGDATABASE:-sensors}"

PGPASSWORD="$ADMIN_PGPASSWORD" psql -v ON_ERROR_STOP=1 -X -q \
  -h "$ADMIN_PGHOST" \
  -p "$ADMIN_PGPORT" \
  -U "$ADMIN_PGUSER" \
  -d "$ADMIN_PGDATABASE" <<SQL
SELECT format('CREATE DATABASE %I', '${DB_NAME}')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}')
\gexec
SQL

PGPASSWORD="$DB_PASS" psql -v ON_ERROR_STOP=1 -X \
  -h "$DB_HOST" \
  -p "$DB_PORT" \
  -U "$DB_USER" \
  -d "$DB_NAME" \
  -f "$dump_file"

echo "Restore completed from: $dump_file"
