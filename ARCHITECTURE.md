# jetson_ai_modbus_rtu 아키텍처

NVIDIA 계층 안에서 이 애플리케이션이 어디에 위치하는지와, 실행 프로세스의
장애 경계·상태 소유권을 설명하는 상위 수준 문서임. 세부 프레임 처리 순서는
`PIPELINE.md`, 설치와 운영 명령은 `README.md`와 `USAGE.md`를 참고함.

핵심 요약은 아래와 같음.

> `ds_camera`는 Linux for Tegra 위에서 JetPack SDK와 DeepStream SDK를 사용하는 Python 기반 User APP임.

---

## 0. Notion에서 flowchart 코드 안 보이게 하는 방법

Markdown 자체에는 Mermaid flowchart 코드를 숨기고 그림만 보여주는 표준 기능이 없음.

Notion에서 가장 깔끔한 방법은 아래 중 하나임.

1. Notion Mermaid 코드블록으로 붙여넣기  
   Notion이 Mermaid를 렌더링하면 코드 대신 그림처럼 보임.

2. Mermaid Live Editor 등에서 SVG/PNG로 export 후 이미지로 업로드  
   이 방식이면 코드가 아예 보이지 않음.

3. 코드가 보여도 덜 거슬리게 하려면 Notion Toggle 안에 Mermaid 코드블록을 넣기  
   다만 이 경우 Toggle을 열어야 그림을 볼 수 있음.

이 문서는 우선 Notion Mermaid 렌더링 기준으로 작성함.  
긴 가로 flowchart는 글자가 작아지므로 대부분 세로 방향 `flowchart TB`로 구성함.

---

## 1. 첨부 이미지 기준 전체 계층

```mermaid
flowchart TB
    U["User APPs<br/>사용자가 만든 애플리케이션<br/>현재 프로젝트: ds_camera"]
    DS["DeepStream SDK<br/>영상 AI 파이프라인 프레임워크<br/>GStreamer / pyds / nv* plugins"]
    JP["JetPack SDK<br/>TensorRT / CUDA / Multimedia<br/>DeepStream이 사용하는 가속 기반"]
    L4T["Linux for Tegra<br/>Jetson Ubuntu / NVIDIA Driver / systemd<br/>카메라 디바이스와 OS 기반"]

    U --> DS --> JP --> L4T
```

`ds_camera`는 첨부 이미지의 최상단 **User APPs** 영역에 해당함.  
DeepStream SDK 자체를 수정하는 프로젝트가 아니라, DeepStream SDK 위에 올라가는 사용자 애플리케이션임.

---

## 2. 이미지 계층과 현재 코드 매핑

```mermaid
flowchart TB
    subgraph USER["User APPs"]
        RUN["jetsonctl.sh<br/>서비스/UI 통합 관리"]
        WEB["web_ui.py<br/>Flask UI Proxy"]
        DAEMON["infer_daemon.py<br/>DeepStream 추론 데몬"]
        WATCH["watchdog.py<br/>상태 감시/복구"]
        BENCH["templates/bench.html<br/>ROI 설정/관제 화면"]
        PARTS["ds/parts/*.py<br/>사용자 서비스 로직"]
    end

    subgraph DSSDK["DeepStream SDK"]
        PY["Python Binding<br/>pyds + gi.repository.Gst"]
        PLUGIN["C/C++ Plugins<br/>nvstreammux / nvinfer / nvtracker<br/>nvdsosd / nvmultistreamtiler / nvv4l2h264enc"]
        MODEL["Pre-trained Model Path<br/>traffic_infer_ds.engine<br/>config_infer_primary.txt"]
        COMM["Device Communication<br/>modbus_daemon.py<br/>ds_modbus_rtu_comm.py<br/>gpio_pulse.py"]
    end

    subgraph JETPACK["JetPack SDK"]
        DL["Deep Learning<br/>TensorRT / CUDA / cuDNN"]
        MM["Multimedia<br/>V4L2 / NVMM / JPEG Encoder"]
        CV["Computer Vision<br/>NumPy / Pillow / DeepStream 내부 처리"]
    end

    subgraph LINUX["Linux for Tegra"]
        OS["Ubuntu / NVIDIA Driver / systemd<br/>/dev/video0 등 디바이스"]
    end

    DAEMON --> PY
    PARTS --> PY
    PARTS --> COMM
    PY --> PLUGIN
    MODEL --> PLUGIN
    PLUGIN --> DL
    PLUGIN --> MM
    PARTS --> CV
    DL --> OS
    MM --> OS
    CV --> OS
```

Graph Composer와 Container Builder는 현재 구조에서 사용하지 않음.  
현재는 Python 코드가 DeepStream GStreamer 파이프라인을 직접 생성하는 방식임.

---

## 3. ds_camera 전체 구조

```mermaid
flowchart TB
    WEBVIEW["/home/jetson/jetson_ai_modbus_rtu/webview.py<br/>Jetson 로컬 PyQt WebView<br/>웹 화면을 띄우는 클라이언트임"]

    subgraph APP["/home/jetson/jetson_ai_modbus_rtu"]
        WEBUI["web_ui.py<br/>로컬 Flask UI Proxy<br/>127.0.0.1:5022"]
        INFER["infer_daemon.py<br/>실제 DeepStream 추론 데몬<br/>127.0.0.1:5122"]
        WD["watchdog.py<br/>추론 상태 감시/복구"]
        HTML["templates/bench.html<br/>ROI 설정/추론 관제 UI"]
        RUNTIME["ds/runtime.py<br/>parts 파일 순차 로드"]
        CORE["ds/parts/*.py<br/>카메라 / ROI / 파이프라인 / 라우트 / Modbus bridge"]
    end

    subgraph DS["DeepStream SDK"]
        GST["GStreamer + pyds"]
        NV["nvstreammux<br/>nvinfer<br/>nvtracker<br/>nvdsosd<br/>nvmultistreamtiler<br/>webrtcbin / jpegenc"]
    end

    subgraph JP["JetPack SDK"]
        ACCEL["TensorRT / CUDA / Multimedia"]
    end

    subgraph L4T["Linux for Tegra"]
        BASE["Jetson Ubuntu<br/>NVIDIA Driver<br/>systemd<br/>/dev/video*"]
    end

    WEBVIEW --> WEBUI
    WEBUI --> HTML
    WEBUI --> INFER
    WD --> INFER
    INFER --> RUNTIME
    RUNTIME --> CORE
    CORE --> GST
    GST --> NV
    NV --> ACCEL
    ACCEL --> BASE
```

`webview.py`는 추론 서버가 아님.  
Jetson 로컬에서 `http://localhost:5022`를 띄우는 클라이언트임.

실제 서버와 추론은 `ds_camera` 내부의 `web_ui.py`, `infer_daemon.py`가 담당함.

---

## 4. 실행 프로세스 구조

현재 기본 실행 방식은 **split 모드**임.

```mermaid
flowchart TB
    USER["사용자<br/>Jetson 로컬 webview<br/>또는 원격 PC 브라우저"]
    WEB["web_ui.py<br/>Flask UI Proxy<br/>PORT 5022"]
    DAEMON["infer_daemon.py<br/>DeepStream Runtime<br/>PORT 5122<br/>내부 전용"]
    WATCH["watchdog.py<br/>/status 감시<br/>멈춤 시 /stop, /start 또는 재시작"]
    SYSTEMD["systemd<br/>자동 실행<br/>프로세스 재시작"]

    USER -->|"HTTP 요청<br/>ROI / start / status"| WEB
    WEB -->|"Proxy 요청"| DAEMON
    WATCH -->|"상태 감시/복구 요청"| DAEMON
    SYSTEMD -. 필요할 때만 .-> WEB
    SYSTEMD --> DAEMON
    SYSTEMD --> WATCH
```

`web_ui.py`는 브라우저 요청을 받는 앞단임.  
`infer_daemon.py`는 카메라, DeepStream pipeline, ROI, 카운팅, 로컬 WebRTC/MJPEG 출력과 Modbus 연동을 소유하는 핵심 프로세스임.
`watchdog.py`는 추론 멈춤을 감지하고 복구하는 운영 안정성 프로세스임.

---

## 5. DeepStream 데이터 흐름

가로로 길게 한 줄로 늘리면 Notion에서 글자가 작아지므로 세로 흐름으로 구성함.

```mermaid
flowchart TB
    SRC["1. 입력<br/>USB Camera /dev/video0<br/>또는 테스트 영상"]
    DEC["2. Source Decode<br/>v4l2src / uridecodebin / nvurisrcbin"]
    MUX["3. Batch 구성<br/>nvstreammux"]
    INFER["4. 1차 추론<br/>nvinfer + TensorRT YOLO Engine"]
    P1["5. Probe 1<br/>YOLO tensor parsing<br/>NvDsObjectMeta 생성"]
    TRACK["6. Tracking<br/>nvtracker<br/>object_id 부여"]
    P2["7. Probe 2<br/>ROI 통과 판정<br/>속도 계산<br/>방향별 카운팅"]
    OSD["8. OSD<br/>nvdsosd<br/>bbox / text / ROI 렌더링"]
    TILER["9. 화면 합성<br/>nvmultistreamtiler"]
    ENC["10. 미리보기 인코딩<br/>JPEG 기본 / H.264 선택"]
    RTC["11. localhost MJPEG<br/>WebRTC는 명시적 opt-in"]
    EVENT["결과 전달<br/>비동기 localhost HTTP<br/>modbus_daemon.py"]
    RTU["Modbus RTU Slave<br/>FC03 / FC16<br/>/dev/ttyUSB0"]

    SRC --> DEC --> MUX --> INFER --> P1 --> TRACK --> P2
    P2 --> OSD --> TILER --> ENC --> RTC
    P2 --> EVENT --> RTU
```

무거운 작업은 대부분 DeepStream SDK와 JetPack SDK 계층에서 수행됨.  
Python 코드는 플러그인 연결, ROI/속도/카운팅, API 라우트, 상태 관리를 담당함.

---

## 6. ds/runtime.py와 parts 구조

`ds/runtime.py`는 각 파일을 독립 import하는 구조가 아님.  
`ds/parts/*.py` 파일을 하나의 공유 네임스페이스에 순서대로 `exec()`해서 로드하는 구조임.

```mermaid
flowchart TB
    RT["ds/runtime.py<br/>parts 순차 로더"]
    P00["00_bootstrap.py<br/>Flask / Gst / pyds 초기화<br/>경로/모델/카메라 기본값"]
    P10["10_common_state.py<br/>_st / _streams<br/>ROI 상태 / 카운트 상태"]
    P20["20_media_yolo.py<br/>영상 메타 / 썸네일<br/>YOLO 후처리 / NMS"]
    P40["40_speed_counting.py<br/>ROI 교차 판정<br/>BEV 보정 / 속도 / 카운팅"]
    P50["50_state_config.py<br/>설정 프리셋<br/>ROI 적용<br/>nvinfer cfg 생성"]
    P55["55_comm_watchdog.py<br/>Modbus event bridge<br/>GPIO / 통신 상태"]
    P60["60_probes_sources.py<br/>GStreamer source 생성<br/>Probe 처리"]
    P70["70_pipeline.py<br/>DeepStream GStreamer Pipeline 빌드"]
    P80["80_routes.py<br/>Flask API routes"]
    P90["90_lifecycle.py<br/>서버 실행<br/>cleanup"]

    RT --> P00 --> P10 --> P20 --> P40 --> P50 --> P55 --> P60 --> P70 --> P80 --> P90
```

이 구조를 쓰는 이유는 GStreamer probe, Flask route, 전역 상태가 서로 강하게 연결되어 있기 때문임.  
예를 들어 `00_bootstrap.py`에서 만든 `app`, `Gst`, `pyds`, `_IS_JETSON`을 `70_pipeline.py`, `80_routes.py`가 그대로 사용함.

---

## 7. 디렉터리 구조

```mermaid
flowchart TB
    ROOT["ds_camera/"]

    EXEC["실행/운영 스크립트<br/>deploy.sh<br/>jetsonctl.sh<br/>install_systemd.sh<br/>setup.sh"]
    ENTRY["프로세스 진입점<br/>web_ui.py<br/>infer_daemon.py<br/>watchdog.py<br/>app.py"]
    DSROOT["DeepStream Runtime<br/>ds/runtime.py<br/>ds/parts/<br/>ds/test_videos/"]
    TEMPLATE["웹 UI<br/>templates/bench.html"]
    CONFIG["DeepStream 설정<br/>deepstream/config_infer_primary.txt<br/>deepstream/config_tracker_IOU.yml<br/>deepstream/config_tracker_*.yml<br/>deepstream/labels.txt"]
    MODEL["모델<br/>models/traffic_infer_ds.engine"]
    COMM["Modbus / GPIO<br/>modbus_daemon.py<br/>ds_modbus_rtu_comm.py<br/>modbus_bridge.py<br/>gpio_pulse.py"]
    STATE["상태/로그<br/>setting_preset.json<br/>current_roi_state.json<br/>logs/<br/>run/<br/>result_logs/"]

    ROOT --> EXEC
    ROOT --> ENTRY
    ROOT --> DSROOT
    ROOT --> TEMPLATE
    ROOT --> CONFIG
    ROOT --> MODEL
    ROOT --> COMM
    ROOT --> STATE
```

같은 작업 루트의 `/home/jetson/jetson_ai_modbus_rtu/webview.py`는 추론 서버가 아니라 로컬 GUI 클라이언트임.
Jetson 로컬 Ubuntu GUI에서 `http://localhost:5022`를 띄우는 클라이언트 실행기임.

---

## 8. ROI와 추론 상태 흐름

```mermaid
flowchart TB
    U1["사용자<br/>카메라 연결 또는 테스트 영상 선택"]
    UI1["bench.html<br/>/connect_camera 또는 /add_local 요청"]
    WEB1["web_ui.py<br/>요청 proxy"]
    D1["infer_daemon.py<br/>stream 상태 생성"]
    STATE1["_streams<br/>입력 소스 상태 보관"]

    U2["사용자<br/>ROI 영역 / 측정선 / 기준 거리 설정"]
    UI2["bench.html<br/>/set_roi 요청"]
    D2["infer_daemon.py<br/>ROI 상태 적용"]
    ROIJSON["current_roi_state.json<br/>현재 ROI 유지"]

    U3["사용자<br/>추론 시작"]
    START["/start 요청"]
    PIPE["DeepStream Pipeline 시작"]
    PROBE["Probe 처리<br/>탐지 / 추적 / 카운팅 갱신"]
    VIEW["bench.html<br/>/status / MJPEG / recent detections 표시"]

    U1 --> UI1 --> WEB1 --> D1 --> STATE1
    STATE1 --> U2 --> UI2 --> WEB1 --> D2 --> ROIJSON
    ROIJSON --> U3 --> START --> WEB1 --> PIPE --> PROBE --> VIEW
```

ROI는 UI에서 설정하지만, 실제 추론 중에는 `infer_daemon.py` 내부 상태와 `current_roi_state.json` 기준으로 사용됨.

---

## 9. Modbus RTU 데이터 전달 흐름

```mermaid
flowchart TB
    COUNT["확정된 ROI 통과 이벤트<br/>class / direction / speed"]
    GPIO["gpio_pulse.py<br/>BOARD 29 / 100ms pulse"]
    BRIDGE["modbus_bridge.py<br/>비차단 큐 / event_id"]
    MODBUSD["modbus_daemon.py<br/>누적값 영속화 / register map"]
    SERIAL["ds_modbus_rtu_comm.py<br/>FC03 / FC16<br/>/dev/ttyUSB0"]

    COUNT --> GPIO
    COUNT --> BRIDGE -->|"HTTP 127.0.0.1:5123/event"| MODBUSD --> SERIAL
```

`modbus_daemon.py`가 직렬 포트와 누적 레지스터의 권위 있는 소유자임.
따라서 inferd가 재시작되는 동안에도 Modbus Slave는 살아 있고 상태 2를 응답하며,
누적값은 `run/modbus_state.json`에서 복원됨.

---

## 10. systemd 자동실행 구조

```mermaid
flowchart TB
    SYSTEMD["systemd<br/>부팅 자동실행<br/>프로세스 재시작"]
    MODBUS["jetson-ai-modbus-rtu-modbusd.service<br/>Modbus RTU / 누적 상태 소유"]
    INFER["jetson-ai-modbus-rtu-inferd.service<br/>infer_daemon.py<br/>DeepStream 추론 데몬"]
    WATCH["jetson-ai-modbus-rtu-watchdog.service<br/>watchdog.py<br/>추론 멈춤 감시/복구"]
    OPERATOR["현장 운영자<br/>바탕화면 아이콘 / jetsonctl.sh ui"]
    WEB["jetsonctl.sh ui<br/>web_ui.py + webview.py<br/>필요할 때만 로컬 실행"]

    SYSTEMD --> MODBUS
    SYSTEMD --> INFER
    SYSTEMD --> WATCH
    OPERATOR --> WEB
    WATCH -->|"상태 확인/복구 요청"| INFER
    WEB -->|"사용자 요청 proxy"| INFER
```

`webview.py`는 systemd 서비스에 포함되어 있지 않음.  
자동실행의 중심은 `ds_camera` 서버 3종이고, webview는 사람이 화면을 볼 때만 실행하는 클라이언트임.

---

## 11. 운영 관점에서 중요한 구분

```mermaid
flowchart TB
    subgraph MUST["추론 유지에 필수"]
        A1["infer_daemon.py<br/>DeepStream pipeline 소유"]
        A2["DeepStream Pipeline<br/>카메라 입력 / 추론 / 추적 / 카운팅"]
        A3["modbus_daemon.py<br/>직렬 통신 / 누적 상태 유지"]
    end

    subgraph STABLE["운영 안정성"]
        B1["watchdog.py<br/>추론 멈춤 감지/복구"]
    end

    subgraph VIEW["화면 확인용"]
        C1["web_ui.py<br/>브라우저와 데몬 사이 proxy"]
        C2["webview.py<br/>Jetson 로컬 GUI 클라이언트"]
        C3["브라우저/MJPEG 화면<br/>추론 현황 확인 / ROI 설정"]
    end

    A1 --> B1
    C3 --> C1
    C1 --> A1
    B1 --> A1
```

리소스를 아끼려면 운영 중에는 화면 계층을 최소화하는 구성이 적합함.

- `infer_daemon.py`: 추론을 위해 필요함
- `modbus_daemon.py`: Modbus 응답과 누적값 유지를 위해 필요함
- `watchdog.py`: 자동 복구를 위해 필요함
- `web_ui.py`: ROI 설정/상태 확인이 필요할 때 사용함
- `webview.py`: Jetson 로컬 GUI 화면을 볼 때만 사용함

---

## 12. 한 문장 요약

```mermaid
flowchart TB
    A["User APPs<br/>ds_camera Python Flask/DeepStream application"]
    B["DeepStream SDK<br/>Python binding + GStreamer nv* plugins"]
    C["JetPack SDK<br/>TensorRT / CUDA / Multimedia"]
    D["Linux for Tegra<br/>Jetson Ubuntu + NVIDIA driver + systemd"]

    A --> B --> C --> D
```

즉 `ds_camera`는 DeepStream SDK 자체를 새로 만든 것이 아님.  
DeepStream SDK와 JetPack SDK 위에 올라가는 **가로등 Edge AI용 사용자 애플리케이션**임.
