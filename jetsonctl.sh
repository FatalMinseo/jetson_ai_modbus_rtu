#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICES=(
  jetson-ai-modbus-rtu-modbusd.service
  jetson-ai-modbus-rtu-inferd.service
  jetson-ai-modbus-rtu-watchdog.service
)

usage() {
  cat <<'EOF'
Usage: ./jetsonctl.sh COMMAND

Commands:
  start           Start Modbus, inference, and watchdog services
  stop            Stop watchdog, inference, and Modbus services
  restart         Restart all runtime services
  status          Show all runtime service states
  logs [TARGET]   Follow logs: all, modbus, infer, or watchdog
  ui              Open the local configuration window
EOF
}

require_installed() {
  local load_state
  load_state="$(systemctl show jetson-ai-modbus-rtu-inferd.service \
    --property=LoadState --value 2>/dev/null || true)"
  if [[ "$load_state" != "loaded" ]]; then
    echo "Services are not installed. Run ./deploy.sh first." >&2
    exit 1
  fi
}

open_local_ui() {
  local python_bin ui_url run_dir log_dir user_id web_pid ready existing_view_pid stale_web_pids
  python_bin="$APP_DIR/.venv/bin/python"
  if [[ ! -x "$python_bin" ]]; then
    python_bin="${PYTHON_BIN_FALLBACK:-python3}"
  fi
  ui_url="http://127.0.0.1:${PORT:-5022}"
  run_dir="$APP_DIR/run"
  log_dir="$APP_DIR/logs"
  mkdir -p "$run_dir" "$log_dir"

  user_id="$(id -u)"
  if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/$user_id" ]]; then
    export XDG_RUNTIME_DIR="/run/user/$user_id"
  fi
  if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "/run/user/$user_id/bus" ]]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$user_id/bus"
  fi
  if [[ -z "${DISPLAY:-}" && -S /tmp/.X11-unix/X0 ]]; then
    export DISPLAY=:0
  fi
  if [[ -z "${XAUTHORITY:-}" && -r "/run/user/$user_id/gdm/Xauthority" ]]; then
    export XAUTHORITY="/run/user/$user_id/gdm/Xauthority"
  fi

  exec 9>"$run_dir/local-ui.lock"
  if ! flock -n 9; then
    existing_view_pid="$(pgrep -u "$user_id" -f "${APP_DIR}/webview\\.py$" | head -n 1 || true)"
    if [[ -n "$existing_view_pid" ]]; then
      kill -USR1 "$existing_view_pid" 2>/dev/null || true
      command -v notify-send >/dev/null 2>&1 \
        && notify-send "Jetson AI Modbus RTU" "기존 화면을 앞으로 가져왔습니다."
    else
      command -v notify-send >/dev/null 2>&1 \
        && notify-send "Jetson AI Modbus RTU" "화면이 시작 중입니다."
    fi
    return 0
  fi

  # A proxy left behind by an interrupted desktop session must not outlive the UI.
  # The single-instance lock guarantees this cannot target the active window.
  stale_web_pids="$(pgrep -u "$user_id" -f "${APP_DIR}/web_ui\\.py$" || true)"
  if [[ -n "$stale_web_pids" ]]; then
    kill $stale_web_pids 2>/dev/null || true
    for _attempt in $(seq 1 20); do
      pgrep -u "$user_id" -f "${APP_DIR}/web_ui\\.py$" >/dev/null || break
      sleep 0.1
    done
  fi

  web_pid=""
  cleanup_ui() {
    if [[ -n "$web_pid" ]] && kill -0 "$web_pid" 2>/dev/null; then
      kill "$web_pid" 2>/dev/null || true
      wait "$web_pid" 2>/dev/null || true
    fi
  }
  trap cleanup_ui EXIT INT TERM

  HOST=127.0.0.1 PORT="${PORT:-5022}" \
    "$python_bin" "$APP_DIR/web_ui.py" 9>&- \
    >>"$log_dir/local-ui-web.log" 2>&1 &
  web_pid="$!"
  ready=0
  for _attempt in $(seq 1 40); do
    if ! kill -0 "$web_pid" 2>/dev/null; then
      break
    fi
    if curl -fsS --max-time 1 "$ui_url/__ui/health" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 0.25
  done
  if [[ "$ready" != "1" ]]; then
    command -v notify-send >/dev/null 2>&1 \
      && notify-send -u critical "Jetson AI Modbus RTU" "로컬 화면 서버를 시작하지 못했습니다. 로그: logs/local-ui-web.log"
    return 1
  fi

  "$python_bin" "$APP_DIR/webview.py" 9>&- \
    >>"$log_dir/local-ui-webview.log" 2>&1
}

command_name="${1:-status}"
case "$command_name" in
  start)
    require_installed
    sudo systemctl start "${SERVICES[@]}"
    ;;
  stop)
    require_installed
    sudo systemctl stop \
      jetson-ai-modbus-rtu-watchdog.service \
      jetson-ai-modbus-rtu-inferd.service \
      jetson-ai-modbus-rtu-modbusd.service
    ;;
  restart)
    require_installed
    sudo systemctl restart jetson-ai-modbus-rtu-modbusd.service
    sudo systemctl restart \
      jetson-ai-modbus-rtu-inferd.service \
      jetson-ai-modbus-rtu-watchdog.service
    ;;
  status)
    require_installed
    systemctl --no-pager --full status "${SERVICES[@]}"
    ;;
  logs)
    require_installed
    case "${2:-all}" in
      all)      journalctl -f \
                  -u jetson-ai-modbus-rtu-modbusd.service \
                  -u jetson-ai-modbus-rtu-inferd.service \
                  -u jetson-ai-modbus-rtu-watchdog.service ;;
      modbus)   journalctl -f -u jetson-ai-modbus-rtu-modbusd.service ;;
      infer)    journalctl -f -u jetson-ai-modbus-rtu-inferd.service ;;
      watchdog) journalctl -f -u jetson-ai-modbus-rtu-watchdog.service ;;
      *) echo "Unknown log target: ${2:-}" >&2; usage; exit 2 ;;
    esac
    ;;
  ui)
    require_installed
    open_local_ui
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown command: $command_name" >&2
    usage
    exit 2
    ;;
esac
