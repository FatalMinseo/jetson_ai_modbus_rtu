#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_CH341="${INSTALL_CH341:-1}"
CH341_INSTALLER="$APP_DIR/drivers/ch341/install.sh"

if [[ "$INSTALL_CH341" == "1" && ! -f "$CH341_INSTALLER" ]]; then
  echo "[deploy] ERROR: CH341 installer is missing: $CH341_INSTALLER" >&2
  echo "[deploy] Run 'git pull --ff-only origin main' and retry." >&2
  exit 1
fi

"$APP_DIR/setup.sh"
if [[ "$INSTALL_CH341" == "1" ]]; then
  "$CH341_INSTALLER"
fi
"$APP_DIR/install_systemd.sh"
# Stop the old embedded-serial inferd first so only modbusd can own ttyUSB0.
sudo systemctl stop jetson-ai-modbus-rtu-watchdog jetson-ai-modbus-rtu-inferd
sudo systemctl restart jetson-ai-modbus-rtu-modbusd
sudo systemctl start jetson-ai-modbus-rtu-inferd jetson-ai-modbus-rtu-watchdog

echo
echo "jetson_ai_modbus_rtu deployment complete"
echo "  UI:     run ./jetsonctl.sh ui or double-click the desktop icon"
echo "  Status: sudo systemctl status jetson-ai-modbus-rtu-modbusd jetson-ai-modbus-rtu-inferd jetson-ai-modbus-rtu-watchdog"
echo "  Logs:   journalctl -u jetson-ai-modbus-rtu-modbusd -f"
