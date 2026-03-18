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

: "${API_SCRIPT:=sensor_api_server.py}"
: "${API_PID_FILE:=sensor_api_server.pid}"
: "${API_LOG_FILE:=sensor_api_server.log}"

py_bin="${VENV_DIR}/bin/python"
pip_bin="${VENV_DIR}/bin/pip"

ensure_venv() {
  if [[ ! -x "$py_bin" ]]; then
    python3 -m venv "$VENV_DIR"
  fi
}

deps_met() {
  "$py_bin" -c "import psycopg2; import flask; import waitress" >/dev/null 2>&1
}

install_deps_if_needed() {
  if deps_met; then
    return 0
  fi
  "$pip_bin" install --upgrade pip >/dev/null
  "$pip_bin" install -r requirements.txt
}

is_running_pidfile() {
  local pid_file="$1"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

is_running_pattern() {
  local pattern="$1"
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

start_server() {
  local script="$1"
  local log_file="$2"
  local pid_file="$3"
  nohup "$py_bin" "$script" >>"$log_file" 2>&1 &
  echo $! >"$pid_file"
}

ensure_venv
install_deps_if_needed

tcp_running=false
api_running=false

if is_running_pidfile "$PID_FILE" || is_running_pattern "python.*${SERVER_SCRIPT}"; then
  tcp_running=true
fi

if is_running_pidfile "$API_PID_FILE" || is_running_pattern "python.*${API_SCRIPT}"; then
  api_running=true
fi

if [[ "$tcp_running" == "true" && "$api_running" == "true" ]]; then
  echo "Servers already running. Nothing to do."
  exit 0
fi

if [[ "$tcp_running" != "true" ]]; then
  start_server "$SERVER_SCRIPT" "$LOG_FILE" "$PID_FILE"
  echo "TCP ingester started (pid $(cat "$PID_FILE")). Logs: $LOG_FILE"
else
  echo "TCP ingester already running. Skipping."
fi

if [[ "$api_running" != "true" ]]; then
  start_server "$API_SCRIPT" "$API_LOG_FILE" "$API_PID_FILE"
  echo "Flask API started (pid $(cat "$API_PID_FILE")). Logs: $API_LOG_FILE"
else
  echo "Flask API already running. Skipping."
fi

