#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

: "${PGHOST:=localhost}"
: "${PGPORT:=5432}"
: "${PGUSER:=sensors}"
: "${PGPASSWORD:=srosnes}"
: "${PGDATABASE:=sensors}"

timestamp="$(date +"%Y-%m-%d_%H-%M")"
backup_dir="${BACKUP_DIR:-$here/backups}"
mkdir -p "$backup_dir"

outfile="${backup_dir}/${PGDATABASE}_${timestamp}.sql"

PGPASSWORD="$PGPASSWORD" pg_dump \
  -h "$PGHOST" \
  -p "$PGPORT" \
  -U "$PGUSER" \
  -d "$PGDATABASE" \
  --format=plain \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  --file "$outfile"

echo "Backup created: $outfile"
