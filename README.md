# ds_camera Jetson 실행 가이드

## 포트폴리오 공개 범위와 모델 안내

Jetson 기반 영상 분석, 프로세스 분리, ROI·차량 카운팅 및 Modbus RTU 연동의
구현 경험을 소개하기 위한 개인 포트폴리오 저장소입니다.

**회사에서 실제 사용하는 모델·학습 가중치·학습 데이터는 포함하지 않습니다.**
별도로 변환·검증한 공개 기본 YOLO11n(COCO 80 클래스)은 참고용 모델이며,
회사에서 실제 사용하는 운영 모델이 아닙니다. 해당 모델 바이너리도 이 저장소에는 올리지 않습니다.

현재 소스의 추론 설정은 별도 5 클래스 모델을 기준으로 합니다. 기본 YOLO11n 엔진을
이름만 바꾸어 대체하는 방식은 지원하지 않으며, 모델을 변경하려면 클래스 라벨·출력 해석·
차량 분류 설정을 함께 맞춰야 합니다. 모델 없이 전체 추론 기능이 바로 실행되지는 않습니다.
모델 배치 경로는 [models/README.md](models/README.md)를 참고하세요.

실제 장비의 ROI·운영 상태, 로그, 가상환경, 테스트 영상, 내부 프로토콜 정의서와 기존 회사 Git 이력은 제외했습니다.
이 저장소의 이력은 개인 공개본부터 새로 관리합니다.

이 프로젝트는 Jetson에서 Flask 웹 UI로 카메라/테스트 영상 ROI를 설정하고,
DeepStream으로 차량 탐지, 속도 계산, 카운팅을 수행하며 결과를 Modbus RTU로
제공합니다. LwM2M/외부 DB 전송은 이 배포본에 포함하지 않습니다.

문서 역할은 구분되어 있습니다. `ARCHITECTURE.md`는 프로세스와 상태 소유권,
`PIPELINE.md`는 프레임·이벤트의 세부 흐름, `USAGE.md`는 현장 운영 절차를
설명합니다.

## 현재 구조

기본 실행은 UI와 추론을 분리한 split 모드입니다.

```text
로컬 PyQt/브라우저
  ↓ http://127.0.0.1:5022 (systemd 배포 기본값)
web_ui.py
  - 사용자가 접속하는 Flask UI/proxy
  - GStreamer, pyds를 직접 import하지 않음

infer_daemon.py
  - 내부 추론 데몬
  - 카메라, DeepStream, ROI, 카운팅, 로컬 WebRTC 미리보기 담당
  - WebRTC 연결 불가 시 localhost MJPEG로 자동 대체
  - 기본 내부 주소: http://127.0.0.1:5122

modbus_daemon.py
  - /dev/ttyUSB0를 단독 소유하는 Modbus RTU 데몬
  - 설정 가능한 실시간 윈도우/누적 레지스터와 재시작·재부팅 명령 담당
  - 추론 재시작 중에도 상태 2로 계속 응답

watchdog.py
  - infer_daemon.py 상태 감시
  - 추론 프레임 진행이 멈추면 /stop -> /start 복구 시도
  - 필요 시 추론 데몬 재시작
```

이 구조의 장점:

- 웹 UI가 재시작되어도 추론 데몬은 유지됩니다.
- 브라우저를 닫아도 추론은 계속 돌 수 있습니다.
- DeepStream 파이프라인이 멈춘 경우 watchdog이 자동 복구를 시도합니다.
- systemd로 등록하면 부팅 자동 실행과 프로세스 자동 재시작이 가능합니다.
- UI 문제와 추론 문제를 분리해서 장애 대응이 쉬워집니다.

## 폴더 설명

| 폴더 | 설명 |
|---|---|
| `ds/` | DeepStream 추론 런타임입니다. Flask 라우트, GStreamer 파이프라인, ROI, 카운팅, 로컬 WebRTC/MJPEG 미리보기, Modbus 이벤트 연동이 여기서 동작합니다. |
| `ds/parts/` | `ds/runtime.py`가 순서대로 로드하는 기능별 파트 파일입니다. 파이프라인 관련 핵심 코드는 이 폴더를 보면 됩니다. |
| `ds/test_videos/` | 웹 UI에서 선택하는 테스트 영상 보관 폴더입니다. |
| `templates/` | Flask 웹 UI 템플릿입니다. 현재 실제 화면은 `bench.html` 하나만 사용합니다. |
| `deepstream/` | nvinfer, nvtracker, label 설정 파일입니다. DeepStream 플러그인이 직접 읽는 설정입니다. |
| `models/` | TensorRT 엔진 파일 보관 폴더입니다. 현재 `traffic_infer_ds.engine`을 사용합니다. |
| `result_logs/` | 루프 완료/집계 결과 JSON 저장 폴더입니다. 비어 있어도 런타임에서 사용하는 폴더라 유지합니다. |
| `deepstream_python_apps/` | NVIDIA DeepStream Python binding 소스만 남긴 축소 폴더입니다. 샘플 앱/문서/테스트는 제거했습니다. |
| `drivers/ch341/` | Jetson `5.15.148-tegra`용 CH340/CH341 USB-RS485 드라이버 소스와 설치 스크립트입니다. |
| `.venv/` | 로컬 Python 가상환경입니다. 배포 산출물이 아니라 장비별 실행 환경입니다. |

## 주요 파일 설명

| 파일 | 설명 |
|---|---|
| `app.py` | 기존 단일 프로세스 모드 진입점입니다. `DS_CAMERA_RUN_MODE=monolith`일 때 사용합니다. |
| `infer_daemon.py` | 내부 추론 데몬 실행 파일입니다. DeepStream 런타임을 127.0.0.1:5122에 띄웁니다. |
| `modbus_daemon.py` | 직렬 포트와 Modbus 레지스터/누적 상태를 추론 프로세스와 독립적으로 관리합니다. |
| `modbus_bridge.py` | 확정된 ROI 통과 이벤트를 추론 probe를 막지 않고 modbusd로 전달합니다. |
| `gpio_pulse.py` | 확정된 ROI 통과 이벤트마다 물리 핀 29(GPIO01/PQ.05)에 비동기 100ms 펄스를 출력합니다. |
| `gpio_install.sh` | GPIO 평가 장비에서만 BOARD 29 출력용 Jetson device-tree overlay를 설치합니다. 실행 후 재부팅이 필요합니다. |
| `web_ui.py` | 사용자가 접속하는 Flask proxy UI입니다. DeepStream을 직접 import하지 않습니다. |
| `watchdog.py` | 추론 프레임 진행 상태와 데몬 응답을 감시하고, 멈춤이 감지되면 복구를 시도합니다. 통신 대상 TCP 체크는 선택적으로 로그만 남깁니다. |
| `deploy.sh` | 최초 의존성 설치, systemd 등록, 바탕화면 아이콘 설치와 서비스 시작을 한 번에 수행합니다. |
| `jetsonctl.sh` | 설치 후 start/stop/restart/status/logs/ui 명령을 한곳에서 제공합니다. |
| `install_systemd.sh` | `deploy.sh`가 내부에서 사용하는 systemd 및 바탕화면 런처 설치 파일입니다. |
| `setup.sh` | Jetson 실행 패키지, venv, Python 의존성, GStreamer/DeepStream 점검을 수행합니다. Python 패키지 목록도 이 파일 안에 있습니다. |
| `repair_packages.sh` | 중단된 Ubuntu/PackageKit 업데이트를 `dpkg --configure -a`와 의존성 복구로 완료합니다. |
| `drivers/ch341/install.sh` | CH340 `1a86:7523` 어댑터를 사용할 때 공식 Linux 5.15.148 CH341 모듈을 빌드·설치합니다. |
| `setting_preset.json` | ROI, 채널별 conf 등 UI 설정 프리셋 저장 파일입니다. |
| `USAGE.md` | 실행, 중지, 재시작, 초기화, 장애 대응을 정리한 운영용 사용법입니다. |
| `PIPELINE.md` | DeepStream 파이프라인 상세 문서입니다. 파이프라인 동작을 볼 때는 이 파일을 먼저 보면 됩니다. |

## DeepStream 핵심 파일

| 파일 | 설명 |
|---|---|
| `ds/runtime.py` | `ds/parts/*.py`를 하나의 공유 네임스페이스에 순서대로 로드합니다. GStreamer probe와 Flask route가 전역 상태를 공유하기 위한 구조입니다. |
| `ds/parts/00_bootstrap.py` | Flask 앱 생성, 경로/모델/트래커/카메라 기본값, Jetson 리소스 측정 함수를 정의합니다. |
| `ds/parts/10_common_state.py` | `_st`, `_streams`, 카운트/속도/통계 공용 상태와 저장 함수를 관리합니다. 탐지 이벤트는 디스크에 저장하지 않고 스트림 메모리에만 최신 50건 유지합니다. |
| `ds/parts/20_media_yolo.py` | 테스트 영상 메타 추출, 썸네일, 영상 정규화, YOLO 텐서 후처리/NMS 유틸리티입니다. |
| `ds/parts/40_speed_counting.py` | ROI 라인 통과 판정, 방향별 카운팅, BEV 기반 속도 계산, 탐지로그 이벤트 생성 담당입니다. |
| `ds/parts/50_state_config.py` | 실행 상태, 로컬 WebRTC/MJPEG 상태, 출력 on/off valve, nvinfer 설정 파일 생성 로직입니다. |
| `ds/parts/55_comm_watchdog.py` | inferd의 Modbus 이벤트 전달, modbusd 상태 조회와 DeepStream 프레임 진행 감시를 담당합니다. |
| `ds/parts/60_probes_sources.py` | GStreamer source bin 생성, 카메라 MJPEG 프리뷰, nvinfer/nvtracker probe 처리입니다. |
| `ds/parts/70_pipeline.py` | DeepStream 파이프라인을 실제로 빌드하고 실행합니다. OSD 뒤에서 추론 경로와 분리된 WebRTC 출력 및 요청 시에만 켜지는 localhost MJPEG 대체 경로를 구성합니다. |
| `ds/parts/80_routes.py` | `/start`, `/stop`, `/status`, `/set_roi`, `/connect_camera`, `/recent_detections` 같은 Flask API 라우트입니다. |
| `ds/parts/90_lifecycle.py` | 종료 시 임시 파일과 백그라운드 루프를 정리하고 Flask 서버를 실행합니다. |
| `deepstream/config_infer_primary.txt` | YOLO TensorRT 엔진을 nvinfer에 연결하는 기준 템플릿입니다. 실제 실행 시에는 `ds/parts/50_state_config.py`가 같은 내용을 `/tmp/nvinfer_cfg_*.txt`로 동적 생성합니다. |
| `deepstream/config_tracker_NvSORT.yml` | 기본값인 Kalman/SORT 기반 성능 균형형 트래커 설정입니다. |
| `deepstream/config_tracker_IOU.yml` | 가장 가벼운 IOU 트래커 설정입니다. |
| `deepstream/config_tracker_NvDCF_counting.yml` | 오클루전과 ID 유지에 초점을 둔 카운팅용 NvDCF 설정입니다. |
| `deepstream/config_tracker_NvDCF_accuracy.yml` | 시각 특징을 강화한 정확도 우선 NvDCF 설정입니다. |
| `deepstream/labels.txt` | 클래스 라벨 목록입니다. |

새 설치 또는 PeopleNet 저장값이 없는 장비에서는 Traffic 모델과 함께 PeopleNet이
기본 활성화됩니다. 사용자가 WebUI에서 PeopleNet을 끄고 추론을 시작하면 그 선택은
저장되며 재부팅 복원과 watchdog 재시작에서도 그대로 유지됩니다.

## 정리된 항목

- 실제 라우트에서 쓰지 않던 `templates/index.html`, `templates/result.html`을 제거했습니다.
- 전시 기본 미리보기는 QtWebEngine의 native video surface를 만들지 않는 localhost MJPEG입니다. WebRTC는 `DS_CAMERA_UI_PREVIEW_TRANSPORT=webrtc`로 명시할 때만 사용합니다.
- 수신 프레임은 DOM 밖의 메모리 canvas에서 완성한 뒤 고정된 표시 canvas에 복사합니다. 화면 레이어 순서를 프레임마다 바꾸지 않으며, 새 프레임이 늦으면 마지막 완성 프레임을 유지합니다.
- 로컬 UI는 Chromium GPU 옵션뿐 아니라 Qt 창 합성도 `QT_QUICK_BACKEND=software`로 설정합니다. 이는 영상 버퍼와 별도의 전체 창 표시 경로이며, 추론 GPU 사용에는 영향을 주지 않습니다. 설정은 UI 프로세스를 다시 실행해야 적용됩니다.
- ROI 카운트는 ID당 한 번이 아니라 두 탐지선을 완전히 통과한 회차마다 기록합니다. 같은 ID의 왕복은 방향별로 각각 집계하며, 진입한 선으로 되돌아오면 해당 시도는 취소합니다. 속도는 회차마다 새로 측정합니다.
- 운영 UI에서 막혀 있던 이미지 업로드/이미지 추론 코드를 제거했습니다.
- 쓰지 않던 `static/`, `network/`, `result_images/`, `deepstream/engines/` 빈 폴더 생성을 제거했습니다.
- Python `__pycache__`, `*.pyc` 캐시 파일을 정리했습니다.

## 설치와 운영

### 실시간 윈도우 길이 변경

`operation_settings.json`의 **`analytics.realtime_window_ms` 한 값만** 수정합니다.
단위는 밀리초이며, 최소 50ms이고 기본값은 500ms입니다.

```json
"analytics": {
  "window_seconds": 600,
  "realtime_window_ms": 500
}
```

추론 분석·내부 실시간 필터·Modbus 실시간 레지스터·UI 표시가 이 설정을 공유합니다.
설정 읽기 로직은 `modbus_daemon.py`에 모아 두고 추론 쪽에서도 같은 클래스를
import합니다. import만으로 데몬이나 시리얼 통신이 실행되지는 않습니다.
실행 중인 데몬은 약 0.5초 주기로 파일을 다시 읽으므로 이후 값 변경에는 재시작이
필요 없습니다. UI는 다음 데이터 조회에서 반영합니다. 저장 중 JSON이 불완전하거나
값이 잘못되면 마지막 정상 값을 유지하고 경고를 남깁니다.

이 기능을 처음 배포할 때만 두 데몬을 재시작해야 합니다. 예전 환경변수
`DS_CAMERA_REALTIME_WINDOW_SECONDS`는 더 이상 사용하지 않으므로 서비스별 값이
서로 달라지는 것을 막습니다. `window_seconds: 600`은 별도 집계 설정입니다.
윈도우 확대 시 이미 제거한 과거 이벤트는 복구되지 않으며, 분석은 새 윈도우 길이만큼
워밍업합니다. 누적 통행량은 초기화하지 않습니다.

### 설치 및 업데이트

최초 설치 또는 새 버전 배포는 한 명령으로 처리합니다.

```bash
cd /home/jetson/jetson_ai_modbus_rtu
./deploy.sh
```

설치 후에는 systemd가 Modbus, 추론, watchdog을 부팅 시 자동으로 시작하고
비정상 종료도 자동 복구합니다. 운영 명령은 `jetsonctl.sh` 하나만 사용합니다.

```bash
./jetsonctl.sh start
./jetsonctl.sh stop
./jetsonctl.sh restart
./jetsonctl.sh status
./jetsonctl.sh logs infer
./jetsonctl.sh ui
```

`ui`는 `127.0.0.1`에 임시 WebUI를 띄우고 PyQt 창을 엽니다. 창을 닫으면 임시
WebUI도 종료되지만 추론·카운팅·Modbus·watchdog은 계속 동작합니다. 바탕화면
아이콘도 같은 `jetsonctl.sh ui` 명령을 사용합니다.

Watchdog 설정을 바꿔야 하면 systemd unit의 Environment override로 관리하고
`./jetsonctl.sh restart`로 반영합니다.

## Modbus RTU 통신

통신 모드는 `modbus_rtu`로 고정됩니다. 독립 `modbus_daemon.py`가
`/dev/ttyUSB0`를 소유하고 FC03 조회, FC16 설정·초기화, 방향별 누적 카운트와
장치 제어 명령을 처리합니다. inferd가 재시작되는 동안에도 modbusd는 별도
프로세스로 유지됩니다.

기본 통신값과 Windows Master 메뉴는 [USAGE.md](USAGE.md)를 참고합니다.

## 소프트웨어 프로그램 형태

Jetson에서는 Electron 같은 데스크톱 앱보다 다음 구조를 권장합니다.

```text
systemd 서비스
  - jetson-ai-modbus-rtu-inferd
  - jetson-ai-modbus-rtu-watchdog

화면 실행
  - 바탕화면 런처 또는 `./jetsonctl.sh ui`
  - 로컬 PyQt 창이 열린 동안만 127.0.0.1:5022 사용
```

이 방식이 가볍고, 추론 리소스를 가장 덜 방해하며, 장애 복구도 쉽습니다.
