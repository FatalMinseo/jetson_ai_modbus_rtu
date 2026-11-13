# ds 런타임 구조

`runtime.py`는 `parts/` 안의 파일을 정해진 순서대로 실행합니다.
GStreamer probe, Flask route, 로컬 미리보기 상태, 카운팅 상태가 같은 전역 상태를 공유해야 해서 아직은 하나의 네임스페이스로 묶어 둔 구조입니다.

## 로드 순서

| 순서 | 파일 | 역할 |
|---:|---|---|
| 1 | `00_bootstrap.py` | Flask 앱, 경로, 모델/트래커 고정값, 카메라 기본값, Jetson 리소스 측정 |
| 2 | `10_common_state.py` | 공용 상태, 카운트/속도 집계, 결과 로그 저장 |
| 3 | `20_media_yolo.py` | 영상 메타/썸네일/정규화, YOLO 후처리 |
| 4 | `40_speed_counting.py` | ROI 라인 통과, 속도 계산, 탐지로그 이벤트 |
| 5 | `50_state_config.py` | 실행 상태, 로컬 MJPEG 출력 valve, nvinfer 설정 생성 |
| 6 | `55_comm_watchdog.py` | LwM2M 전송 스레드와 내부 보조 감시 |
| 7 | `60_probes_sources.py` | GStreamer source bin, 카메라 프리뷰, probe 처리 |
| 8 | `70_pipeline.py` | DeepStream 파이프라인 생성/실행 |
| 9 | `80_routes.py` | Flask API 라우트 |
| 10 | `90_lifecycle.py` | 종료 정리와 Flask 실행 |

파이프라인 상세 흐름은 상위 폴더의 `PIPELINE.md`를 보면 됩니다.
