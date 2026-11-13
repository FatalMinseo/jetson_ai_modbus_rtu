#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBVIEW_FILE="${WEBVIEW_FILE:-$APP_DIR/webview.py}"
cd "$APP_DIR"

APT_UPDATED=0
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
INSTALL_DEEPSTREAM="${INSTALL_DEEPSTREAM:-auto}"   # auto | yes | no
INSTALL_PYDS="${INSTALL_PYDS:-auto}"               # auto | yes | no
RUN_CHECKS="${RUN_CHECKS:-1}"
PY_PIP_PACKAGES=(
  "Flask>=2.2"
  "numpy<2"
  "psutil>=5.9"
  "Pillow>=9"
  "pyserial>=3.5"
)

log() { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing command: $1"
}

apt_update_once() {
  if [[ "$APT_UPDATED" -eq 0 ]]; then
    sudo apt-get update
    APT_UPDATED=1
  fi
}

apt_install() {
  apt_update_once
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
}

pkg_available() {
  apt-cache show "$1" >/dev/null 2>&1
}

python_in_venv() {
  "$VENV_DIR/bin/python" "$@"
}

pip_in_venv() {
  "$VENV_DIR/bin/python" -m pip "$@"
}

log "ds_camera setup start"
log "APP_DIR=$APP_DIR"

if [[ "$(uname -m)" != "aarch64" ]]; then
  warn "This script is intended for Jetson aarch64. Current arch: $(uname -m)"
fi

if [[ -r /etc/nv_tegra_release ]]; then
  log "Jetson release: $(head -n 1 /etc/nv_tegra_release)"
else
  warn "/etc/nv_tegra_release not found. Is this a Jetson/JetPack image?"
fi

need_cmd sudo
need_cmd "$PYTHON_BIN"

# apt/dpkg 작업이 전원 차단, 재부팅 또는 Ctrl+C로 중단된 장비는 이후의 모든
# apt 설치가 "dpkg was interrupted"로 실패한다. 새 패키지를 설치하기 전에
# 미완료 패키지를 먼저 구성하고 의존성을 복구한다.
DPKG_AUDIT="$(sudo dpkg --audit 2>&1 || true)"
if [[ -n "${DPKG_AUDIT//[[:space:]]/}" ]]; then
  warn "Interrupted/incomplete dpkg state detected; repairing before setup."
  printf '%s\n' "$DPKG_AUDIT"
  sudo env DEBIAN_FRONTEND=noninteractive dpkg --configure -a
  sudo env DEBIAN_FRONTEND=noninteractive apt-get -f install -y
fi

TARGET_USER="${SUDO_USER:-$USER}"
if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx dialout; then
  log "Adding $TARGET_USER to dialout for Modbus RTU serial access"
  sudo usermod -aG dialout "$TARGET_USER"
  warn "dialout membership applies after logout/login or reboot."
else
  log "$TARGET_USER already belongs to dialout"
fi
log "Installing Ubuntu/GStreamer/QtWebEngine build/runtime packages"
apt_install \
  ca-certificates curl gnupg lsb-release \
  python3 python3-pip python3-venv python3-dev \
  python3-jetson-gpio gpiod \
  python3-gi python3-gi-cairo python3-gst-1.0 python-gi-dev \
  python3-pyqt5 python3-pyqt5.qtwebengine python3-pyqt5.qtwebchannel \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gst-plugins-bad-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  gstreamer1.0-alsa gstreamer1.0-nice \
  libgdk-pixbuf-2.0-0 libgdk-pixbuf2.0-common libgdk-pixbuf2.0-bin \
  v4l-utils ffmpeg \
  build-essential cmake ninja-build meson git pkg-config \
  libglib2.0-dev libglib2.0-dev-bin libgstreamer1.0-dev \
  libgstreamer-plugins-base1.0-dev libcairo2-dev libgirepository1.0-dev \
  libjson-glib-dev

if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx gpio; then
  log "Adding $TARGET_USER to gpio for the ROI pulse output"
  sudo usermod -aG gpio "$TARGET_USER"
  warn "gpio membership applies after logout/login or reboot."
else
  log "$TARGET_USER already belongs to gpio"
fi

if ! gst-inspect-1.0 nvstreammux >/dev/null 2>&1; then
  if [[ "$INSTALL_DEEPSTREAM" == "no" ]]; then
    warn "DeepStream GStreamer plugins not found and INSTALL_DEEPSTREAM=no."
  else
    log "DeepStream plugins not found. Trying to install an NVIDIA DeepStream apt package if available."
    apt_update_once
    DS_PKG=""
    for cand in deepstream-7.1 deepstream-7.0 deepstream-6.4 nvidia-deepstream; do
      if pkg_available "$cand"; then
        DS_PKG="$cand"
        break
      fi
    done
    if [[ -n "$DS_PKG" ]]; then
      log "Installing $DS_PKG"
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$DS_PKG"
    else
      warn "No DeepStream apt package was found in the configured apt repositories."
      warn "Install DeepStream SDK for your JetPack manually, then rerun this script."
      warn "Required plugins: nvstreammux nvinfer nvtracker nvdsosd nvvideoconvert."
    fi
  fi
fi

log "Creating application directories"
mkdir -p \
  "$APP_DIR/ds/test_videos" \
  "$APP_DIR/result_logs" \
  "$APP_DIR/logs" \
  "$APP_DIR/run"

log "Creating Python venv with system site packages: $VENV_DIR"
"$PYTHON_BIN" -m venv --system-site-packages "$VENV_DIR"
pip_in_venv install --upgrade pip setuptools wheel
pip_in_venv install "${PY_PIP_PACKAGES[@]}"

if ! python_in_venv -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; Gst.init(None); print('Gst ok')" >/dev/null 2>&1; then
  die "Python GI/GStreamer import failed inside venv. Check python3-gi/python3-gst-1.0 installation."
fi

if ! python_in_venv -c "import pyds; print('pyds ok')" >/dev/null 2>&1; then
  if [[ "$INSTALL_PYDS" == "no" ]]; then
    warn "pyds not found and INSTALL_PYDS=no."
  else
    PY_VER_HEX="$(python_in_venv - <<'PY'
import sys
print(sys.version_info.major * 100 + sys.version_info.minor)
PY
)"
    PY_TAG="cp${PY_VER_HEX}"   # e.g. cp310

    # Detect installed DeepStream version
    DS_VER=""
    for vf in /opt/nvidia/deepstream/deepstream/version \
               /opt/nvidia/deepstream/deepstream-7.1/version \
               /opt/nvidia/deepstream/deepstream-7.0/version \
               /opt/nvidia/deepstream/deepstream-6.4/version; do
      if [[ -f "$vf" ]]; then
        DS_VER="$(awk '/^Version:/{print $2}' "$vf")"
        break
      fi
    done

    # Map DeepStream version -> pyds GitHub release tag
    # Add entries here when new releases come out.
    case "$DS_VER" in
      7.1) PYDS_TAG="v1.2.0" ;;
      7.0) PYDS_TAG="v1.1.11" ;;
      6.4) PYDS_TAG="v1.1.10" ;;
      *)   PYDS_TAG="" ;;
    esac

    if [[ -n "$PYDS_TAG" ]]; then
      PYDS_WHL="pyds-${PYDS_TAG#v}-${PY_TAG}-${PY_TAG}-linux_aarch64.whl"
      PYDS_URL="https://github.com/NVIDIA-AI-IOT/deepstream_python_apps/releases/download/${PYDS_TAG}/${PYDS_WHL}"
      PYDS_TMP="/tmp/${PYDS_WHL}"
      log "Downloading pre-built pyds wheel: $PYDS_WHL"
      if command -v wget >/dev/null 2>&1; then
        wget -q --show-progress "$PYDS_URL" -O "$PYDS_TMP" && pip_in_venv install "$PYDS_TMP" || \
          warn "Pre-built pyds wheel download/install failed."
      elif command -v curl >/dev/null 2>&1; then
        curl -L --progress-bar "$PYDS_URL" -o "$PYDS_TMP" && pip_in_venv install "$PYDS_TMP" || \
          warn "Pre-built pyds wheel download/install failed."
      else
        warn "Neither wget nor curl found. Cannot download pyds wheel."
      fi
    else
      # Fallback: try bundled source build (requires Python >= 3.12)
      if [[ "$PY_VER_HEX" -lt 312 ]]; then
        warn "pyds import failed. DeepStream version '$DS_VER' has no known pre-built wheel for Python $PY_VER_HEX."
        warn "Download the matching pyds wheel from https://github.com/NVIDIA-AI-IOT/deepstream_python_apps/releases"
        warn "and install it with: .venv/bin/pip install <wheel>.whl"
      else
        BIND_DIR="$APP_DIR/deepstream_python_apps/bindings"
        PYBIND_HEADER="$BIND_DIR/3rdparty/pybind11/include/pybind11/pybind11.h"
        if [[ -f "$PYBIND_HEADER" ]]; then
          warn "pyds import failed. Trying bundled deepstream_python_apps binding build."
          DS_PATH=""
          for cand in /opt/nvidia/deepstream/deepstream /opt/nvidia/deepstream/deepstream-7.1 /opt/nvidia/deepstream/deepstream-7.0 /opt/nvidia/deepstream/deepstream-6.4; do
            if [[ -d "$cand" ]]; then
              DS_PATH="$cand"
              break
            fi
          done
          if [[ -n "$DS_PATH" ]]; then
            export CMAKE_ARGS="${CMAKE_ARGS:-} -DDS_PATH=$DS_PATH"
          fi
          export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-$(nproc)}"
          if ! pip_in_venv install "$BIND_DIR"; then
            warn "Bundled pyds build failed."
            warn "Install the DeepStream Python binding wheel matching your DeepStream SDK, then rerun setup.sh."
          fi
        else
          warn "Bundled pyds source build is unavailable in this trimmed package."
          warn "Install the DeepStream Python binding wheel matching your DeepStream SDK, then rerun setup.sh."
        fi
      fi
    fi
  fi
fi

log "Making launch scripts executable"
chmod +x \
  "$APP_DIR/setup.sh" \
  "$APP_DIR/install_systemd.sh" \
  "$APP_DIR/deploy.sh" \
  "$APP_DIR/jetsonctl.sh" \
  "$APP_DIR/drivers/ch341/install.sh"

if [[ "$RUN_CHECKS" == "1" ]]; then
  log "Running environment checks"
  python_in_venv - <<'PY'
import importlib
mods = ["flask", "numpy", "psutil", "PIL", "serial", "gi"]
for name in mods:
    importlib.import_module(name)
print("python packages ok")
PY

  if [[ -f "$WEBVIEW_FILE" ]]; then
    python_in_venv - <<'PY'
from PyQt5.QtCore import QUrl
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineProfile
from PyQt5.QtWebEngineCore import QWebEngineUrlRequestInterceptor
print("pyqt webengine ok")
PY
  fi

  python_in_venv - <<'PY'
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)
missing = []
for name in [
    "v4l2src", "jpegdec", "videoconvert", "nvstreammux", "nvinfer",
    "nvtracker", "nvdsosd", "nvvideoconvert", "jpegenc", "appsink",
]:
    if Gst.ElementFactory.find(name) is None:
        missing.append(name)
print("missing_gst=" + ",".join(missing) if missing else "gstreamer elements ok")
if missing:
    raise SystemExit(2)
PY

  if ! python_in_venv -c "import pyds; print('pyds ok')" >/dev/null 2>&1; then
    die "pyds is still not importable. DeepStream Python binding is required before running ds_camera."
  fi

  python_in_venv -m py_compile \
    "$APP_DIR/web_ui.py" \
    "$APP_DIR/infer_daemon.py" \
    "$APP_DIR/watchdog.py" \
    "$APP_DIR/app.py" \
    "$APP_DIR/ds_modbus_rtu_comm.py" \
    "$APP_DIR/detection_fusion.py" \
    "$APP_DIR/gpio_pulse.py" \
    "$APP_DIR/ds/runtime.py" \
    "$APP_DIR/ds/parts/00_bootstrap.py" \
    "$APP_DIR/ds/parts/10_common_state.py" \
    "$APP_DIR/ds/parts/20_media_yolo.py" \
    "$APP_DIR/ds/parts/40_speed_counting.py" \
    "$APP_DIR/ds/parts/50_state_config.py" \
    "$APP_DIR/ds/parts/55_comm_watchdog.py" \
    "$APP_DIR/ds/parts/60_probes_sources.py" \
    "$APP_DIR/ds/parts/70_pipeline.py" \
    "$APP_DIR/ds/parts/80_routes.py" \
    "$APP_DIR/ds/parts/90_lifecycle.py"
  if [[ -f "$WEBVIEW_FILE" ]]; then
    python_in_venv -m py_compile "$WEBVIEW_FILE"
  fi
fi

cat <<EOF

Setup complete.

Next:
  cd "$APP_DIR"
  ./install_systemd.sh
  ./jetsonctl.sh start

Local configuration UI:
  ./jetsonctl.sh ui

Optional environment variables:
  PORT=5022
  CAMERA_DEVICE=/dev/video0
  WEBVIEW_FILE=$WEBVIEW_FILE
  DS_CAMERA_COMM=modbus_rtu  # default, enable Modbus RTU slave over RS-485
  DS_CAMERA_COMM=none        # disable external communication while testing
  DS_CAMERA_MODBUS_PORT=/dev/ttyUSB0
  DS_CAMERA_MODBUS_BAUDRATE=460800
  DS_CAMERA_MODBUS_SLAVE_ID=101
  DS_CAMERA_WINDOW_SECONDS=600           # most recent 10 minutes of events

EOF
