# DeepStream Python Bindings

이 폴더는 NVIDIA `deepstream_python_apps` 저장소 전체가 아니라, `pyds` binding 소스만 남긴 축소본입니다.

운영 런타임은 시스템에 설치된 `pyds`를 먼저 import합니다. 이 축소본에는 NVIDIA 샘플 앱과 git submodule이 없으므로, 일반 설치는 DeepStream 버전에 맞는 `pyds` wheel을 쓰는 경로를 권장합니다.

삭제한 항목:

- 샘플 앱 (`apps/`)
- 문서 빌드 소스 (`docs/`)
- 노트북 (`notebooks/`)
- 테스트 코드 (`tests/`)
- git 메타데이터 (`.git/`)
- binding submodule/qemu cross-build 보조 파일
- 샘플 이미지와 HOWTO/FAQ 문서

남긴 항목:

- `bindings/`: PyDS Python binding 소스
- `LICENSE`, `THIRD_PARTY_LICENSE`: NVIDIA 원본 라이선스
