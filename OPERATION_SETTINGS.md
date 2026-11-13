# 운영 설정

전역 기본값은 `operation_settings.json`에서 관리한다. 앱을 다시 시작하면 적용된다.

| 항목 | 의미 |
| --- | --- |
| `modbus_rtu.port` | Jetson의 RS-485 시리얼 장치 |
| `modbus_rtu.baudrate` | RS-485 보드레이트 |
| `modbus_rtu.slave_id` | EdgeAI Modbus Slave ID |
| `modbus_rtu.timeout_seconds` | RS-485 읽기 타임아웃(초) |
| `analytics.window_seconds` | ROI 진출 이벤트를 유지할 실시간 무빙 윈도우 길이 |
| `roi_defaults.ref_distance_m` | 새 ROI에 적용할 두 측정선 사이 실제 거리(m) |
| `roi_defaults.confidence_threshold` | 새 스트림에 적용할 기본 탐지 confidence |

카메라별 ROI 좌표와 개별 보정값은 `current_roi_state.json`에 계속 저장된다. 이 파일의 값이 해당 카메라에 대해서는 전역 기본값보다 우선한다.

`DS_CAMERA_MODBUS_PORT`, `DS_CAMERA_MODBUS_BAUDRATE`, `DS_CAMERA_MODBUS_SLAVE_ID`, `DS_CAMERA_MODBUS_TIMEOUT_SEC`, `DS_CAMERA_WINDOW_SECONDS` 환경변수를 지정하면 해당 항목은 `operation_settings.json`보다 우선한다.
