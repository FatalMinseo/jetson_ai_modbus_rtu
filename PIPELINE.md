# DS_CAMERA 파이프라인 상세 문서

> 카메라 센서 → 추론 → 추론 결과 처리 → 데이터 변환 → 데이터 전송 까지의 전체 데이터 흐름을 기술한다.
> 이 문서는 프레임과 이벤트가 이동하는 저수준 경로를 설명한다. 프로세스 경계는
> `ARCHITECTURE.md`, 설치·운영 방법은 `README.md`와 `USAGE.md`를 참고한다.

---

## 목차

1. [전체 디렉터리 구조](#1-전체-디렉터리-구조)
2. [초기화 단계](#2-초기화-단계)
3. [카메라·비디오 소스 설정](#3-카메라비디오-소스-설정)
4. [DeepStream 파이프라인 빌드](#4-deepstream-파이프라인-빌드)
5. [추론 단계 — Probe 1: 텐서 파싱](#5-추론-단계--probe-1-텐서-파싱)
6. [트래킹 단계 — nvtracker](#6-트래킹-단계--nvtracker)
7. [카운팅·속도 측정 — Probe 2](#7-카운팅속도-측정--probe-2)
8. [루프 완료 및 결과 스냅샷](#8-루프-완료-및-결과-스냅샷)
9. [데이터 변환 및 전송 — Modbus RTU](#9-데이터-변환-및-전송--modbus-rtu)
10. [핵심 데이터 구조](#10-핵심-데이터-구조)
11. [전체 흐름 요약 테이블](#11-전체-흐름-요약-테이블)

---

## 1. 전체 디렉터리 구조

```
ds_camera/
├── jetsonctl.sh                 # systemd 서비스와 로컬 UI 통합 관리
├── infer_daemon.py              # 내부 추론 데몬 진입점
├── web_ui.py                    # 사용자가 접속하는 Flask proxy UI
├── watchdog.py                  # 추론 데몬 상태 감시/복구
├── modbus_daemon.py             # /dev/ttyUSB0와 누적 레지스터 소유
├── modbus_bridge.py             # probe → modbusd 비차단 이벤트 큐
├── ds_modbus_rtu_comm.py        # Modbus RTU 프레임/레지스터 구현
├── gpio_pulse.py                # 확정 이벤트 GPIO 펄스 출력
├── app.py                       # 단일 프로세스 모드 진입점
├── install_systemd.sh           # systemd 서비스 설치
├── setup.sh                     # Jetson 의존성/venv 설치
├── setting_preset.json          # ROI/conf 프리셋 저장
│
├── ds/                          # 핵심 파이프라인 오케스트레이터
│   ├── __init__.py              # app, run() 익스포트
│   ├── runtime.py               # parts/ 폴더의 10개 파일을 순서대로 로드
│   └── parts/
│       ├── 00_bootstrap.py      # Flask 앱, GPU/CPU 모니터링, 레이블 로딩
│       ├── 10_common_state.py   # 전역 공유 상태 (_st, _streams)
│       ├── 20_media_yolo.py     # 비디오 유틸리티, NumPy NMS, 좌표 변환
│       ├── 40_speed_counting.py # 라인 교차 카운팅, BEV 속도 계산
│       ├── 50_state_config.py   # ROI/프리셋 로딩, 설정 관리
│       ├── 55_comm_watchdog.py  # Modbus bridge/GPIO/통신 상태
│       ├── 60_probes_sources.py # GStreamer 프로브 + 비디오 소스 생성
│       ├── 70_pipeline.py       # nv*(mux/infer/tracker/osd) 체인 빌드
│       ├── 80_routes.py         # Flask 라우트 (/start, /upload, /set_roi)
│       └── 90_lifecycle.py      # 백그라운드 서비스, 클린업
│
├── deepstream/
│   ├── config_infer_primary.txt # nvinfer 기준 템플릿 (실행 시 /tmp 설정으로 동적 생성)
│   ├── config_tracker_NvSORT.yml # 기본 트래커 설정
│   ├── config_tracker_IOU.yml
│   ├── config_tracker_NvDCF_counting.yml
│   ├── config_tracker_NvDCF_accuracy.yml
│   └── labels.txt               # [person, car, bus, truck, motorcycle]
│
├── models/
│   └── traffic_infer_ds.engine  # TensorRT 컴파일된 5-class YOLO11n 엔진
│
├── run/modbus_state.json        # Modbus 누적값/명령 sequence 영속 상태
└── result_logs/                 # 루프 완료 후 저장되는 JSON 결과
```

---

## 2. 초기화 단계

### 2-1. 진입점

```
systemd / jetsonctl.sh
 ├─ infer_daemon.py
 │   └─ from ds import run()
 │       └─ ds/runtime.py 실행
 │           └─ parts/00~90 파일을 하나의 공유 네임스페이스에 순서대로 exec()
 ├─ watchdog.py
 │   └─ infer_daemon.py의 /status 감시
 └─ web_ui.py
     └─ 브라우저 요청을 infer_daemon.py로 proxy
```

`runtime.py`는 각 파트 파일을 **별도 모듈이 아닌 하나의 네임스페이스**에 순차적으로 실행한다.
따라서 `00_bootstrap.py`에서 정의한 변수를 `70_pipeline.py`에서 그대로 참조할 수 있다.

운영 진입점은 systemd이며 `jetsonctl.sh`가 서비스와 로컬 UI를 관리한다.

---

### 2-2. 부트스트랩 (`parts/00_bootstrap.py`)

| 작업 | 내용 |
|---|---|
| Flask 앱 생성 | `app = Flask(__name__)` |
| Jetson 하드웨어 감지 | `/etc/nv_tegra_release`, `/proc/device-tree/model` 파일 존재 여부 확인 |
| 레이블 로딩 | `labels.txt` → `CLASS_NAMES = {0:"person", 1:"car", 2:"bus", 3:"truck", 4:"motorcycle"}` |
| 색상 팔레트 | 클래스 시각화용 8색 RGB 팔레트 |
| GPU/CPU 모니터링 | `_read_gpu_pct()`, `_read_cpu_pct()` 함수 정의 |
| 경로 상수 | `MODEL_DIR`, `RESULT_LOG_DIR`, `VIDEO_DIR` |

---

### 2-3. 공유 상태 초기화 (`parts/10_common_state.py`)

두 개의 핵심 전역 객체가 생성된다.

#### `_st` — 전역 파이프라인 설정 딕셔너리

```python
_st = {
    'running': False,
    'conf': 0.30,               # 신뢰도 임계값
    'model_name': 'traffic_infer_ds',
    'tracker_key': 'iou',
    'mux_w': int, 'mux_h': int, # nvstreammux 해상도
    'infer_w': 640, 'infer_h': 640,
    'show_stream': True,        # 로컬 WebRTC/MJPEG 렌더링 여부
    'peoplenet_active': False,
    'restart_requested': False,
    'restart_reason': '',
    'speed_by_cls_dir': {},     # 클래스+방향별 속도 집계
}
```

#### `_streams` — 스트림별 상태 리스트

각 카메라/비디오 소스마다 하나의 딕셔너리 원소가 추가된다.

```python
_streams[i] = {
    'video_path': str,           # 파일 경로 또는 카메라 디바이스
    'is_camera': bool,
    'video_fps': float,
    'frame_w': int, 'frame_h': int,
    'frame_idx': int,            # 현재 루프 내 프레임 수
    'loop_count': int,           # 완료된 루프 횟수
    'counts': {                  # 방향별 카운트
        class_id: {'상행': N, '하행': N}
    },
    'track_state': {             # 오브젝트 ID별 추적 상태
        object_id: { ... }
    },
    'roi_pts': [[x,y], ...],
    'line1': [[x1,y1],[x2,y2]], # 속도 측정 상단선
    'line2': [[x1,y1],[x2,y2]], # 속도 측정 하단선
    'ref_distance': float,       # 선 간 실거리 (미터)
    'expected_loop_frames': int, # fps × duration
    '_eos_seen': bool,
}
```

---

## 3. 카메라·비디오 소스 설정

### 3-1. 카메라 연결 (`POST /connect_camera`)

```
브라우저 → POST /connect_camera
 └─ /dev/video0~N 중 사용 가능한 디바이스 탐색
 └─ v4l2src로 MJPEG 프레임 1장 캡처
 └─ JPEG에서 해상도 추출 (frame_w, frame_h)
 └─ _streams[0] 생성:
     {is_camera: True, camera_device: '/dev/video0', ...}
```

### 3-2. 비디오 파일 업로드 (`POST /upload`)

```
브라우저 → POST /upload (multipart)
 └─ /tmp/tmpXXXX.avi 저장
 └─ _normalize_video_to_mp4()
     └─ H.264/MP4가 아닌 경우 ffmpeg로 변환
 └─ GStreamer Discoverer로 메타데이터 추출:
     - fps, width, height, duration
 └─ expected_loop_frames = fps × duration
 └─ _streams[0] 생성:
     {video_path: '/tmp/norm_XXX.mp4', is_camera: False, ...}
```

---

## 4. DeepStream 파이프라인 빌드

`POST /start` 요청 시 `parts/70_pipeline.py`의 `_build_single_pipeline()`이 호출된다.

### 4-1. 전체 GStreamer 엘리먼트 체인

```
[소스 레이어] ─────────────────────────────────────────────
  카메라: v4l2src
  파일:   uridecodebin  또는  nvurisrcbin
      │
      ├─ nvvideoconvert   (NVMM 포맷 표준화)
      ├─ videorate        (최대 30fps 캡핑)
      └─ identity+capsfilter (타임스탬프 동기화)

[멀티플렉서] ─────────────────────────────────────────────
  nvstreammux
      ├─ batch-size = 스트림 수
      ├─ width/height = (mux_w, mux_h)
      ├─ enable-padding = 1       (letterbox — 중앙 정렬 + 패딩)
      ├─ sync-inputs = 0          (비동기 — 느린 스트림 기다리지 않음)
      └─ drop-pipeline-eos = 1    (개별 EOS로 파이프라인 중단 방지)

[1차 추론] ─────────────────────────────────────────────────
  nvinfer (YOLO11n TensorRT)
      ├─ model-engine-file = traffic_infer_ds.engine
      ├─ batch-size = N
      ├─ output-tensor-meta = 1   (Python Probe가 텐서 직접 파싱)
      ├─ cluster-mode = 4         (클러스터링 없음 — Python에서 NMS 수행)
      ├─ maintain-aspect-ratio = 1 (letterbox 입력)
      └─ model-color-format = 1   (RGB)
      │
      ▼ [★ Python Probe 1: _infer_tensor_probe] ← 핵심
      │   텐서 → YOLO 디코딩 → NMS → NvDsObjectMeta 생성
      │

[2차 추론 — 선택사항] ─────────────────────────────────────
  nvinfer (PeopleNet, peoplenet_active=True 일 때만)
      └─ class_0(person) 탐지를 더 정확한 전용 모델로 교체

[트래커] ────────────────────────────────────────────────────
  nvtracker (UI 선택: IOU / NvSORT / NvDCF Counting / NvDCF Accuracy)
      ├─ ll-lib-file = libnvds_nvmultiobjecttracker.so
      ├─ tracker-width/height = mux 해상도 기준
      └─ 선택 프로파일에 따라 동일 객체에 object_id 부여

[분기 — TEE] ───────────────────────────────────────────────
  post_infer_tee
      ├─ Branch A: infer_sink (fakesink) — 추론 항상 ON
      └─ Branch B: output_valve (토글 가능)
                   │
                   ▼ [★ Python Probe 2: _osd_counting_probe] ← 핵심
                       트래킹 결과 → 라인 교차 → 속도 → OSD 렌더링
                   │
                   ▼ nvtiler (그리드 레이아웃)
                   ▼ nvdsosd (bbox/텍스트 CPU 렌더링)
                   ▼ preview_tee
                       ├─ valve → jpegenc → appsink → localhost MJPEG (전시 기본)
                       └─ H.264 → RTP → WebRTC (환경변수로 명시할 때만)
```

### 4-2. 전시용 미리보기 이중 버퍼

GStreamer의 MJPEG 출력 큐와 선택적 WebRTC 출력 큐는 최대 2프레임이며 `leaky=downstream`으로
설정된다. 한 프레임을 인코딩하는 동안 최신 한 프레임만 대기하고, UI가 느리면
오래된 프레임을 버려 추론 경로에 역압력을 만들지 않는다.

브라우저는 WebRTC `<video>` 표면을 직접 표시하지 않고 디코딩 소스로만 사용한다.
WebRTC와 MJPEG 모두 완성된 프레임을 DOM 밖의 back buffer canvas에 먼저
그린 다음 고정된 front canvas에 복사한다. DOM 레이어 순서를 프레임마다 변경하지 않는다.
다음 프레임이 늦거나 디코더가 재동기화되면 front
buffer의 마지막 완성 프레임을 그대로 유지하므로 빈 표면이 노출되지 않는다.

```text
decode/receive frame N
    → DOM 밖 canvas B에 전체 프레임 draw
    → draw 완료 후 고정된 표시 canvas A로 복사
    → canvas B는 다음 프레임 준비에 재사용
    → frame N+1이 없으면 A를 계속 표시
```

이 버퍼는 지연을 쌓기 위한 재생 큐가 아니라, 불완전하거나 비어 있는 프레임을
사용자에게 보여주지 않기 위한 표시 버퍼다.

영상 버퍼와 별도로 `webview.py`는 Qt import 전에 `QT_QUICK_BACKEND=software`를
설정한다. Chromium의 `--disable-gpu --disable-gpu-compositing`만으로는 Qt 5의
QQuickWidget/OpenGL 창 합성이 꺼지지 않는다. 프로그램 내용 전체가 빈 배경으로
바뀌는 간헐적 깜박임을 실제 화면 녹화에서 확인하여, Qt 합성도 소프트웨어 경로로
분리했다. 추론 데몬의 GPU 설정은 바꾸지 않는다. 해당 설정은 웹페이지 새로고침이
아니라 로컬 UI 재실행 때 적용된다. 간헐적 문제이므로 짧은 녹화에서 빈 화면이
없었다는 결과만으로 전시 시간 전체의 안정성을 보장하지는 않는다.

2026-10-06 현장 검증에서는 추론을 유지한 채 최대화된 UI를 하나씩 실행하여
실제 X11 화면을 녹화했다. 기존 Qt OpenGL 경로는 30초 녹화 552프레임 중
프로그램 내용 전체가 빈 배경으로 바뀐 2프레임이 확인됐다. Qt 소프트웨어
경로는 30초 517프레임과 추가 60초 1,037프레임에서 동일한 빈 화면이
검출되지 않았고 영상 갱신도 유지됐다. 추론 상태 API는 약 30 FPS를 유지했다.
화면 판정 영역에서는 Ubuntu 알림이 겹치는 상단 영역을 제외했다.

이후 재실행에서 `loadStarted` 뒤 완료 없이 검정 화면에 머무는 별도의 시작 실패도
확인됐다. 창을 중앙 위젯에 연결하고 표시한 뒤 GUI 이벤트 루프에서 첫 탐색을
시작하도록 순서를 변경했다. 로드 진행이 15초 동안 멈추면 기존 로드를 취소하고
단일 재시도 타이머로 재탐색한다. 취소 시 발생하는 `loadFinished(False)`가 중복
탐색을 예약하지 않게 했으며, 연속 3회 복구 실패 후에는 자동 재시도를 중단하고
오류 메시지를 표시한다. 이는 시작 순서/로드 정지에 대한 방어이며, 저수준 경합의
정확한 원인을 확정한 것은 아니다. F5 또는 Ctrl+R로 수동 재시도할 수 있다.
변경 후 `jetsonctl.sh ui`로 창을 하나씩 5회 새로 실행한 검증에서는 모두
1.50~1.76초 안에 로드 완료됐고 정지 복구 타이머는 발동하지 않았다. 첫 번째와
다섯 번째 실행은 화면 캡처로 UI 및 영상 표시도 확인했다.

---

### 4-3. 루프 감지 메커니즘

비디오 파일 재생 시 루프(반복 재생)를 감지하는 두 가지 방법이 있다.

기본 입력은 APACK과 동일한 `nvurisrcbin(file-loop=1)`이며 파이프라인과
TensorRT 엔진을 유지한 채 소스 내부에서 반복한다. 사용할 수 없거나
`DS_USE_NVURI_FILE_LOOP=0`이면 `uridecodebin`의 EOS 재빌드 방식으로 폴백한다.

**방법 A — EOS 프로브** (`_make_loop_probe`):
```
uridecodebin에서 EOS 이벤트 발생
 └─ ss['_eos_seen'] = True
 └─ _finalize_stream_cycle() 호출 → 결과 스냅샷
 └─ 모든 스트림 EOS 확인 후 restart_requested = True
```

**방법 B — 프레임 카운트 폴백** (`_osd_counting_probe` 내부, uridecodebin 전용):
```
frame_idx >= expected_loop_frames 도달 시
 └─ _finalize_stream_cycle('frame_count') 호출
```

EOS 이벤트가 일부 DeepStream 버전에서 신뢰할 수 없으므로, 방법 B가 안전망으로 동작한다.

---

## 5. 추론 단계 — Probe 1: 텐서 파싱

**위치:** `parts/60_probes_sources.py` → `_infer_tensor_probe()`

**GStreamer 프로브 설치 위치:** nvinfer 엘리먼트의 **src pad** (출력 측)

### 5-1. 텐서 데이터 추출

```python
# DeepStream Python 바인딩으로 프레임 메타데이터 순회
batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
for frame_meta in batch_meta.frame_meta_list:
    for user_meta in frame_meta.frame_user_meta_list:
        if user_meta.base_meta.meta_type == NvDsInferTensorMeta:
            tensor_meta = pyds.NvDsInferTensorMeta.cast(user_meta.user_meta_data)
            # output_blob_names에서 'output0' 텐서 추출
            arr = get_layer_as_numpy(tensor_meta, 'output0')
```

**텐서 형태:** `(batch_size, num_detections, 5 + num_classes)` 또는 `(batch, 8400, 85)` 형태 (YOLO11n 표준)

---

### 5-2. YOLO 후처리 (`yolo_postprocess`)

```
raw tensor (shape: [batch, 8400, 5+classes])
        │
        ▼
[1] 각 앵커(8400개)에서 클래스별 confidence 추출
        │
        ▼
[2] max_confidence > conf_threshold 필터링
        │
        ▼
[3] cx, cy, w, h → x1, y1, x2, y2 변환 (letterbox 640×640 공간)
        │
        ▼
[4] NumPy NMS (IOU 임계값 0.45)
    - confidence 내림차순 정렬
    - 가장 높은 confidence 박스 선택
    - 해당 박스와 IOU > 0.45인 모든 박스 제거
    - 남은 박스로 반복
        │
        ▼
결과: [[x1, y1, x2, y2, confidence, class_id], ...]
      (모두 640×640 infer 공간 좌표)
```

---

### 5-3. Letterbox 역변환 (infer 공간 → mux 공간)

nvinfer의 `maintain-aspect-ratio=1` 설정으로 입력 이미지는 **letterbox** 처리된다:
- 원본 이미지를 640×640에 맞게 비율 유지 스케일링
- 상하 또는 좌우에 검은 패딩 추가

탐지 결과를 원본 mux 해상도로 되돌려야 한다:

```python
# scale: 원본이 640에 맞도록 축소된 비율
scale = min(infer_w / mux_w, infer_h / mux_h)

# 패딩 계산
pad_left = (infer_w - mux_w * scale) / 2
pad_top  = (infer_h - mux_h * scale) / 2

# 역변환
x1_mux = (x1_infer - pad_left) / scale
y1_mux = (y1_infer - pad_top)  / scale
x2_mux = (x2_infer - pad_left) / scale
y2_mux = (y2_infer - pad_top)  / scale

# 경계 클리핑
x1_mux = max(0, min(mux_w, x1_mux))
```

---

### 5-4. NvDsObjectMeta 생성

변환된 좌표로 DeepStream 오브젝트 메타데이터를 생성한다:

```python
obj_meta = pyds.nvds_acquire_obj_meta_from_pool(batch_meta)
obj_meta.class_id = class_id
obj_meta.confidence = score
obj_meta.object_id = UNTRACKED_OBJ_ID   # 0xFFFFFFFFFFFFFFFF (임시)
obj_meta.obj_label = CLASS_NAMES[class_id]

rect = obj_meta.rect_params
rect.left, rect.top   = x1_mux, y1_mux
rect.width, rect.height = (x2_mux - x1_mux), (y2_mux - y1_mux)

pyds.nvds_add_obj_meta_to_frame(frame_meta, obj_meta, None)
```

`object_id = UNTRACKED_OBJ_ID`는 아직 트래커가 ID를 부여하지 않았음을 나타내는 플레이스홀더다.

---

### 5-5. bInferDone 플래그

```python
frame_meta.bInferDone = True   # PeopleNet 미사용 시
```

이 플래그를 `True`로 설정해야 다음 단계인 **nvtracker**가 이 프레임의 탐지 결과를 처리한다.

---

## 6. 트래킹 단계 — nvtracker

**라이브러리:** `/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so`

| UI 선택값 | 설정 파일 | 용도 |
|---|---|---|
| NvSORT | `config_tracker_NvSORT.yml` | 속도와 ID 안정성의 균형, 기본값 |
| IOU | `config_tracker_IOU.yml` | 가장 가벼운 선택지 |
| NvDCF (Counting) | `config_tracker_NvDCF_counting.yml` | 가림 이후 ID 유지와 카운팅 우선 |
| NvDCF (Accuracy) | `config_tracker_NvDCF_accuracy.yml` | 시각 특징을 강화한 정확도 우선 |

선택값은 추론 시작 설정에 저장되며 재부팅 복원과 watchdog 파이프라인 재시작에도
동일하게 적용됩니다.

### 6-1. IOU 트래커 동작 원리

```
프레임 N-1의 트랙 목록 (object_id가 있는 bbox들)
        │
        ├─ 프레임 N의 신규 탐지 목록 (object_id = UNTRACKED)
        │
        ▼
[1] 모든 (이전 트랙) × (신규 탐지) 조합에 대해 IOU 계산
    IOU = 교집합 넓이 / 합집합 넓이

[2] 탐욕(Greedy) 할당:
    - IOU 내림차순 정렬
    - IOU가 가장 높은 쌍부터 매칭
    - 이미 매칭된 트랙/탐지는 제외

[3] 매칭 결과:
    - 매칭된 탐지 → 이전 트랙의 object_id 계승
    - 매칭 안 된 탐지 → 새 object_id 생성 (신규 객체)
    - 매칭 안 된 이전 트랙 → 소멸 (객체 떠남)

[4] 출력:
    - obj_meta.object_id에 할당된 ID 기입
    - Probe 2에서 이 ID를 읽어 트랙 상태 관리
```

### 6-2. IOU 트래커의 특성

- **장점:** 연산 비용이 매우 낮음, 실시간 처리에 유리
- **단점:** 프레임 간 큰 이동이나 오클루전에 취약
- **ID 재활용 없음:** 한번 소멸된 ID는 재사용되지 않음 (새 ID 발급)
- **프레임 스킵 주의:** `infer_interval > 0` 설정 시 IOU 정확도 저하 가능

---

## 7. 카운팅·속도 측정 — Probe 2

**위치:** `parts/60_probes_sources.py` → `_osd_counting_probe()`

**GStreamer 프로브 설치 위치:** output_valve → q_post_track 사이 (nvtracker 이후)

### 7-1. 루프 감지 (영상 재생 되감기 탐지)

```python
cur_frame_num = frame_meta.frame_num
cur_buf_pts   = frame_meta.buf_pts   # 타임스탬프 (nanoseconds)

# 프레임 번호가 이전보다 작아지면 루프 재시작
if cur_frame_num < ss['_last_frame_num'] - 5:
    _advance_stream_loop(ss, reason='frame_num_rewind')

# 타임스탬프가 크게 되돌아가도 루프 재시작
if cur_buf_pts < ss['_last_buf_pts'] - threshold:
    _advance_stream_loop(ss, reason='pts_rewind')
```

`_advance_stream_loop()` 실행 시:
- `loop_count += 1`
- `counts`, `track_state` 초기화
- `frame_idx = 0`

---

### 7-2. 트랙 오브젝트 수집

```python
tracked = []
for obj_meta in frame_meta.obj_meta_list:
    track_id  = obj_meta.object_id
    class_id  = obj_meta.class_id
    rect      = obj_meta.rect_params
    x1 = rect.left
    y1 = rect.top
    x2 = rect.left + rect.width
    y2 = rect.top  + rect.height
    tracked.append((track_id, x1, y1, x2, y2, class_id))
```

---

### 7-3. 속도 측정 — 두 가지 방식

`roi_ready = True` 일 때만 `_do_counting()` 호출.

#### 방식 A: BEV 호모그래피 (ref_distance > 0)

**원리:** 카메라의 원근 왜곡을 제거하고 조감도(Bird's Eye View)로 변환한 뒤 픽셀 이동거리로 속도 계산.

```
실세계 사다리꼴 ROI (카메라 시점)
        │
        ▼ cv2.getPerspectiveTransform() 또는 NumPy 호모그래피
        │
BEV 직사각형 (400×600 정규화 공간)
        │
        ▼
객체 발끝(bbox 하단 중심) 좌표를 BEV로 변환:
    bev_y = homography(foot_x, foot_y)  → 0~600 범위 y값

        │
        ▼
최근 5프레임의 bev_y 히스토리에서 이동거리 계산:
    dy_px = bev_y[t] - bev_y[t-1]  (픽셀)
    dt_sec = time[t] - time[t-1]   (초)

        │
        ▼
실거리 환산:
    v_kph = |dy_px| × (ref_distance / 600) / dt_sec × 3.6

        │
        ▼
최근 5~10 샘플의 중앙값(median) → 노이즈 제거 후 표시 속도
```

#### 방식 B: 라인 통과 시간 측정 (최종 기록의 기본값)

```
line1 통과 시각 t1 기록
        │
line2 통과 시각 t2 기록
        │
        ▼
v_kph = ref_distance / |t2 - t1| × 3.6

유효 범위: 1.0 < v_kph < 350.0
```

통과 시간 차이는 0.05초 이상이어야 한다. BEV 샘플이 5개 이상이고 그 중앙값이
시간 기반 속도와 30% 이내로 일치하면 두 값의 평균을 기록한다. 사람은 속도
유효성과 무관하게 통과 대수를 기록하며 속도는 0으로 전달한다.

---

### 7-4. 라인 교차 감지

라인 교차는 **외적(Cross Product)**의 부호 변화로 감지한다.

```python
def _cross(line_p1, line_p2, point):
    """점이 라인의 어느 쪽에 있는지 부호로 반환"""
    dx = line_p2[0] - line_p1[0]
    dy = line_p2[1] - line_p1[1]
    return dx * (point[1] - line_p1[1]) - dy * (point[0] - line_p1[0])

# prev_pos와 cur_pos의 부호가 반대이면 교차
prev_sign = sign(_cross(line_p1, line_p2, prev_pos))
cur_sign  = sign(_cross(line_p1, line_p2, cur_pos))

if prev_sign * cur_sign < 0 and movement_crosses_finite_line_segment:
    crossing_detected!
    record crossing_time = current_frame_time
```

---

### 7-5. 카운팅 로직

중복 방지 단위는 **객체 ID의 전체 수명이 아니라 한 번의 완전 통과**다.
같은 ID 100이 `L2 → L1` 통과 후 돌아서 `L1 → L2`를 통과하면 상행 1,
하행 1을 기록한다(L2가 화면에서 아래쪽인 경우). 같은 방향의 반복 통과도 허용한다.

1. 각 선에서 다른 선의 중점이 있는 쪽으로 교차하면 구간 **진입**으로 판단한다.
   새 진입 시각을 저장하고 이전 통과의 시간·BEV 샘플·표시 속도를 초기화한다.
2. 진입한 선으로 다시 나가면 시도를 취소한다. 대수는 세지 않고, 재진입 때
   시간을 새로 측정한다.
3. 반대 선으로 나가면 완전 통과다. 해당 회차의 시간 차이로 속도를 계산하고,
   기존 속도 유효성 조건에 따라 카운트·통계·Modbus 이벤트를 한 번 기록한다.
4. 완료된 두 교차 기록과 샘플은 소비하여 비운다. 속도가 무효인 통과도 소비한다.
   다음 통과는 같은 ID여도 새 진입부터 시작한다. 완료 지점의 교차를 다음 진입으로
   재사용하지 않으므로 정지 프레임이 반복되어도 중복 카운트되지 않는다.

아래쪽 선에서 진입해 위쪽 선으로 나가면 상행, 반대는 하행이다. 분석 이벤트의
영상 시각은 `elapsed_offset + max(t1, t2)`로 실제 마지막 선 통과 시각을 사용한다.
영상 중간에서 처음 탐지되어 진출만 관측한 객체는 완전 통과로 세지 않는다.

이 변경은 트래커 ID를 변경하거나 재발급하지 않는다. 정확히 선 위의 점(`부호=0`)
처리와 서로 다른 ID 간 재연결은 별도 문제이며 이번 변경으로 보완되지 않는다.

---

### 7-6. track_state 구조 (오브젝트별 추적 상태)

```python
track_state[object_id] = {
    'cls':            int,          # 클래스 ID (다수결 투표)
    'class_votes':    [int, ...],   # 모든 프레임에서 탐지된 클래스 목록
    'last_pos':       (cx, bottom_y),
    'last_time':      float,        # 마지막 업데이트 시각 (초)
    'bev_y_hist':     [(y, t), ...],# BEV y좌표 + 시각 이력 (최근 10개)
    'speed_samples':  [float, ...], # 프레임별 속도 샘플 (최근 10개)
    'last_valid_disp': float,       # OSD 표시용 중앙값 속도
    'line1_cross':    None or {'time': float},
    'line2_cross':    None or {'time': float},
    'speed':          None or float,# 최종 속도 (km/h)
    'direction':      None or str,  # '상행' or '하행'
}
```

**클래스 다수결 투표 (`class_votes`):**
같은 객체가 프레임마다 다른 클래스로 탐지될 수 있으므로 (예: car ↔ truck),
`class_votes`에 모든 탐지 클래스를 쌓고 `Counter.most_common(1)`로 최종 클래스를 결정한다.

---

## 8. 루프 완료 및 결과 스냅샷

**함수:** `_finalize_stream_cycle(ss, reason)`

```
EOS 이벤트  또는  frame_idx >= expected_loop_frames
        │
        ▼
결과 스냅샷 생성:
{
    "completed_at": "2024-04-29 14:35:22",
    "loop_no": 1,
    "video_name": "test.mp4",
    "loop_time_sec": 30.0,
    "counts": {
        "person":     {"상행": 10, "하행": 15, "합계": 25},
        "car":        {"상행": 25, "하행": 30, "합계": 55,
                       "상행_avg_kph": 48.5, "하행_avg_kph": 52.3},
        "bus":        { ... },
        "truck":      { ... },
        "motorcycle": { ... }
    },
    "total_count": 150,
    "frame_idx": 900,
    "video_fps": 30.0,
    "ref_distance": 2.5,
    "speed_stat": {"min": 1.5, "max": 89.2, "avg": 45.3, "n": 80}
}
        │
        ▼
per-loop 상태 초기화:
    loop_count   += 1
    counts        = {}
    track_state   = {}
    frame_idx     = 0
    _last_frame_num = -1
    _last_buf_pts   = -1

        │
        ▼  (GET /save_result_log?stream_idx=0 시)
JSON 파일 저장:
    파일명: {video_name}_{model}_{tracker}_YYYY-MM-DD_HH-MM-SS.json
    경로:   /result_logs/
```

---

## 9. 데이터 변환 및 전송 — Modbus RTU

현재 배포본은 LwM2M을 사용하지 않는다. 확정된 ROI 통과 이벤트는 다음 경로로
전달된다.

```text
_do_counting()
  → _record_modbus_roi_event(class, direction, speed)
      ├─ gpio_pulse.py: BOARD 29에 비동기 100ms 펄스
      └─ modbus_bridge.py: event_id를 붙여 메모리 큐에 적재
           → POST http://127.0.0.1:5123/event
                → modbus_daemon.py
                    ├─ event_id 중복 제거
                    ├─ 설정된 길이의 실시간 통계 갱신
                    ├─ 방향별 누적 통계 갱신
                    ├─ run/modbus_state.json 원자적 저장
                    └─ ds_modbus_rtu_comm.py 레지스터 맵 제공
                         → /dev/ttyUSB0, FC03/FC16
```

추론 probe는 직렬 I/O나 HTTP 완료를 기다리지 않는다. `modbus_bridge.py`의
worker가 실패한 이벤트를 재시도하고, `modbus_daemon.py`는 `event_id`로 중복
반영을 막는다. 누적값과 직렬 포트의 권위 있는 소유자는 modbusd이므로 inferd
재시작 중에도 Modbus 통신과 기존 누적값이 유지된다.

Master의 FC16 쓰기는 다음과 같이 처리된다.

- ROI 기준 거리·confidence: `operation_settings.json`에 저장 후 inferd 내부 API로 반영
- 누적 슬롯 초기화: modbusd에서 먼저 영속값을 지우고 inferd UI 상태에 동기화
- 추론 재시작·OS 재부팅: 응답 프레임 전송 후 별도 제어 명령 실행

### 9-1. 이전 LwM2M 설계 기록

아래 내용은 이 프로젝트의 이전 LwM2M 배포 설계를 설명하는 기록이며 현재 실행
코드에는 포함되지 않는다. 과거 통신 규격을 비교하거나 마이그레이션할 때만 참고한다.

#### 이전 백그라운드 런타임 시작

`parts/90_lifecycle.py` → `_start_background_services()`:

```
Flask 앱 시작
        │
        ▼
LwM2MRuntime.start()
    └─ 데몬 스레드 생성
    └─ asyncio 이벤트 루프 실행
    └─ lwm2m_client/client.py 로드
    └─ data.json에서 ClientModel 초기화
    └─ CoAP 서버에 LwM2M 등록:
           endpoint: `DS_CAMERA_LWM2M_ENDPOINT` 값 또는 기본 `Pylwm2mTEST2`
           lifetime: 300초
           register_update_interval: 90초
```

---

#### 이전 데이터 변환 (`build_13432_from_stats`)

추론 시작 후 집계 주기(`DS_CAMERA_LWM2M_INTERVAL_SEC`, 기본 600초)가 지나면
LwM2M 통신 스레드가 결과를 Object 13432 형식으로 변환한다.
LwM2M endpoint 등록도 첫 집계 주기 도달 시점에 시작된다.

**클래스 → LwM2M 타입 매핑:**

| class_id | 클래스 | LwM2M type 코드 |
|---|---|---|
| 0 | person | 47 |
| 1 | car | 32 |
| 2 | bus | 6 |
| 3 | truck | 24 |
| 4 | motorcycle | 28 |

**Object 13432 구조 (JSON 표현):**

```json
{
  "13432": {
    "0": {
      "0": 47,
      "1": {"0": 10, "1": 15}
    },
    "1": {
      "0": 32,
      "1": {"0": 25, "1": 30},
      "2": {"0": 48, "1": 52}
    },
    "2": {
      "0": 6,
      "1": {"0": 5, "1": 8},
      "2": {"0": 35, "1": 40}
    }
  }
}
```

- `인스턴스[0]`: type 코드
- `인스턴스[1]`: {0: 상행 카운트, 1: 하행 카운트}
- `인스턴스[2]`: {0: 상행 평균속도, 1: 하행 평균속도} (속도 있는 클래스만)

---

#### 이전 TLV 인코딩 (`ds_lwm2m_comm.py`)

LwM2M 표준 TLV(Type-Length-Value) 바이너리 포맷으로 인코딩:

```
Object 13432 JSON
        │
        ▼
TLV 인코딩:
    각 리소스 = [타입 바이트] + [ID 필드] + [길이 필드] + [값]
    멀티 인스턴스 = 중첩 TLV
        │
        ▼
바이너리 페이로드: b'\xc8\x00\x2f...'
```

---

#### 이전 전송 채널 (3가지)

#### 채널 A: CoAP NOTIFY (기본)

```
LwM2M 서버가 /13432/* 리소스에 observe 등록
        │
        ▼
데이터 갱신 시 → NOTIFY 패킷 전송:
    프로토콜: CoAP over UDP (포트 5683)
    라이브러리: aiocoap
    페이로드: TLV 인코딩된 바이너리
    주기: 10분마다 또는 루프 완료 시
```

#### 채널 B: LwM2M READ 응답

```
서버가 GET /13432 요청 전송
        │
        ▼
data.json에서 현재 상태 읽어 TLV로 응답
```

#### 채널 C: UART 시리얼 (IP 없는 장치용)

```
/dev/ttyUSB0 수신 대기
        │
특정 요청 패킷 감지:
    0x48 0x01 0xd1 0xf1 0xa4 ...
        │
        ▼
응답 패킷 전송:
    [헤더] + 0xFF + [TLV 페이로드]
```

---

#### 이전 결과 저장 흐름 전체

```
DeepStream Probe 2                    LwM2M 워치독 스레드
(GStreamer thread)                    (asyncio daemon thread)
        │                                     │
        │ frame 완료 시마다                     │
        ▼                                     │
_streams[i]['counts'] 업데이트                │
_streams[i]['track_state'] 업데이트           │
        │                                     │
        │ 루프 완료 시                          │
        ▼                                     │
_finalize_stream_cycle()                      │
        │                                     │
        ├─ latest_loop_result 스냅샷           │
        ├─ /result_logs/*.json 저장            │
        └─ _st['new_result'] = True ──────────┤
                                              │
                                     _st['new_result'] 감지
                                              │
                                     build_13432_from_stats()
                                              │
                                     TLV 인코딩
                                              │
                                     ├─ CoAP NOTIFY
                                     ├─ data.json 업데이트
                                     └─ UART 응답 (if connected)
```

---

## 10. 핵심 데이터 구조

### 데이터 변환 여정 요약

```
[카메라/파일]
    NVMM NV12 raw frame
        │ nvstreammux (letterbox, batch)
        ▼
    NVMM NV12 (고정 해상도, 배치)
        │ nvinfer (TensorRT)
        ▼
    output0 tensor [batch, 8400, 5+classes] float32
        │ Probe 1: yolo_postprocess + letterbox 역변환
        ▼
    NvDsObjectMeta (x1,y1,x2,y2, class_id, confidence) × N개
        │ nvtracker (선택 프로파일)
        ▼
    NvDsObjectMeta + object_id (퍼시스턴트 트랙 ID)
        │ Probe 2: _do_counting
        ▼
    track_state[id] = {bev_y_hist, speed_samples, line_crossings}
    counts[cls][dir] = int
        │ _finalize_stream_cycle
        ▼
    확정 ROI 통과 이벤트
        │ modbus_bridge.py (비차단 큐)
        ▼
    modbus_daemon.py (중복 제거 + 누적 영속화)
        │ ds_modbus_rtu_comm.py
        ▼
    Modbus register map → /dev/ttyUSB0
        │
        ▼
    result_logs/*.json (영구 저장)
```

---

## 11. 전체 흐름 요약 테이블

| 단계 | 컴포넌트 | 입력 | 처리 | 출력 |
|---|---|---|---|---|
| **캡처** | v4l2src / uridecodebin | 카메라 신호 / 파일 | 디코딩 | NVMM NV12 |
| **멀티플렉싱** | nvstreammux | 다중 스트림 NVMM | 배치 + letterbox 패딩 | NVMM NV12 (고정 해상도) |
| **추론** | nvinfer (YOLO11n TRT) | NVMM NV12 | TensorRT 전방향 패스 | output0 텐서 (float32) |
| **텐서 파싱** | Probe 1 (_infer_tensor_probe) | output0 텐서 | YOLO 디코딩 + NMS + 좌표 역변환 | NvDsObjectMeta 리스트 |
| **트래킹** | nvtracker (IOU/NvSORT/NvDCF) | NvDsObjectMeta (ID 없음) | 선택 프로파일 기반 객체 연관 | NvDsObjectMeta + object_id |
| **카운팅/속도** | Probe 2 (_osd_counting_probe) | 트래킹된 오브젝트 | 라인 교차 + BEV 속도 + 카운팅 | counts, track_state, speed_stat |
| **렌더링** | nvdsosd | ObjectMeta + DisplayMeta | CPU 모드 bbox/텍스트 렌더링 | RGBA 오버레이 프레임 |
| **인코딩** | jpegenc / 선택적 H.264 encoder | RGBA 프레임 | 전시 기본 JPEG, opt-in H.264 | JPEG 또는 RTP/H.264 프레임 |
| **스트리밍** | `/stream.mjpg` / 선택적 webrtcbin | 인코딩 프레임 | localhost MJPEG, opt-in WebRTC | 이중 canvas 실시간 영상 |
| **스냅샷** | _finalize_stream_cycle | counts, speed_stat | Python dict 직렬화 | latest_loop_result |
| **저장** | JSON writer | latest_loop_result | json.dump | /result_logs/*.json |
| **이벤트 브리지** | modbus_bridge.py | class, direction, speed | 비차단 큐 + 재시도 + event_id | localhost `/event` |
| **누적/영속화** | modbus_daemon.py | ROI 이벤트 | 중복 제거 + 실시간/누적 통계 | `run/modbus_state.json` |
| **전송** | ds_modbus_rtu_comm.py | register map | FC03 조회 / FC16 설정·제어 | `/dev/ttyUSB0` Modbus RTU |

---

*최종 현행화: 2026-10-06*
