#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
PYTHON_BIN="${PYTHON_BIN:-$APP_DIR/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="${PYTHON_BIN_FALLBACK:-python3}"
fi

INFER_PORT="${DS_CAMERA_DAEMON_PORT:-5122}"
COMM_MODE="modbus_rtu"

install_desktop_launcher() {
  local user_home desktop_dir launcher escaped_app_dir user_id runtime_dir session_bus
  user_home="$(getent passwd "$USER_NAME" | cut -d: -f6)"
  if [[ -z "$user_home" ]]; then
    echo "Unable to resolve home directory for $USER_NAME" >&2
    return 1
  fi

  desktop_dir=""
  if command -v xdg-user-dir >/dev/null 2>&1; then
    if [[ "$(id -un)" == "$USER_NAME" ]]; then
      desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
    else
      desktop_dir="$(sudo -u "$USER_NAME" xdg-user-dir DESKTOP 2>/dev/null || true)"
    fi
  fi
  if [[ -z "$desktop_dir" || "$desktop_dir" == "$user_home" ]]; then
    desktop_dir="$user_home/Desktop"
  fi

  if [[ "$(id -un)" == "$USER_NAME" ]]; then
    mkdir -p "$desktop_dir"
  else
    sudo -u "$USER_NAME" mkdir -p "$desktop_dir"
  fi
  launcher="$desktop_dir/Jetson-AI-Modbus-RTU.desktop"
  escaped_app_dir="${APP_DIR//|/\|}"
  sed "s|@APP_DIR@|$escaped_app_dir|g" \
    "$APP_DIR/jetson-ai-modbus-rtu.desktop.in" >"$tmp_launcher"
  if [[ "$(id -un)" == "$USER_NAME" ]]; then
    install -m 0755 "$tmp_launcher" "$launcher"
  else
    sudo install -o "$USER_NAME" -g "$(id -gn "$USER_NAME")" \
      -m 0755 "$tmp_launcher" "$launcher"
  fi
  chmod 0755 "$APP_DIR/jetsonctl.sh"

  if command -v gio >/dev/null 2>&1; then
    user_id="$(id -u "$USER_NAME")"
    runtime_dir="/run/user/$user_id"
    session_bus="unix:path=$runtime_dir/bus"
    if [[ "$(id -un)" == "$USER_NAME" ]]; then
      env XDG_RUNTIME_DIR="$runtime_dir" DBUS_SESSION_BUS_ADDRESS="$session_bus" \
        gio set -t string "$launcher" metadata::trusted true >/dev/null 2>&1 || true
    else
      sudo -u "$USER_NAME" env XDG_RUNTIME_DIR="$runtime_dir" \
        DBUS_SESSION_BUS_ADDRESS="$session_bus" \
        gio set -t string "$launcher" metadata::trusted true >/dev/null 2>&1 || true
    fi
  fi

  echo "Desktop launcher installed: $launcher"
}

tmp_infer="$(mktemp)"
tmp_modbus="$(mktemp)"
tmp_watchdog="$(mktemp)"
tmp_reboot="$(mktemp)"
tmp_sudoers="$(mktemp)"
tmp_launcher="$(mktemp)"
cleanup_tmp_files() {
  rm -f "$tmp_infer" "$tmp_modbus" "$tmp_watchdog" \
    "$tmp_reboot" "$tmp_sudoers" "$tmp_launcher"
}
trap cleanup_tmp_files EXIT
SYSTEMCTL_BIN="$(command -v systemctl)"
VISUDO_BIN="$(command -v visudo)"

cat >"$tmp_modbus" <<EOF
[Unit]
Description=jetson_ai_modbus_rtu independent Modbus RTU daemon
[Service]
Type=simple
User=${USER_NAME}
SupplementaryGroups=dialout
WorkingDirectory=${APP_DIR}
Environment=DS_CAMERA_DAEMON_URL=http://127.0.0.1:${INFER_PORT}
Environment=DS_CAMERA_MODBUSD_URL=http://127.0.0.1:5123
Environment="DS_CAMERA_MODBUSD_INFER_RESTART_CMD=sudo -n ${SYSTEMCTL_BIN} restart jetson-ai-modbus-rtu-inferd.service"
Environment="DS_CAMERA_MODBUSD_REBOOT_CMD=sudo -n ${SYSTEMCTL_BIN} start jetson-ai-modbus-rtu-reboot.service"
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYTHON_BIN} ${APP_DIR}/modbus_daemon.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat >"$tmp_infer" <<EOF
[Unit]
Description=jetson_ai_modbus_rtu DeepStream inference daemon
After=jetson-ai-modbus-rtu-modbusd.service
Wants=jetson-ai-modbus-rtu-modbusd.service

[Service]
Type=simple
User=${USER_NAME}
SupplementaryGroups=dialout gpio
WorkingDirectory=${APP_DIR}
Environment=HOST=127.0.0.1
Environment=PORT=${INFER_PORT}
Environment=DS_CAMERA_COMM=${COMM_MODE}
Environment=DS_CAMERA_MODBUS_EXTERNAL=1
Environment=DS_CAMERA_MODBUSD_URL=http://127.0.0.1:5123
Environment=JETSON_MODEL_NAME=JETSON_ORIN_NANO
Environment=DS_CAMERA_GPIO_PULSE_ENABLED=1
Environment=DS_CAMERA_GPIO_BOARD_PIN=29
Environment=DS_CAMERA_GPIO_PULSE_MS=100
Environment=DS_CAMERA_GPIO_ACTIVE_HIGH=1
Environment=DS_CAMERA_UI_PREVIEW_TRANSPORT=mjpeg
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYTHON_BIN} ${APP_DIR}/infer_daemon.py
Restart=always
# Jetson NVMM/VIC buffer pools can be released asynchronously after a crash.
# Give the driver time to return contiguous pages before rebuilding them.
RestartSec=8

[Install]
WantedBy=multi-user.target
EOF

cat >"$tmp_watchdog" <<EOF
[Unit]
Description=jetson_ai_modbus_rtu inference watchdog
After=jetson-ai-modbus-rtu-inferd.service
Wants=jetson-ai-modbus-rtu-inferd.service

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${APP_DIR}
Environment=DS_CAMERA_DAEMON_URL=http://127.0.0.1:${INFER_PORT}
Environment="DS_CAMERA_WATCHDOG_RESTART_CMD=sudo -n ${SYSTEMCTL_BIN} restart jetson-ai-modbus-rtu-inferd.service"
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYTHON_BIN} ${APP_DIR}/watchdog.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat >"$tmp_reboot" <<EOF
[Unit]
Description=jetson_ai_modbus_rtu controlled OS reboot

[Service]
Type=oneshot
ExecStart=${SYSTEMCTL_BIN} --no-block reboot
EOF

cat >"$tmp_sudoers" <<EOF
${USER_NAME} ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} restart jetson-ai-modbus-rtu-inferd.service
${USER_NAME} ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} start jetson-ai-modbus-rtu-reboot.service
EOF

# Validate before installing so an invalid file never enters /etc/sudoers.d.
"$VISUDO_BIN" -cf "$tmp_sudoers" >/dev/null

sudo install -m 0644 "$tmp_modbus" /etc/systemd/system/jetson-ai-modbus-rtu-modbusd.service
sudo install -m 0644 "$tmp_infer" /etc/systemd/system/jetson-ai-modbus-rtu-inferd.service
sudo install -m 0644 "$tmp_watchdog" /etc/systemd/system/jetson-ai-modbus-rtu-watchdog.service
sudo install -m 0644 "$tmp_reboot" /etc/systemd/system/jetson-ai-modbus-rtu-reboot.service
sudo install -m 0440 "$tmp_sudoers" /etc/sudoers.d/jetson-ai-modbus-rtu
# Remove the retired always-on WebUI unit from upgraded devices. The local UI
# now owns a short-lived loopback-only web process while its window is open.
sudo systemctl disable --now jetson-ai-modbus-rtu-web.service >/dev/null 2>&1 || true
if [[ -f /etc/systemd/system/jetson-ai-modbus-rtu-web.service ]]; then
  sudo rm -f /etc/systemd/system/jetson-ai-modbus-rtu-web.service
fi
sudo systemctl daemon-reload
sudo systemctl enable jetson-ai-modbus-rtu-modbusd.service jetson-ai-modbus-rtu-inferd.service jetson-ai-modbus-rtu-watchdog.service
install_desktop_launcher

echo "Installed systemd services and local desktop launcher:"
echo "  jetson-ai-modbus-rtu-modbusd.service independent serial/register owner"
echo "  jetson-ai-modbus-rtu-inferd.service  http://127.0.0.1:${INFER_PORT}"
echo "  communication             ${COMM_MODE}"
echo "  saved inference state     restored by inferd"
echo "  jetson-ai-modbus-rtu-watchdog.service"
echo
echo "Manage:"
echo "  ./jetsonctl.sh start|stop|restart|status"
echo "  ./jetsonctl.sh logs [all|modbus|infer|watchdog]"
echo "  ./jetsonctl.sh ui"
