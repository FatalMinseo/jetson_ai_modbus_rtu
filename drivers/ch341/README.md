# Jetson CH340/CH341 USB 직렬 드라이버

이 폴더는 `1a86:7523` CH340 USB-RS485 어댑터를 Jetson Orin Nano의
`5.15.148-tegra` 커널에서 사용하기 위한 오프라인 설치 소스와 스크립트입니다.

`ch341.c`는 Linux stable `v5.15.148`의
`drivers/usb/serial/ch341.c`를 변경하지 않고 포함합니다.

- 원본: <https://github.com/gregkh/linux/blob/v5.15.148/drivers/usb/serial/ch341.c>
- 라이선스: GPL-2.0 (`LICENSE` 참고)
- 지원 커널: `5.15.148-tegra`
- 지원 확인 장치: QinHeng CH340 `1a86:7523`

## 설치

```bash
cd /home/jetson/jetson_ai_modbus_rtu
./drivers/ch341/install.sh
```

설치 스크립트는 다음을 수행합니다.

1. 실행 중인 커널과 NVIDIA 커널 헤더·`Module.symvers` 일치 여부 확인
2. 임시 폴더에서 `ch341.ko` 빌드
3. CH340을 가로채는 `brltty`/`brltty-udev` 서비스 중지 및 mask
4. 실행 중인 Modbus 서비스를 잠시 중지
5. 기존 사용자 설치 모듈을 `/var/backups/jetson-ch341/<커널>/`에 백업
6. `/lib/modules/<커널>/updates/ch341.ko` 설치
7. `depmod` 실행, 모듈 로드 및 기존 Modbus 서비스 재시작

점자 단말기를 실제로 사용하는 장비에서는 `brltty`를 비활성화하면 안 됩니다.
그 경우 다음처럼 설치합니다.

```bash
DISABLE_BRLTTY=0 ./drivers/ch341/install.sh
```

설치 후 어댑터를 뺐다가 다시 연결하고 확인합니다.

```bash
ls -l /dev/ttyUSB*
sudo dmesg -T | grep -Ei "ch341|ttyUSB|control message" | tail -20
```

정상 로그 예시는 다음과 같습니다.

```text
ch341-uart converter now attached to ttyUSB0
```

직접 빌드한 모듈이라 `module verification failed`와 커널 taint 경고가 표시될 수
있습니다. 모듈이 실제로 로드되고 `/dev/ttyUSB*`가 생성되면 서명 강제가 활성화된
환경은 아닙니다.

## 배경

초기에 `linux-kernel-module-dkms-ch341-compat` 저장소의 코드를 빌드했으나,
Jetson에서 다음 오류로 장치 초기화가 실패했습니다.

```text
failed to receive control message: -11
ch341-uart: probe of ttyUSB0 failed with error -11
```

해당 소스는 제어 전송 버퍼를 스택 배열로 변경한 코드였고, 공식 5.15.148
드라이버는 `kmalloc`으로 USB/DMA에 적합한 버퍼를 사용합니다. 공식 소스로 만든
모듈로 교체한 뒤 `/dev/ttyUSB0` 생성이 확인됐습니다. 전체 커널 이미지 재빌드나
플래시는 필요하지 않았습니다.

커널 버전이 바뀌면 이 모듈을 그대로 사용할 수 없습니다. 새 커널과 정확히 맞는
NVIDIA 헤더 및 같은 계열의 CH341 소스로 다시 빌드해야 합니다.
