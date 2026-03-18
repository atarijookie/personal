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

: "${PID_FILE:=sensor_tcp_ingest.pid}"

if [[ ! -f "$PID_FILE" ]]; then
  echo "PID file not found: $PID_FILE"
  exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "${pid}" ]]; then
  echo "PID file is empty: $PID_FILE"
  rm -f "$PID_FILE"
  exit 0
fi

if ! kill -0 "$pid" >/dev/null 2>&1; then
  echo "Process $pid is not running."
  rm -f "$PID_FILE"
  exit 0
fi

kill "$pid" >/dev/null 2>&1 || true

for _ in {1..50}; do
  if ! kill -0 "$pid" >/dev/null 2>&1; then
    echo "Stopped process $pid."
    rm -f "$PID_FILE"
    exit 0
  fi
  sleep 0.1
done

echo "Process $pid did not stop gracefully; sending SIGKILL."
kill -9 "$pid" >/dev/null 2>&1 || true
rm -f "$PID_FILE"
echo "Stopped process $pid."

