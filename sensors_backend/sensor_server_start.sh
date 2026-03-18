#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

if [[ -f ".env" ]]; then
  # Export settings for this script (python also reads .env itself)
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

: "${VENV_DIR:=.venv}"
: "${SERVER_SCRIPT:=sensor_tcp_ingest.py}"
: "${PID_FILE:=sensor_tcp_ingest.pid}"
: "${LOG_FILE:=sensor_tcp_ingest.log}"

py_bin="${VENV_DIR}/bin/python"
pip_bin="${VENV_DIR}/bin/pip"

ensure_venv() {
  if [[ ! -x "$py_bin" ]]; then
    python3 -m venv "$VENV_DIR"
  fi
}

deps_met() {
  "$py_bin" -c "import psycopg2" >/dev/null 2>&1
}

install_deps_if_needed() {
  if deps_met; then
    return 0
  fi
  "$pip_bin" install --upgrade pip >/dev/null
  "$pip_bin" install -r requirements.txt
}

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
  fi
  if pgrep -f "python.*${SERVER_SCRIPT}" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

start_server() {
  nohup "$py_bin" "$SERVER_SCRIPT" >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
}

ensure_venv
install_deps_if_needed

if is_running; then
  echo "Server already running. Nothing to do."
  exit 0
fi

start_server
echo "Server started (pid $(cat "$PID_FILE")). Logs: $LOG_FILE"

