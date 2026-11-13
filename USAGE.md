# ds_camera 사용법

> 최초 설치는 `./deploy.sh` 한 번으로 수행합니다. 설치 후 Modbus, 추론, watchdog은
> systemd가 자동 운영하며, 설정 화면은 바탕화면 아이콘 또는 `./jetsonctl.sh ui`로
> 필요할 때만 엽니다. 설정 창을 닫아도 추론과 Modbus 통신은 계속됩니다.

이 문서는 Jetson 장비의 설치, 서비스 관리, 설정 및 장애 확인 방법을 정리합니다.

## 1. 기본 구조

기본 실행 방식은 `split` 모드입니다.

```text
브라우저
  -> web_ui.py                 # 사용자가 접속하는 웹 UI, 기본 포트 5022
  -> infer_daemon.py           # 내부 추론 데몬, 기본 포트 5122
  -> watchdog.py               # 추론 멈춤 감시 및 복구

Windows Master
  -> modbus_daemon.py          # 독립 RTU Slave, 내부 상태 포트 5123
  <- modbus_bridge.py          # inferd의 확정 통과 이벤트 수신
```

웹 UI를 껐다 켜도 추론 데몬은 따로 유지될 수 있습니다.
추론 데몬을 재시작해도 Modbus 데몬과 누적 카운트는 유지됩니다.
Master의 누적 슬롯 초기화와 WebUI의 카운트 초기화는 modbusd/inferd 양쪽에
동시에 반영됩니다.
WebUI 누적 카운트는 inferd의 임시 메모리가 아니라 Windows Master 메뉴 `10`과
동일한 modbusd 영구 누적 슬롯을 표시합니다.
WebUI와 Windows Master 메뉴 `17/18`의 ROI 거리·confidence 변경은 서로 동기화되며,
Master 메뉴 `2`는 현재 공통 설정값을 표시합니다.
실서비스에서는 `systemd` 등록 후 부팅 자동 실행으로 운영하는 것을 권장합니다.

## 2. 최초 설치

### 2.1 새 Jetson에 저장소 받기

프로젝트 폴더가 없는 새 장비에서는 다음 순서로 받습니다.

```bash
cd /home/jetson
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/FatalMinseo/jetson_ai_modbus_rtu.git
cd jetson_ai_modbus_rtu
```

비공개 저장소 인증을 요구하면 Gitea 사용자명과 비밀번호 또는 Access Token을
입력합니다. 인증정보는 프로젝트 파일에 직접 저장하지 않습니다.

### 2.2 의존성 및 systemd 서비스 설치

최초 설치는 `setup.sh`와 `install_systemd.sh`를 따로 실행하지 않고 다음 명령
하나로 진행합니다.

```bash
cd /home/jetson/jetson_ai_modbus_rtu
chmod +x deploy.sh jetsonctl.sh
./deploy.sh
```

`deploy.sh`가 하는 일:

- 이전 apt 작업이 중단돼 `dpkg` 미완료 상태이면 먼저 `dpkg --configure -a`와
  의존성 복구 실행
- Ubuntu/GStreamer 실행 패키지 설치
- Python 가상환경 `.venv` 생성
- Flask, numpy, Pillow, psutil, pyserial 등 Python 패키지 설치
- DeepStream 플러그인, `pyds` import 가능 여부 점검
- Modbus RTU 사용자에게 `dialout` 그룹 권한 설정
- `modbusd`, `inferd`, `watchdog` systemd 서비스 설치
- 원격 추론 재시작 및 OS 재부팅용 제한된 sudo 권한 설치
- 로컬 바탕화면 실행 아이콘 설치
- `modbusd`, `inferd`, `watchdog`을 부팅 자동 실행으로 등록하고 즉시 시작
- 설정 화면을 열 때만 로컬 WebUI 프로세스를 임시 실행

DeepStream SDK나 `pyds`가 맞지 않으면 추론 실행 전에 먼저 해결해야 합니다.

이 배포본은 Jetson Orin Nano의 CH340/CH341 USB-RS485 어댑터(`1a86:7523`)
지원을 기본으로 포함합니다. 따라서 일반적인 최초 설치는 그대로 실행하면
드라이버까지 설치됩니다.

```bash
./deploy.sh
```

CP210x 또는 FT232RL만 사용하며 CH341 모듈을 설치하지 않을 장비에서는 다음처럼
명시적으로 건너뜁니다.

```bash
INSTALL_CH341=0 ./deploy.sh
```

`drivers/ch341/install.sh`가 없다는 메시지가 나오면 해당 장비가 아직 드라이버
파일이 포함된 최신 커밋을 받지 못한 것입니다.

```bash
git pull --ff-only origin main
ls -l drivers/ch341/install.sh
./deploy.sh
```

이미 배포된 장비에서 CH341 드라이버만 설치하려면 다음을 실행합니다. 실행 중인
Modbus 서비스는 설치 중에만 자동으로 중지되고 완료 후 다시 시작됩니다.

```bash
./drivers/ch341/install.sh
```

이 설치는 전체 커널을 교체하지 않고 현재 `5.15.148-tegra`용 `ch341.ko`만
빌드합니다. 또한 CH340을 가로채는 `brltty-udev`를 mask합니다. 자세한 원인,
검증 및 복구 방법은 `drivers/ch341/README.md`를 참고합니다.

설치 확인:

```bash
sudo systemctl status \
  jetson-ai-modbus-rtu-modbusd \
  jetson-ai-modbus-rtu-inferd \
  jetson-ai-modbus-rtu-watchdog \
  --no-pager

curl http://127.0.0.1:5122/status
curl http://127.0.0.1:5123/status
```

세 서비스가 `active (running)`이고 두 상태 주소에서 JSON이 반환되면 백그라운드
설치가 완료된 것입니다. 카메라나 RS-485 어댑터가 아직 연결되지 않았다면
서비스는 실행 중이어도 `/dev/video*` 또는 `/dev/ttyUSB0` 미연결 상태가 표시될
수 있습니다.

### 2.3 최초 카메라·ROI 설정

바탕화면의 `Jetson AI Modbus RTU` 아이콘을 더블클릭하거나 다음을 실행합니다.

```bash
cd /home/jetson/jetson_ai_modbus_rtu
./jetsonctl.sh ui
```

화면에서 카메라, ROI/L1/L2, 거리, confidence를 설정하고 `추론 시작`을 누릅니다.
PeopleNet 설정과 추론 실행 상태는 저장되어 재부팅 및 watchdog 복구 후에도
유지됩니다. 설정이 끝나면 PyQt 창만 닫으면 됩니다. 로컬 WebUI는 함께 종료되고
추론·카운팅·Modbus·watchdog은 계속 동작합니다.

### 2.4 이미 설치된 장비 업데이트

로컬 설정 파일을 보호하기 위해 먼저 변경 상태를 확인한 뒤 업데이트합니다.

```bash
cd /home/jetson/jetson_ai_modbus_rtu
git status --short
git pull --ff-only origin main
./deploy.sh
```

`git pull`이 로컬 변경 때문에 중단되면 `operation_settings.json`과
`current_roi_state.json`을 임의로 삭제하거나 `git reset --hard`하지 말고 먼저
백업 및 변경 내용을 확인합니다. `deploy.sh` 실행 중에는 서비스를 새 코드로
교체하기 위해 추론과 Modbus 통신이 잠시 재시작됩니다.

## 3. 설치 후 서비스 관리

설치 이후에는 수동 Python 프로세스를 띄우지 않고 `jetsonctl.sh`만 사용합니다.

```bash
./jetsonctl.sh start
./jetsonctl.sh stop
./jetsonctl.sh restart
./jetsonctl.sh status
```

추론만 논리적으로 중지하려면 설정 화면의 `정지` 버튼을 사용합니다. 전체
백그라운드 서비스를 중지할 때만 `./jetsonctl.sh stop`을 사용합니다.

## 4. 설정 화면

```bash
./jetsonctl.sh ui
```

설정 창은 로컬에서만 열리며 Wi-Fi 연결이 필요하지 않습니다. 창을 닫아도
백그라운드 추론과 Modbus 통신은 유지됩니다.

## 5. 로그

```bash
./jetsonctl.sh logs all
./jetsonctl.sh logs infer
./jetsonctl.sh logs modbus
./jetsonctl.sh logs watchdog
```

내부 상태는 다음 주소에서도 확인할 수 있습니다.

```bash
curl http://127.0.0.1:5122/status
curl http://127.0.0.1:5123/status
```

## 6. watchdog과 통신 감시

watchdog은 두 종류가 있습니다.

### 외부 watchdog

`watchdog.py`는 `jetson-ai-modbus-rtu-inferd`를 밖에서 감시합니다.

주요 감시 대상:

- infer daemon HTTP 응답 여부
- `/status`의 `running` 상태
- 각 채널의 `frame_idx`, `loop_count`, `total_elapsed_sec` 진행 여부
- 선택적으로 TCP 연결 가능 여부

즉, 외부 watchdog의 핵심 목적은 **추론 프로세스와 DeepStream 프레임 진행 복구**입니다.

### Modbus RTU 통신 감시

실서비스에서는 독립 `modbus_daemon.py`가 `/dev/ttyUSB0`와 누적 레지스터를
관리합니다. inferd는 확정된 ROI 통과 이벤트만 내부 HTTP로 modbusd에 전달하며,
LwM2M 서버나 외부 DB로 데이터를 보내지 않습니다.

- `modbusd`: 직렬 연결, FC03/FC16, 누적 카운트와 설정 관리
- `inferd`: 탐지·ROI 통과 이벤트를 modbusd로 전달
- `watchdog`: inferd HTTP 응답과 프레임 진행 상태 감시
- systemd `Restart=always`: 각 데몬이 비정상 종료되면 자동 재시작

통신 상태는 `/status` 응답의 `db_status.mode`가 `modbus_rtu`인지 확인하거나
modbusd 상태 API와 journal에서 확인합니다.

```bash
curl http://127.0.0.1:5123/status
journalctl -u jetson-ai-modbus-rtu-modbusd -f
```

## 7. systemd 실서비스 운영

`./deploy.sh`가 서비스 등록·자동 시작·바탕화면 아이콘 설치까지 처리합니다.
서비스 파일만 다시 설치해야 할 때는 `./install_systemd.sh`를 사용할 수 있습니다.
일상 운영에서는 다음 통합 명령만 사용합니다.

```bash
./jetsonctl.sh status
./jetsonctl.sh restart
./jetsonctl.sh logs all
```

`modbusd`, `inferd`, `watchdog`은 부팅 자동 실행 대상입니다. 설정 UI는 systemd
서비스가 아니며 `./jetsonctl.sh ui`를 실행한 동안에만 로컬에서 동작합니다.

## 8. 초기화 방법

카운트 초기화는 설정 화면의 `데이터 초기화` 버튼 또는 정해진 Modbus FC16
초기화 명령을 사용합니다. 서비스 상태만 새로 시작하려면 다음을 실행합니다.

```bash
./jetsonctl.sh restart
```

ROI와 동작 설정 파일은 현장 설정값이므로 임의로 삭제하지 않습니다. Python
환경을 다시 설치해야 할 정도로 패키지가 손상된 경우에는 저장 설정을 보존한 뒤
`./deploy.sh`를 다시 실행합니다. 테스트 결과 JSON만 지울 때는 필요한 파일을
확인한 후 `result_logs/`에서 선택적으로 정리합니다.

## 9. 문제 발생 시 대응

먼저 상태와 로그를 확인합니다.

```bash
./jetsonctl.sh status
./jetsonctl.sh logs infer
```

추론 영상이 멈췄다면 `./jetsonctl.sh restart`로 세 서비스를 정상 순서로
재시작합니다. 설정 화면만 이상하면 PyQt 창을 닫고 `./jetsonctl.sh ui`로 다시
엽니다. 이는 AI 파이프라인을 재시작하지 않습니다.

카메라와 Modbus 장치는 다음으로 확인합니다.

```bash
v4l2-ctl --list-devices
ls -l /dev/video* /dev/ttyUSB* 2>/dev/null
curl http://127.0.0.1:5123/status
```

CH340 장치가 `lsusb`에는 `1a86:7523`으로 보이지만 `/dev/ttyUSB*`가 없다면
`./drivers/ch341/install.sh`를 실행합니다. Wi-Fi와 NetworkManager 상태는 로컬
추론·카운팅·Modbus RTU 동작 조건이 아닙니다.

## 10. 운영 시 지우면 안 되는 것

아래 항목은 실행에 필요합니다.

- `models/traffic_infer_ds.engine`
- `deepstream/labels.txt`
- `deepstream/config_tracker_*.yml`
- `templates/bench.html`
- `ds/parts/*.py`
- `modbus_daemon.py`
- `ds_modbus_rtu_comm.py`
- `modbus_bridge.py`

아래 항목은 필요할 때 정리해도 됩니다.

- `__pycache__/`
- `*.pyc`
- `run/`
- `logs/`
- `result_logs/*.json`
