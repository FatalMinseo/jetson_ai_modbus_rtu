#!/usr/bin/env bash
set -euo pipefail

DRIVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KERNEL_RELEASE="$(uname -r)"
KERNEL_BUILD="/lib/modules/$KERNEL_RELEASE/build"
MODULE_DEST="/lib/modules/$KERNEL_RELEASE/updates/ch341.ko"
BACKUP_DIR="/var/backups/jetson-ch341/$KERNEL_RELEASE"
DISABLE_BRLTTY="${DISABLE_BRLTTY:-1}"
BUILD_ONLY="${BUILD_ONLY:-0}"
MODBUS_SERVICE="jetson-ai-modbus-rtu-modbusd.service"
MODBUS_WAS_ACTIVE=0
BUILD_TMP=""

log() { printf '[ch341] %s\n' "$*"; }
die() { printf '[ch341] ERROR: %s\n' "$*" >&2; exit 1; }

cleanup() {
  [[ -z "$BUILD_TMP" ]] || rm -rf "$BUILD_TMP"
  if [[ "$MODBUS_WAS_ACTIVE" == "1" ]]; then
    sudo systemctl start "$MODBUS_SERVICE" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

[[ "$(uname -m)" == "aarch64" ]] || die "this bundled driver is intended for Jetson aarch64"
[[ "$KERNEL_RELEASE" == "5.15.148-tegra" ]] || die "unsupported kernel: $KERNEL_RELEASE (expected 5.15.148-tegra)"
[[ -d "$KERNEL_BUILD" ]] || die "matching kernel headers are missing: $KERNEL_BUILD"
[[ -f "$KERNEL_BUILD/Module.symvers" ]] || die "matching Module.symvers is missing: $KERNEL_BUILD/Module.symvers"
command -v make >/dev/null 2>&1 || die "make is missing; install build-essential first"
command -v gcc >/dev/null 2>&1 || die "gcc is missing; install build-essential first"
command -v sudo >/dev/null 2>&1 || die "sudo is required"

BUILD_TMP="$(mktemp -d /tmp/jetson-ch341-build.XXXXXX)"
cp "$DRIVER_DIR/ch341.c" "$DRIVER_DIR/Makefile" "$BUILD_TMP/"

log "building for $KERNEL_RELEASE"
make -C "$KERNEL_BUILD" M="$BUILD_TMP" modules
[[ -f "$BUILD_TMP/ch341.ko" ]] || die "build completed without ch341.ko"

BUILT_VERMAGIC="$(modinfo -F vermagic "$BUILD_TMP/ch341.ko")"
[[ "$BUILT_VERMAGIC" == "$KERNEL_RELEASE "* ]] || die "module vermagic does not match: $BUILT_VERMAGIC"

if [[ "$BUILD_ONLY" == "1" ]]; then
  log "build-only verification successful: $BUILT_VERMAGIC"
  exit 0
fi

if [[ "$DISABLE_BRLTTY" == "1" ]]; then
  if systemctl cat brltty.service >/dev/null 2>&1 || systemctl cat brltty-udev.service >/dev/null 2>&1; then
    log "masking brltty services that conflict with CH340/CH341 adapters"
    sudo systemctl mask --now brltty.service brltty-udev.service
    sudo pkill -x brltty >/dev/null 2>&1 || true
  fi
fi

if systemctl is-active --quiet "$MODBUS_SERVICE"; then
  log "temporarily stopping $MODBUS_SERVICE"
  sudo systemctl stop "$MODBUS_SERVICE"
  MODBUS_WAS_ACTIVE=1
fi

if lsmod | awk '{print $1}' | grep -qx ch341; then
  log "unloading the currently loaded ch341 module"
  sudo modprobe -r ch341 || die "ch341 is in use; stop the serial service and retry"
fi

if [[ -f "$MODULE_DEST" ]]; then
  log "backing up the previous module to $BACKUP_DIR/ch341.ko.previous"
  sudo install -d -m 755 "$BACKUP_DIR"
  sudo install -m 644 "$MODULE_DEST" "$BACKUP_DIR/ch341.ko.previous"
fi

log "installing $MODULE_DEST"
sudo install -D -m 644 "$BUILD_TMP/ch341.ko" "$MODULE_DEST"
sudo depmod -a "$KERNEL_RELEASE"
sudo modprobe ch341

if [[ "$MODBUS_WAS_ACTIVE" == "1" ]]; then
  log "restarting $MODBUS_SERVICE"
  sudo systemctl start "$MODBUS_SERVICE"
  MODBUS_WAS_ACTIVE=0
fi

log "installed successfully"
modinfo ch341 | grep -E '^(filename|vermagic):'
printf '\nReconnect the adapter, then verify with:\n'
printf '  ls -l /dev/ttyUSB*\n'
printf '  sudo dmesg -T | grep -Ei "ch341|ttyUSB|control message" | tail -20\n'
