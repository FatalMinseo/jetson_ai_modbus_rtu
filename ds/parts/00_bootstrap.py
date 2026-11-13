# Auto-split from runtime.py: lines 1-243
#!/usr/bin/env python3
"""
bench_ds_camera.py — USB 카메라 전용 DeepStream 파이프라인 / port 5022

파이프라인:
  v4l2src → MJPEG decode → nvstreammux
    → nvinfer  [TRT, output-tensor-meta=1]
    → [infer src probe: 텐서 → NvDsObjectMeta 생성]
    → nvtracker [IOU tracker → object_id 할당]
    → [tracker src probe: ROI 카운팅 + NvDsDisplayMeta(ROI폴리곤/탐지선)]
    → nvdsosd   [GPU bbox/text/라인 렌더링]
    → nvvideoconvert
    → nvmultistreamtiler [GPU 그리드 합성]
    → nvvideoconvert (NVMM RGBA → sys I420)
    → jpegenc → appsink [localhost MJPEG 미리보기]

출력:
  - Local MJPEG: /stream.mjpg, /last_frame.jpg

cv2 완전 제거:
  - 영상 메타: ffprobe
  - 첫 프레임 썸네일: ffmpeg pipe
  - 합성 썸네일: Pillow
  - NMS: 순수 numpy
  - point-in-polygon: ray casting 알고리즘
"""

import os, sys, time, json, threading, tempfile, ctypes, glob as _glob, re, gc, subprocess, shutil, socket
import numpy as np
import psutil
from collections import deque
from flask import Flask, render_template, request, jsonify, Response
from detection_fusion import cross_model_suppressed_ids

try:
    from PIL import Image, ImageDraw, ImageFont
    import io as _io
    _PIL_OK = True
except ImportError:
    Image = ImageDraw = ImageFont = None
    _PIL_OK = False
    print('[WARN] Pillow 미설치 — 합성 썸네일 비활성화')

PACKAGE_DIR   = os.path.dirname(os.path.abspath(__file__))
BASE_DIR      = os.path.abspath(os.path.join(PACKAGE_DIR, '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# ── pyds / GStreamer 초기화 ────────────────────────────────────
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
Gst.init(None)
import pyds

# WebRTC is used only by the optional local display branch. Inference,
# tracking, counting and Modbus continue even when signaling or the peer is
# unavailable.
from gi.repository import GLib as _GLib
try:
    gi.require_version('GstSdp', '1.0')
    gi.require_version('GstWebRTC', '1.0')
    from gi.repository import GstSdp, GstWebRTC
    _WEBRTC_AVAILABLE = True
except Exception:
    GstSdp = GstWebRTC = None
    _WEBRTC_AVAILABLE = False

# QtWebEngine on Jetson can invalidate the whole page when a WebRTC video
# surface is recreated. Exhibition deployments therefore default to the
# canvas-based localhost MJPEG path. WebRTC remains an explicit opt-in for
# environments where its native surface has been validated.
_PREVIEW_TRANSPORT = os.environ.get(
    'DS_CAMERA_UI_PREVIEW_TRANSPORT', 'mjpeg').strip().lower()
if _PREVIEW_TRANSPORT not in ('mjpeg', 'webrtc'):
    _PREVIEW_TRANSPORT = 'mjpeg'
if _PREVIEW_TRANSPORT != 'webrtc':
    _WEBRTC_AVAILABLE = False
print(f'[Preview] UI transport: {_PREVIEW_TRANSPORT}', flush=True)

_RTSP_AVAILABLE = False
GstRtspServer = None


class LiveRestreamService:
    """No-op compatibility shim; ds_camera production mode is camera-only."""

    def __init__(self, enabled: bool, max_stream_fps: float = 30.0):
        self.enabled = False
        self.max_stream_fps = max_stream_fps

    def wait_ready(self, streams, timeout: float = 3.0) -> bool:
        return True

    def start(self, streams):
        return None

    def stop(self, streams):
        return None

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

# UNTRACKED_OBJECT_ID: nvtracker가 할당 전 초기값
try:
    _UNTRACKED_OBJ_ID = pyds.UNTRACKED_OBJECT_ID
except AttributeError:
    _UNTRACKED_OBJ_ID = 0xFFFFFFFFFFFFFFFF

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=None,
)
app.config['TEMPLATES_AUTO_RELOAD'] = True

MODEL_DIR     = os.path.join(BASE_DIR, 'models')
LABELS_FILE   = os.path.join(BASE_DIR, 'deepstream', 'labels.txt')
SETTING_PRESET_FILE = os.path.join(BASE_DIR, 'setting_preset.json')
LEGACY_ROI_LOG_FILE = os.path.join(BASE_DIR, 'roi_logs.json')
CURRENT_ROI_STATE_FILE = os.path.join(BASE_DIR, 'current_roi_state.json')
OPERATION_SETTINGS_FILE = os.path.join(BASE_DIR, 'operation_settings.json')
VIDEO_DIR     = os.path.join(BASE_DIR, 'ds', 'test_videos')  # ds_camera 기준 테스트 영상 폴더
os.makedirs(VIDEO_DIR, exist_ok=True)

# nvtracker 공유 라이브러리 (DS 버전별 경로 자동 탐색)
_TRACKER_LIB_CANDIDATES = [
    '/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so',
    '/opt/nvidia/deepstream/deepstream-7.1/lib/libnvds_nvmultiobjecttracker.so',
    '/opt/nvidia/deepstream/deepstream-6.0/lib/libnvds_nvmultiobjecttracker.so',
    '/opt/nvidia/deepstream/deepstream-6.1/lib/libnvds_nvmultiobjecttracker.so',
    '/opt/nvidia/deepstream/deepstream/lib/libnvds_mot_iou.so',
]
_TRACKER_LIB = next(
    (p for p in _TRACKER_LIB_CANDIDATES if os.path.exists(p)),
    _TRACKER_LIB_CANDIDATES[0]
)

FIXED_MODEL_NAME = 'traffic_infer_ds'
FIXED_MODEL_ENGINE = os.path.join(MODEL_DIR, FIXED_MODEL_NAME + '.engine')
DEFAULT_TRACKER_KEY = 'nvsort'
# Backward-compatible name used by older saved settings and result metadata.
FIXED_TRACKER_KEY = DEFAULT_TRACKER_KEY

_MULTI_TRACKER_LIB_CANDIDATES = [
    path for path in _TRACKER_LIB_CANDIDATES
    if not path.endswith('libnvds_mot_iou.so')
]

TRACKER_PROFILES = {
    'nvsort': {
        'label': 'NvSORT',
        'll_config_file': os.path.join(BASE_DIR, 'deepstream', 'config_tracker_NvSORT.yml'),
        'lib_candidates': list(_MULTI_TRACKER_LIB_CANDIDATES),
    },
    'iou': {
        'label': 'IOU',
        'll_config_file': os.path.join(BASE_DIR, 'deepstream', 'config_tracker_IOU.yml'),
        'lib_candidates': list(_TRACKER_LIB_CANDIDATES),
    },
    'nvdcf_counting': {
        'label': 'NvDCF (Counting)',
        'll_config_file': os.path.join(BASE_DIR, 'deepstream', 'config_tracker_NvDCF_counting.yml'),
        'lib_candidates': list(_MULTI_TRACKER_LIB_CANDIDATES),
    },
    'nvdcf_accuracy': {
        'label': 'NvDCF (Accuracy)',
        'll_config_file': os.path.join(BASE_DIR, 'deepstream', 'config_tracker_NvDCF_accuracy.yml'),
        'lib_candidates': list(_MULTI_TRACKER_LIB_CANDIDATES),
    },
}


def _get_tracker_profile(profile_key: str | None):
    key = str(profile_key or DEFAULT_TRACKER_KEY).strip().lower()
    if key not in TRACKER_PROFILES:
        key = DEFAULT_TRACKER_KEY
    profile = TRACKER_PROFILES[key]
    lib_path = next(
        (p for p in profile.get('lib_candidates', []) if os.path.exists(p)),
        ''
    )
    return {
        'key': key,
        'label': profile['label'],
        'll_config_file': profile['ll_config_file'],
        'll_lib_file': lib_path,
    }

CAMERA_NAME   = os.environ.get('CAMERA_NAME', 'ELP-USB13M02-MFV(5-50)')
CAMERA_DEVICE = os.environ.get('CAMERA_DEVICE', '/dev/video0')
CAMERA_WIDTH  = int(os.environ.get('CAMERA_WIDTH', '1280'))
CAMERA_HEIGHT = int(os.environ.get('CAMERA_HEIGHT', '720'))
CAMERA_FPS    = int(os.environ.get('CAMERA_FPS', '30'))

# 카메라는 파일 영상처럼 루프 경계가 없으므로 NvSORT ID가 계속 증가한다.
# 임계값에 도달한 뒤 화면이 비어 있는 안전한 시점에 STREAM_RESET을 보낸다.
_CAMERA_TRACKER_ID_RESET_THRESHOLD = max(
    1, int(os.environ.get('DS_CAMERA_TRACKER_ID_RESET_THRESHOLD', '60000')))
_CAMERA_TRACKER_EMPTY_RESET_SEC = max(
    0.0, float(os.environ.get('DS_CAMERA_TRACKER_EMPTY_RESET_SEC', '2.0')))

_GPU_LOAD_PATH = '/sys/devices/platform/bus@0/17000000.gpu/load'
def _read_gpu_pct():
    try: return round(int(open(_GPU_LOAD_PATH).read().strip()) / 10.0, 1)
    except: return -1.0
def _read_cpu_pct(): return psutil.cpu_percent(interval=None)
psutil.cpu_percent(interval=None)


def _is_jetson():
    if os.path.exists('/etc/nv_tegra_release'):
        return True
    try:
        with open('/proc/device-tree/model', 'r', encoding='utf-8',
                  errors='ignore') as f:
            return 'jetson' in f.read().lower()
    except Exception:
        return False


_IS_JETSON = _is_jetson()
_GST_LAUNCH = shutil.which('gst-launch-1.0')
_FFMPEG = shutil.which('ffmpeg')
_env_live = os.environ.get('DS_USE_LIVE_RESTREAM', '').strip().lower()
_USE_LIVE_RESTREAM = _env_live in ('1', 'true', 'yes', 'on')
_env_nvuri = os.environ.get('DS_USE_NVURI_FILE_LOOP', '').strip().lower()
# 기본값 ON: 로컬 H.264/MP4 파일은 APACK과 동일하게 NVIDIA 내부
# file-loop를 사용해 추론/트래커 파이프라인을 유지한다.
# 중간 restream 재인코딩이 화질을 무너뜨릴 때 DS_USE_LIVE_RESTREAM=1을 명시해 별도 송출 경로를 쓴다.
_USE_NVURI_FILE_LOOP = _env_nvuri not in ('0', 'false', 'no', 'off')
_MAX_STREAM_FPS = 30.0
_RELAY_SERVICE = LiveRestreamService(_USE_LIVE_RESTREAM, _MAX_STREAM_FPS)


def _safe_set_property(el, prop, value):
    try:
        if el is not None and el.find_property(prop) is not None:
            el.set_property(prop, value)
            return True
    except Exception as e:
        print(f'[DS] {el.get_name()}.{prop} 설정 실패: {e}')
    return False


def _apply_nvvideoconvert_jetson_war(el):
    """Jetson/DS 7.1 계열 copy-hw 불안정성 우회."""
    if not _IS_JETSON or el is None:
        return
    _safe_set_property(el, 'copy-hw', 2)          # VIC
    _safe_set_property(el, 'compute-hw', 2)       # VIC
    # Keep the plugin default-sized pool.  This helper is applied to several
    # converters in one pipeline; forcing eight output surfaces on every one
    # exhausts the Jetson's 256 MiB contiguous CMA/NVMM region.
    _safe_set_property(el, 'output-buffers', 4)


# ── 클래스 이름 로드 ──────────────────────────────────────────
def _load_labels():
    if os.path.exists(LABELS_FILE):
        with open(LABELS_FILE) as f:
            return {i: l.strip() for i, l in enumerate(f.readlines()) if l.strip()}
    for fn in sorted(os.listdir(MODEL_DIR)):
        if fn.endswith('.pt'):
            from ultralytics import YOLO
            m = YOLO(os.path.join(MODEL_DIR, fn))
            return dict(m.names)
    return {}

CLASS_NAMES = _load_labels()
NUM_CLASSES = len(CLASS_NAMES)
print(f'[DS] 클래스: {list(CLASS_NAMES.values())}')

CLASS_COLORS = [
    (0,230,80),(255,165,0),(0,180,255),(200,0,255),
    (255,80,220),(255,80,80),(255,220,0),(160,255,80),
]
