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
: "${API_PID_FILE:=sensor_api_server.pid}"

stop_by_pidfile() {
  local pid_file="$1"
  local label="$2"

  if [[ ! -f "$pid_file" ]]; then
    echo "$label PID file not found: $pid_file"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -z "${pid}" ]]; then
    echo "$label PID file is empty: $pid_file"
    rm -f "$pid_file"
    return 0
  fi

  if ! kill -0 "$pid" >/dev/null 2>&1; then
    echo "$label process $pid is not running."
    rm -f "$pid_file"
    return 0
  fi

  kill "$pid" >/dev/null 2>&1 || true

  for _ in {1..50}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      echo "Stopped $label process $pid."
      rm -f "$pid_file"
      return 0
    fi
    sleep 0.1
  done

  echo "$label process $pid did not stop gracefully; sending SIGKILL."
  kill -9 "$pid" >/dev/null 2>&1 || true
  rm -f "$pid_file"
  echo "Stopped $label process $pid."
}

stop_by_pidfile "$PID_FILE" "TCP ingester"
stop_by_pidfile "$API_PID_FILE" "Flask API"

