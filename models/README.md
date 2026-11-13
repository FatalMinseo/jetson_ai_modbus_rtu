# 모델 파일 별도 배포

이 저장소는 모델 바이너리와 INT8 보정 데이터를 포함하지 않습니다.
추론 실행 전 사용할 모델을 장비에 별도로 배치하세요.

- 기본 차량 모델: `models/traffic_infer_ds.engine`
- PeopleNet 사용 시: `people_net/peoplenet_explicit_fp16.engine`
- INT8 보정 데이터 사용 시: `people_net/resnet34_peoplenet_int8.txt`

클래스 라벨과 추론 설정은 소스에 포함됩니다. 모델을 사용하는 추론 기능은
해당 파일을 별도로 준비하기 전에는 실행할 수 없습니다.
TensorRT 엔진은 배포할 Jetson의 실행 환경에 맞는 파일을 사용하세요.
