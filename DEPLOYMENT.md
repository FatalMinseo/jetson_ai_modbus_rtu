# jetson_ai_modbus_rtu 운영 안내

이 폴더는 DeepStream 추론, ROI 통행량/속도 계산과 Modbus RTU Slave를
독립적으로 운영하는 배포본입니다. LwM2M 통신은 포함하지 않습니다.

## 상태 저장과 재부팅 복원

WebUI에서 마지막으로 확정한 다음 설정은 `operation_settings.json`과
`current_roi_state.json`에 저장됩니다.

- 입력 종류: 카메라 또는 테스트 영상
- 카메라 장치, 해상도, FPS
- ROI, L1/L2, 거리, confidence
- Traffic 단독 또는 Traffic+PeopleNet
- 추론 실행/중지 상태

추론이 실행 중인 상태에서 재부팅되면 같은 입력과 설정으로 자동 복원합니다.
WebUI에서 추론을 중지한 경우에는 중지 상태가 저장되어 재부팅 후에도 추론을
자동으로 시작하지 않습니다. 저장한 카메라가 부팅 직후 준비되지 않으면 5초
간격으로 다시 연결합니다.

`street_ds.mp4`는 테스트용으로 포함하지만 기본 입력으로 강제하지 않습니다.

## 서비스 구성

| 서비스 | 역할 | 기본 부팅 |
|---|---|---|
| `jetson-ai-modbus-rtu-modbusd` | 직렬 포트, 레지스터, 누적 카운트 및 장치 명령 | 활성 |
| `jetson-ai-modbus-rtu-inferd` | 카메라 및 DeepStream 추론 | 활성 |
| `jetson-ai-modbus-rtu-watchdog` | inferd와 추론 진행 상태 감시 | 활성 |

Watchdog은 내부 inferd(`127.0.0.1:5122`)만 확인합니다. 설정 UI는 필요할 때만
로컬 프로세스로 열리므로 창을 닫아도 추론과 Modbus 통신은 유지됩니다.
`inferd`가 재시작되거나 멈춰도 `modbusd`는 계속 통신하며 상태값 `2`를
응답합니다. 정상적으로 프레임이 진행되면 상태값은 `1`입니다.

방향별 누적 카운트는 작은 고정 크기 상태 파일 `run/modbus_state.json`에
저장됩니다. 따라서 추론 재시작 및 정상적인 전원 재부팅 후에도 유지되며,
해당 슬롯에 FC16 초기화 명령을 쓸 때만 0으로 초기화됩니다.

누적값 초기화는 양방향으로 동기화됩니다. Windows Master 메뉴 `11~16`으로
슬롯을 초기화하면 WebUI의 해당 객체·방향 카운트도 함께 초기화되고, WebUI의
카운트 초기화 버튼을 사용하면 modbusd의 6개 누적 슬롯도 함께 초기화됩니다.
WebUI 누적값과 Windows Master 메뉴 `10`은 모두 modbusd의 동일한 영구 누적
슬롯을 읽으므로 inferd 또는 UI 재시작 뒤에도 서로 다른 값이 표시되지 않습니다.

ROI 거리와 confidence도 양방향으로 동기화됩니다. WebUI에서 변경하면 Windows
Master 메뉴 `2`에 반영되고, Master 메뉴 `17/18`에서 변경하면 실행 중 추론과
재부팅 복원 설정에 함께 반영됩니다.

## 새 장비 배포

```bash
cd /home/jetson/jetson_ai_modbus_rtu
./deploy.sh
```

Jetson Orin Nano `5.15.148-tegra`용 CH340/CH341 USB-RS485 드라이버는
`deploy.sh`에서 기본으로 설치됩니다.

```bash
./deploy.sh
```

CP210x와 FT232RL만 사용해서 CH341 모듈이 필요 없는 장비에서는 다음처럼
건너뜁니다.

```bash
INSTALL_CH341=0 ./deploy.sh
```

## 선택 사항: BOARD 29 GPIO 펄스 출력

GPIO 파형을 사용하는 성능평가 장비에서만 다음 스크립트를 실행합니다.
일반 현장 장비의 추론, ROI 집계 및 Modbus RTU에는 필요하지 않습니다.

```bash
cd /home/jetson/jetson_ai_modbus_rtu
./gpio_install.sh
sudo reboot
```

스크립트는 BOARD 29(GPIO01/PQ.05)를 push-pull 출력으로 설정하는 device-tree
overlay를 컴파일해 `/boot`에 설치하고 Jetson-IO 부팅 항목으로 선택합니다.
이미 같은 overlay가 선택된 경우에는 중복 항목을 만들지 않습니다.

## 설정 화면이 필요할 때

```bash
./jetsonctl.sh ui
```

바탕화면 아이콘도 같은 명령을 실행합니다. UI는 `127.0.0.1:5022`에서만 열리고
PyQt 창을 닫으면 로컬 UI 프로세스도 함께 종료됩니다. 추론·카운팅·watchdog·
Modbus RTU는 계속 동작합니다.

## 전체 중지

WebUI의 추론 중지 버튼은 추론만 중지하고 Modbus RTU는 유지합니다. Modbus와
추론을 모두 중지하려면 다음을 사용합니다.

```bash
sudo systemctl stop jetson-ai-modbus-rtu-watchdog jetson-ai-modbus-rtu-inferd
```

모든 서비스를 함께 중지하려면:

```bash
./jetsonctl.sh stop
```

## 기본 Modbus 설정

| 항목 | 값 |
|---|---:|
| 장치 | `/dev/ttyUSB0` |
| Baudrate | `460800` |
| Slave ID | `101` (`0x65`) |
| 형식 | `8-N-1` |

## 장치 제어 명령

장치 명령은 중복 실행 방지를 위해 `0x010A`(명령)와 `0x010B`(새로운
Sequence)를 **한 번의 FC16**으로 함께 기록합니다.

Slave ID `0` broadcast 요청은 읽기와 쓰기 모두 무시합니다. 따라서 broadcast
패킷으로 설정 변경, 추론 재시작 또는 OS 재부팅을 실행할 수 없으며, 조회와
제어는 반드시 장비에 설정된 Slave ID(기본 `101`)로 요청해야 합니다.

| 명령 | 동작 |
|---:|---|
| `3` | 추론 프로세스 전체 재시작 |
| `4` | Jetson OS 재부팅 |

명령 3과 4는 FC16 정상 응답을 먼저 전송한 직후 별도 실행 스레드에서 처리합니다.
명령 4는 비밀번호 입력 없이 허용된 root systemd reboot 서비스만 시작하며,
sudo 설정이 잘못된 경우에도 `sudo -n`이 즉시 실패하므로 비밀번호 입력 화면에서
대기하지 않습니다.

명령 3은 카메라 입력, ROI/L1/L2, 거리, confidence, 모델 설정, 추론 희망
상태 및 Modbus 누적값을 유지합니다. DeepStream/GStreamer/트래커 프로세스
상태만 새로 생성되므로 진행 중이던 Track ID와 한쪽 선만 통과한 미완료 이벤트는
사라집니다. 처리 순서는 `FC16 응답 → 상태 2 → inferd 재시작 → 저장 설정 복원
→ 프레임 정상 진행 → 상태 1`입니다.

Windows Master에서는 `21`이 추론 프로세스 재시작, `22`가 Jetson OS
재부팅입니다. 메뉴 입력 즉시 별도 확인 없이 명령을 전송합니다.

## 배포 확인

```bash
sudo systemctl status \
  jetson-ai-modbus-rtu-modbusd \
  jetson-ai-modbus-rtu-inferd \
  jetson-ai-modbus-rtu-watchdog --no-pager

curl http://127.0.0.1:5123/status
curl http://127.0.0.1:5122/status
```
