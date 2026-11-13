# setup.sh 설치 패키지 정리

이 문서는 `/home/jetson/jetson_ai_modbus_rtu/setup.sh` 실행 시 설치되거나 확인되는 패키지와 라이브러리를 정리한 문서임.

`setup.sh`는 Jetson Orin Nano 같은 Jetson aarch64 환경에서 `ds_camera`를 실행하기 위한 Ubuntu 패키지, GStreamer/DeepStream 관련 패키지, QtWebEngine 로컬 웹뷰 패키지, Python 가상환경 패키지, `pyds` 바인딩을 준비함.

---

## 1. 실행 전 기본 동작

`setup.sh`는 아래 기본값을 사용함.

| 항목 | 기본값 | 설명 |
|---|---|---|
| `PYTHON_BIN` | `python3` | venv 생성에 사용할 Python 실행 파일 |
| `VENV_DIR` | `$APP_DIR/.venv` | Python 가상환경 경로 |
| `INSTALL_DEEPSTREAM` | `auto` | DeepStream plugin이 없으면 apt 패키지 설치 시도 |
| `INSTALL_PYDS` | `auto` | `pyds` import 실패 시 wheel 설치 또는 source build 시도 |
| `RUN_CHECKS` | `1` | 설치 후 Python/GStreamer/pyds 검사 수행 |

또한 실행 초기에 아래를 확인함.

| 확인 항목 | 설명 |
|---|---|
| `uname -m` | `aarch64`가 아니면 Jetson 환경이 아닐 수 있다고 경고함 |
| `/etc/nv_tegra_release` | JetPack/L4T 이미지 여부 확인용 파일임 |
| `sudo` | apt 설치를 위해 필요함 |
| `python3` | venv 생성을 위해 필요함 |

이전 `apt` 작업이 중단되어 `dpkg --audit`에 미완료 패키지가 나타나면 새 패키지
설치 전에 `dpkg --configure -a`와 `apt-get -f install`을 자동 실행함. 따라서
디스크의 코드는 최신이지만 서비스 재시작 단계까지 도달하지 못하는 부분 배포
상태를 방지함.

### 중단된 업데이트 수동 복구

Ubuntu Software Updater/PackageKit 또는 `apt` 작업이 중간에 종료되어 패키지가
`iU`(unpacked, not configured) 상태로 남으면 아이콘 로딩, NetworkManager 표시,
다음 패키지 설치가 비정상 동작할 수 있습니다. 다음 스크립트로 복구합니다.

```bash
cd /home/jetson/jetson_ai_modbus_rtu
./repair_packages.sh
```

스크립트가 실행하는 핵심 명령은 다음과 같습니다.

```bash
sudo dpkg --configure -a
sudo apt-get -y --fix-broken install
sudo dpkg --audit
```

복구 과정에서 NetworkManager나 GNOME Shell 관련 패키지가 다시 설정되면
`Object does not exist at path /org/freedesktop/NetworkManager` 같은 DBus 메시지가
일시적으로 나타날 수 있습니다. NetworkManager가 다시 올라온 뒤 연결 상태를
확인하고, `/var/run/reboot-required`가 있으면 현장 데이터를 저장한 후 재부팅합니다.

---

## 2. Ubuntu apt 패키지

`setup.sh`는 먼저 `sudo apt-get update`를 한 번 실행한 뒤 아래 패키지를 설치함.

### 2-1. 기본 시스템/저장소 유틸리티

| 패키지 | 용도 |
|---|---|
| `ca-certificates` | HTTPS 인증서 처리 |
| `curl` | 파일 다운로드, pyds wheel 다운로드 fallback |
| `gnupg` | apt repository key 처리 |
| `lsb-release` | Ubuntu 배포판 정보 확인 |

### 2-2. Python 개발/실행 패키지

| 패키지 | 용도 |
|---|---|
| `python3` | Python 실행 환경 |
| `python3-pip` | pip 패키지 설치 |
| `python3-venv` | `.venv` 가상환경 생성 |
| `python3-dev` | Python C extension 빌드용 헤더 |
| `python3-gi` | Python GObject introspection |
| `python3-gi-cairo` | Python Cairo/GI 연동 |
| `python3-gst-1.0` | Python에서 GStreamer 사용 |
| `python-gi-dev` | GI 개발 헤더 |

### 2-3. PyQt5/QtWebEngine 패키지

`/home/jetson/jetson_ai_modbus_rtu/webview.py`는 PyQt5 QWebEngine으로 `http://localhost:5022`를 여는 로컬 클라이언트임.
이 파일 실행을 위해 아래 패키지를 설치함.

| 패키지 | 용도 |
|---|---|
| `python3-pyqt5` | PyQt5 기본 위젯/QtCore/QtWidgets |
| `python3-pyqt5.qtwebengine` | `QWebEngineView`, `QWebEngineProfile`, `QtWebEngineCore` |
| `python3-pyqt5.qtwebchannel` | Qt WebChannel 연동 라이브러리 |

### 2-4. GStreamer GI 패키지

| 패키지 | 용도 |
|---|---|
| `gir1.2-gstreamer-1.0` | GStreamer GI typelib |
| `gir1.2-gst-plugins-base-1.0` | GStreamer base plugin GI typelib |
| `gir1.2-gst-plugins-bad-1.0` | GStreamer bad plugin GI typelib |

### 2-5. GStreamer 런타임 플러그인

| 패키지 | 용도 |
|---|---|
| `gstreamer1.0-tools` | `gst-inspect-1.0`, `gst-launch-1.0` 등 도구 |
| `gstreamer1.0-plugins-base` | 기본 GStreamer plugin |
| `gstreamer1.0-plugins-good` | 일반적으로 안정적인 plugin |
| `gstreamer1.0-plugins-bad` | 추가 영상 plugin 포함 |
| `gstreamer1.0-plugins-ugly` | 추가 codec/plugin |
| `gstreamer1.0-libav` | FFmpeg/libav 기반 codec |
| `gstreamer1.0-alsa` | ALSA audio plugin |

### 2-6. 영상/카메라 도구

| 패키지 | 용도 |
|---|---|
| `v4l-utils` | `/dev/video*` 카메라 확인 및 V4L2 도구 |
| `ffmpeg` | 영상 변환, 썸네일/프레임 추출, 메타 처리 |

### 2-7. 빌드 도구

| 패키지 | 용도 |
|---|---|
| `build-essential` | gcc/g++/make 등 기본 빌드 도구 |
| `cmake` | C/C++ 프로젝트 빌드 설정 |
| `ninja-build` | 빠른 빌드 backend |
| `meson` | 일부 라이브러리 빌드 시스템 |
| `git` | 소스 관리/외부 소스 사용 |
| `pkg-config` | C/C++ library compile/link flag 조회 |

### 2-7-1. CH340/CH341 커널 모듈

`setup.sh` 단독 실행은 커널 모듈을 변경하지 않지만, 기본 `deploy.sh`는 CH340
USB-RS485 어댑터 `1a86:7523`용 모듈을 빌드·설치합니다.

```bash
./deploy.sh
```

CH341 모듈 설치를 건너뛰려면 다음처럼 실행합니다.

```bash
INSTALL_CH341=0 ./deploy.sh
```

드라이버만 별도로 설치하는 명령은 `./drivers/ch341/install.sh`입니다.

번들 소스를 빌드하려면 실행 중인 커널과 일치하는 NVIDIA L4T 커널 헤더 및
`Module.symvers`, `build-essential`이 필요합니다. 설치 스크립트가 이를 먼저
검사하며 전체 커널 이미지나 부트 파티션은 변경하지 않습니다.

### 2-8. 개발 헤더/라이브러리

| 패키지 | 용도 |
|---|---|
| `libglib2.0-dev` | GLib 개발 헤더 |
| `libglib2.0-dev-bin` | GLib 개발 도구 |
| `libgstreamer1.0-dev` | GStreamer 개발 헤더 |
| `libgstreamer-plugins-base1.0-dev` | GStreamer base plugin 개발 헤더 |
| `libcairo2-dev` | Cairo 그래픽 개발 헤더 |
| `libgirepository1.0-dev` | GObject introspection 개발 헤더 |
| `libjson-glib-dev` | JSON-GLib 개발 헤더 |

---

## 3. DeepStream SDK apt 패키지

`setup.sh`는 먼저 아래 명령으로 DeepStream GStreamer plugin 존재 여부를 확인함.

```bash
gst-inspect-1.0 nvstreammux
```

`nvstreammux`가 없고 `INSTALL_DEEPSTREAM`이 `no`가 아니면, apt repository에서 아래 후보 중 사용 가능한 패키지를 찾아 설치함.

| 우선순위 | 후보 패키지 |
|---:|---|
| 1 | `deepstream-7.1` |
| 2 | `deepstream-7.0` |
| 3 | `deepstream-6.4` |
| 4 | `nvidia-deepstream` |

위 패키지가 apt repository에 없으면 자동 설치하지 못하고, DeepStream SDK를 JetPack 버전에 맞게 수동 설치하라는 경고를 출력함.

필요한 핵심 DeepStream plugin은 아래와 같음.

| Plugin | 역할 |
|---|---|
| `nvstreammux` | 다중 입력 batch 구성 |
| `nvinfer` | TensorRT 기반 추론 |
| `nvtracker` | 객체 추적 |
| `nvdsosd` | bbox/text/ROI OSD 렌더링 |
| `nvvideoconvert` | NVIDIA video format 변환 |

---

## 4. 생성되는 애플리케이션 폴더

아래 폴더를 생성함.

| 폴더 | 용도 |
|---|---|
| `ds/test_videos` | 테스트 영상 보관 |
| `result_logs` | 추론/집계 결과 저장 |
| `logs` | daemon/watchdog 로그 저장 |
| `run` | pid 파일 저장 |

---

## 5. Python venv 생성

아래 명령 구조로 가상환경을 생성함.

```bash
python3 -m venv --system-site-packages .venv
```

`--system-site-packages`를 사용하는 이유는 Jetson/DeepStream 환경의 시스템 Python 패키지, 특히 GI/GStreamer 계열과 `pyds` 접근 가능성을 높이기 위함임.

venv 생성 후 아래 Python packaging 도구를 업그레이드함.

| pip 패키지 | 용도 |
|---|---|
| `pip` | Python 패키지 설치 도구 |
| `setuptools` | Python package build/install 도구 |
| `wheel` | wheel 패키지 처리 |

---

## 6. Python pip 패키지

`setup.sh` 내부 `PY_PIP_PACKAGES` 배열 기준으로 아래 패키지를 설치함.

| pip 패키지 | 버전 조건 | 용도 |
|---|---|---|
| `Flask` | `>=2.2` | 웹 UI/API 서버 |
| `numpy` | `<2` | YOLO 후처리, 좌표/배열 계산 |
| `psutil` | `>=5.9` | CPU/메모리 사용률 측정 |
| `Pillow` | `>=9` | 썸네일/이미지 처리 |
| `aiocoap` | `>=0.4` | LwM2M/CoAP 통신 |

설치 명령은 아래 형태임.

```bash
.venv/bin/python -m pip install "Flask>=2.2" "numpy<2" "psutil>=5.9" "Pillow>=9" "aiocoap>=0.4"
```

---

## 7. Python GI/GStreamer 확인

pip 설치 후 venv 안에서 아래 import를 확인함.

```python
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)
```

이 확인에 실패하면 `python3-gi`, `python3-gst-1.0` 설치 상태를 확인하라는 에러와 함께 setup이 중단됨.

---

## 8. pyds 설치/확인

`pyds`는 DeepStream Python binding임.  
`setup.sh`는 먼저 아래 import를 시도함.

```python
import pyds
```

이미 import 가능하면 추가 설치하지 않음.

### 8-1. pyds가 없을 때 pre-built wheel 설치 시도

`pyds` import가 실패하고 `INSTALL_PYDS`가 `no`가 아니면, 설치된 DeepStream 버전을 확인한 뒤 GitHub release wheel 다운로드를 시도함.

DeepStream 버전과 pyds release tag 매핑은 아래와 같음.

| DeepStream 버전 | pyds release tag |
|---|---|
| `7.1` | `v1.2.0` |
| `7.0` | `v1.1.11` |
| `6.4` | `v1.1.10` |

wheel 파일명은 Python 버전에 맞춰 아래 형태로 구성됨.

```text
pyds-<version>-cp<python_major_minor>-cp<python_major_minor>-linux_aarch64.whl
```

예시:

```text
pyds-1.2.0-cp310-cp310-linux_aarch64.whl
```

다운로드 도구는 아래 순서로 사용함.

| 도구 | 조건 |
|---|---|
| `wget` | 시스템에 있으면 우선 사용 |
| `curl` | `wget`이 없으면 사용 |

`setup.sh`는 `curl`을 apt로 설치하지만 `wget`은 별도로 설치하지 않음.

### 8-2. pyds source build fallback

pre-built wheel 매핑을 찾지 못한 경우, Python 3.12 이상이면 bundled source build를 시도할 수 있음.

조건은 아래와 같음.

| 조건 | 설명 |
|---|---|
| Python 버전 | `3.12` 이상 |
| source 경로 | `deepstream_python_apps/bindings` |
| pybind11 header | `deepstream_python_apps/bindings/3rdparty/pybind11/include/pybind11/pybind11.h` 존재 필요 |
| DeepStream 경로 | `/opt/nvidia/deepstream/deepstream*` 후보 중 존재하는 경로 |

현재 Python이 3.12 미만이거나 source build 준비물이 없으면, 맞는 pyds wheel을 수동 설치하라는 경고를 출력함.

---

## 9. 실행 스크립트 권한 설정

아래 파일에 실행 권한을 부여함.

| 파일 | 역할 |
|---|---|
| `jetsonctl.sh` | 설치 후 서비스와 로컬 UI 통합 관리 |
| `setup.sh` | 설치 스크립트 |
| `install_systemd.sh` | systemd 서비스 설치 |

---

## 10. 설치 후 환경 검사

`RUN_CHECKS=1`이면 아래 검사를 수행함.

### 10-1. Python 패키지 import 검사

아래 모듈 import를 확인함.

| 모듈 | 대응 패키지 |
|---|---|
| `flask` | `Flask` |
| `numpy` | `numpy` |
| `psutil` | `psutil` |
| `PIL` | `Pillow` |
| `aiocoap` | `aiocoap` |
| `gi` | `python3-gi` |

### 10-2. PyQt5/QtWebEngine import 검사

상위 경로의 `webview.py`가 있으면 아래 import를 확인함.

```python
from PyQt5.QtCore import QUrl
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineProfile
from PyQt5.QtWebEngineCore import QWebEngineUrlRequestInterceptor
```

이 검사는 `webview.py`가 필요한 PyQt5/QtWebEngine 패키지가 설치되어 있는지 확인하기 위한 것임.

### 10-3. GStreamer element 검사

아래 element가 GStreamer에서 생성 가능한지 확인함.

| Element | 역할 |
|---|---|
| `v4l2src` | USB/V4L2 카메라 입력 |
| `jpegdec` | MJPEG/JPEG decode |
| `videoconvert` | 일반 video format 변환 |
| `nvstreammux` | DeepStream batch mux |
| `nvinfer` | DeepStream inference |
| `nvtracker` | DeepStream tracker |
| `nvdsosd` | DeepStream OSD |
| `nvvideoconvert` | NVIDIA video convert |
| `jpegenc` | 로컬 미리보기 JPEG 인코딩 |
| `appsink` | JPEG 프레임을 HTTP 미리보기로 전달 |

하나라도 없으면 `missing_gst=...`를 출력하고 setup이 실패함.

### 10-4. pyds import 재검사

최종적으로 아래를 다시 확인함.

```python
import pyds
```

실패하면 DeepStream Python binding이 필요하다는 에러와 함께 setup이 중단됨.

### 10-5. Python 문법 검사

아래 파일을 `py_compile`로 검사함.

| 파일 |
|---|
| `app.py` |
| `web_ui.py` |
| `infer_daemon.py` |
| `watchdog.py` |
| `ds_lwm2m_comm.py` |
| `ds_lwm2m_runtime.py` |
| `ds/runtime.py` |
| `ds/parts/00_bootstrap.py` |
| `ds/parts/10_common_state.py` |
| `ds/parts/20_media_yolo.py` |
| `ds/parts/40_speed_counting.py` |
| `ds/parts/50_state_config.py` |
| `ds/parts/55_comm_watchdog.py` |
| `ds/parts/60_probes_sources.py` |
| `ds/parts/70_pipeline.py` |
| `ds/parts/80_routes.py` |
| `ds/parts/90_lifecycle.py` |
| `/home/jetson/jetson_ai_modbus_rtu/webview.py` |

---

## 11. 설치 후 안내되는 실행 방법

setup이 완료되면 아래 실행 방법을 안내함.

```bash
cd /home/jetson/jetson_ai_modbus_rtu
./jetsonctl.sh start
```

접속 주소는 아래 형식임.

```text
http://<jetson-ip>:${PORT:-5022}
```

---

## 12. 주요 환경변수

setup 완료 메시지에서 안내하는 환경변수는 아래와 같음.

| 환경변수 | 예시 | 설명 |
|---|---|---|
| `PORT` | `5022` | 웹 UI proxy 포트 |
| `CAMERA_DEVICE` | `/dev/video0` | 사용할 카메라 디바이스 |
| `WEBVIEW_FILE` | `/home/jetson/jetson_ai_modbus_rtu/webview.py` | QtWebEngine 로컬 웹뷰 파일 경로 |
| `DS_CAMERA_COMM` | `none` | 테스트 중 LwM2M/DB 통신 비활성화 |
| `DS_CAMERA_COMM` | `lwm2m` | LwM2M/DB 통신 활성화 |

setup 동작을 바꾸는 환경변수는 아래와 같음.

| 환경변수 | 값 | 설명 |
|---|---|---|
| `PYTHON_BIN` | `python3` 등 | venv 생성에 사용할 Python |
| `VENV_DIR` | `.venv` 등 | venv 생성 위치 |
| `INSTALL_DEEPSTREAM` | `auto`, `yes`, `no` | DeepStream plugin이 없을 때 apt 설치 시도 여부 |
| `INSTALL_PYDS` | `auto`, `yes`, `no` | `pyds`가 없을 때 설치 시도 여부 |
| `RUN_CHECKS` | `1`, `0` | 설치 후 검사 실행 여부 |

---

## 13. 요약

`setup.sh`가 준비하는 핵심은 아래 4가지임.

| 구분 | 내용 |
|---|---|
| Ubuntu/GStreamer | 카메라, MJPEG, codec, GStreamer runtime/devel 패키지 설치 |
| QtWebEngine | `webview.py` 실행을 위한 PyQt5/QWebEngine 패키지 설치 |
| DeepStream | `nvstreammux`, `nvinfer`, `nvtracker`, `nvdsosd` 등 plugin 확인 및 조건부 설치 |
| Python | `.venv` 생성 후 Flask, numpy, psutil, Pillow, aiocoap 설치 |
| pyds | DeepStream Python binding 확인, 없으면 wheel 설치 또는 source build 시도 |

---

## 14. 새 SD카드 최초 설치 기준 용량 추정

새 SD카드에 JetPack 이미지를 새로 굽고 `ds_camera/setup.sh`를 처음 실행한다고 가정하면, 추가로 필요한 용량은 **DeepStream SDK가 이미 있느냐 없느냐**에 따라 크게 달라짐.

가장 현실적인 기준은 아래 2가지임.

| 새 SD카드 상태 | setup 후 추가 점유 용량 예상 |
|---|---:|
| JetPack 이미지 + DeepStream 이미 설치됨 | 약 `250~500 MB` |
| JetPack 이미지 + DeepStream 미설치 | 약 `1.0~1.5 GB` |

위 용량은 `ds_camera` 프로젝트 폴더, `.venv`, `setup.sh` apt 패키지, DeepStream 설치분을 포함한 감각치임.  
다운로드 캐시(`/var/cache/apt/archives`)를 정리하지 않으면 일시적으로 수백 MB가 더 남을 수 있음.

### 14-1. 항목별 용량

| 항목 | 대략 용량 | 설명 |
|---|---:|---|
| `ds_camera` 프로젝트 폴더 | 약 `68 MB` | `.venv` 제외 기준, 테스트 영상 약 `52 MB`, TensorRT engine 약 `8.6 MB` 포함 |
| `.venv` | 약 `74 MB` | Flask, numpy, psutil, Pillow, aiocoap 등 설치 후 현재 기준 |
| setup apt 명시 패키지 | 설치 후 약 `159 MB` | `setup.sh`에 직접 적힌 apt 패키지 38개 합산 |
| QtWebEngine 관련 패키지 | 약 `150~250 MB` | `python3-pyqt5`, `python3-pyqt5.qtwebengine`, `libqt5webenginecore5` 등. Desktop 이미지에는 이미 일부 포함될 수 있음 |
| DeepStream 7.1 deb 다운로드 | 약 `603 MB` | `apt-cache show deepstream-7.1` 기준 |
| DeepStream 7.1 설치 후 | 약 `744~747 MB` | `/opt/nvidia/deepstream` 현재 실측 약 `747 MB` |

### 14-2. setup.sh에 명시된 apt 패키지 목록 자체의 크기

apt metadata 기준으로 `setup.sh`에 직접 명시된 apt 패키지 38개만 합산하면 아래 정도임.

| 기준 | 대략 용량 |
|---|---:|
| deb archive 다운로드 크기 | 약 `37 MB` |
| 설치 후 디스크 사용량 | 약 `159 MB` |

이 값은 `setup.sh`에 직접 적힌 패키지만 합산한 값임.  
새 JetPack 이미지에서는 이미 깔려 있는 패키지도 있고, 반대로 dependency/recommended package가 추가로 딸려올 수도 있어서 실제값은 달라질 수 있음.

### 14-3. 주요 apt 패키지별 설치 후 용량

명시 패키지 중 설치 후 용량이 큰 항목은 아래와 같음.

| 패키지 | 다운로드 크기 | 설치 후 용량 |
|---|---:|---:|
| `ffmpeg` | 약 `16.5 MB` | 약 `53.6 MB` |
| `git` | 약 `3.1 MB` | 약 `17.7 MB` |
| `cmake` | 약 `4.4 MB` | 약 `16.9 MB` |
| `libglib2.0-dev` | 약 `1.7 MB` | 약 `10.8 MB` |
| `libgirepository1.0-dev` | 약 `0.8 MB` | 약 `10.7 MB` |
| `gstreamer1.0-plugins-bad` | 약 `2.3 MB` | 약 `7.9 MB` |
| `python3-pip` | 약 `1.2 MB` | 약 `6.8 MB` |
| `libgstreamer1.0-dev` | 약 `0.5 MB` | 약 `5.9 MB` |
| `gstreamer1.0-plugins-good` | 약 `1.8 MB` | 약 `5.9 MB` |
| `libgstreamer-plugins-base1.0-dev` | 약 `0.4 MB` | 약 `5.7 MB` |

### 14-4. QtWebEngine 포함 여부에 따른 차이

`webview.py`를 Jetson 로컬 GUI에서 실행하려면 PyQt5/QtWebEngine이 필요함.

주요 QtWebEngine 관련 패키지 크기는 아래 정도임.

| 패키지 | 다운로드 크기 | 설치 후 용량 |
|---|---:|---:|
| `python3-pyqt5` | 약 `2.5 MB` | 약 `16.2 MB` |
| `python3-pyqt5.qtwebengine` | 약 `0.1 MB` | 약 `0.7 MB` |
| `python3-pyqt5.qtwebchannel` | 약 `0.02 MB` | 약 `0.1 MB` |
| `libqt5webenginecore5` | 약 `37.8 MB` | 약 `119.4 MB` |
| `libqt5quick5` | 약 `1.6 MB` | 약 `5.9 MB` |

Qt 기본 라이브러리까지 새로 딸려오면 실제 추가 용량은 더 커질 수 있음.  
Jetson Desktop 이미지처럼 Qt 계열이 이미 일부 설치된 환경이면 추가 용량은 줄어듦.

### 14-5. DeepStream 포함 여부에 따른 차이

DeepStream은 `setup.sh`의 일반 apt 패키지보다 훨씬 큼.

| 상황 | 대략 용량 |
|---|---:|
| DeepStream이 이미 설치된 JetPack/SD 이미지 | 추가 DeepStream 설치 없음 |
| DeepStream 7.1 deb 다운로드 | 약 `603 MB` |
| DeepStream 7.1 설치 후 점유 | 약 `744~747 MB` |
| CUDA/TensorRT/VPI까지 없는 최소 Ubuntu 환경 | 수 GB 이상 추가될 수 있음 |

따라서 설치 용량을 볼 때 가장 중요한 분기점은 `DeepStream SDK가 이미 설치되어 있는가`임.

### 14-6. Python venv/pip 용량

현재 `.venv` 실제 사용량은 약 `74 MB`임.

이 안에는 아래 pip 패키지와 Python packaging 도구가 포함됨.

| 분류 | 패키지 |
|---|---|
| packaging | `pip`, `setuptools`, `wheel` |
| app dependency | `Flask`, `numpy`, `psutil`, `Pillow`, `aiocoap` |

`pyds` wheel을 새로 설치해야 하는 경우에는 wheel 파일이 추가되지만, 전체 용량 관점에서는 DeepStream SDK보다 훨씬 작음.

### 14-7. 최종 감각치

| 환경 | 예상 추가 용량 |
|---|---:|
| 새 SD카드에 JetPack + DeepStream이 이미 있는 경우 | 약 `250~500 MB` |
| 새 SD카드에 JetPack만 있고 DeepStream이 없는 경우 | 약 `1.1~1.8 GB` |
| JetPack/CUDA/TensorRT도 없는 최소 Ubuntu에 설치하는 경우 | `setup.sh` 범위를 넘어가며 수 GB 이상 가능 |

정확한 추가 설치량은 새 SD카드에서 아래 명령으로 확인 가능함.

```bash
apt-get -s install <setup.sh의 apt 패키지 목록>
```

실제 설치 전에 이 명령의 `Need to get`, `After this operation` 값을 보면 됨.

### 14-8. apt 캐시 정리

설치 후 다운로드 캐시까지 줄이고 싶으면 아래 명령으로 apt archive를 정리할 수 있음.

```bash
sudo apt-get clean
```

캐시를 지워도 설치된 패키지는 삭제되지 않음.
