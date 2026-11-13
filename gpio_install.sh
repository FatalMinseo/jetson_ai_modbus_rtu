#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DTS_FILE="$APP_DIR/hardware/tegra234-p3768-board29-gpio-output.dts"
DTBO_NAME="jetson-board29-gpio-output.dtbo"
BOOT_DTBO="/boot/$DTBO_NAME"
JETSON_IO_CONFIG="/opt/nvidia/jetson-io/config-by-hardware.py"
OVERLAY_NAME="Jetson BOARD 29 GPIO output"
EXTLINUX_CONFIG="/boot/extlinux/extlinux.conf"
BUILD_DIR=""

log() { printf '[gpio-install] %s\n' "$*"; }
die() { printf '[gpio-install] ERROR: %s\n' "$*" >&2; exit 1; }

cleanup() {
  [[ -z "$BUILD_DIR" ]] || rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

[[ "$(uname -m)" == "aarch64" ]] || die "BOARD 29 overlay is supported only on Jetson aarch64"
[[ -r /etc/nv_tegra_release ]] || die "NVIDIA Jetson release information is missing"
[[ -r /proc/device-tree/compatible ]] || die "Jetson device-tree compatibility information is missing"
tr '\0' '\n' </proc/device-tree/compatible \
  | grep -Eq '^nvidia,p3768-0000\+p3767-0005(-super)?$' \
  || die "unsupported Jetson carrier/module; expected p3768 + p3767-0005"

command -v dtc >/dev/null 2>&1 || die "dtc is missing; install device-tree-compiler first"
command -v sudo >/dev/null 2>&1 || die "sudo is required"
[[ -f "$DTS_FILE" ]] || die "DTS file is missing: $DTS_FILE"
[[ -f "$JETSON_IO_CONFIG" ]] || die "Jetson-IO tool is missing: $JETSON_IO_CONFIG"
[[ -f "$EXTLINUX_CONFIG" ]] || die "extlinux configuration is missing: $EXTLINUX_CONFIG"

BUILD_DIR="$(mktemp -d /tmp/jetson-gpio-overlay.XXXXXX)"
BUILD_DTBO="$BUILD_DIR/$DTBO_NAME"

log "compiling BOARD 29 GPIO output overlay"
dtc -@ -I dts -O dtb -o "$BUILD_DTBO" "$DTS_FILE"
[[ -s "$BUILD_DTBO" ]] || die "dtc did not create a valid overlay"

log "installing $BOOT_DTBO"
sudo install -m 0644 "$BUILD_DTBO" "$BOOT_DTBO"

if sudo grep -Fq "OVERLAYS $BOOT_DTBO" "$EXTLINUX_CONFIG"; then
  log "BOARD 29 overlay is already selected in extlinux"
else
  log "selecting BOARD 29 overlay with Jetson-IO"
  sudo python3 "$JETSON_IO_CONFIG" -n "$OVERLAY_NAME"
fi

sudo grep -F "OVERLAYS $BOOT_DTBO" "$EXTLINUX_CONFIG" >/dev/null \
  || die "Jetson-IO completed without selecting $BOOT_DTBO"

log "installation complete"
printf '\nReboot is required before BOARD 29 can drive the GPIO pulse:\n'
printf '  sudo reboot\n\n'
printf 'After reboot, verify with:\n'
printf '  sudo /opt/nvidia/jetson-io/config-by-pin.py -p 29\n'
printf '  gpioinfo gpiochip0 | grep "PQ.05"\n'
