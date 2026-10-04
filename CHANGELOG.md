# 변경 이력

이 프로젝트는 [유의적 버전](https://semver.org/lang/ko/)을 따른다.

## [1.1.0] — 2026-10-04

첫 번째 정식 태그 릴리스. 코드는 `rpi5` 태그(버전_1)에서 공개한 것과 같은 계보이고,
이번 릴리스에서 배포 가능한 형태로 정리했다.

### 추가
- MIT 라이선스 — 저장소를 오픈소스로 공개
- `PACKAGES.md` — 실제로 import·호출하는 것만 추린 의존성 목록
  (필수 2개 / 선택 하드웨어 7개 / 외부 CLI 도구), apt·pip 이름과 검증된 버전,
  각 패키지가 빠질 때 잃는 기능까지 명시
- `install.sh` — `--core` / `--tools` / `--dry-run` 옵션. 이미 설치된 apt 패키지는 건너뛴다
- `pyproject.toml` + `requirements.txt` / `requirements-hardware.txt` / `requirements-dev.txt`
  — 필수·하드웨어·개발 의존성 3분할, 휠과 sdist 빌드 가능
- `README.md` — 설치부터 USB 판정 근거, 플로피 VM 부팅, 안전 수칙, 구조까지 전체 설명서
- `CHANGELOG.md` — 이 문서

### 바뀜
- 버전 문자열을 1.1.0으로 통일 (`pyproject.toml`, `updev/__init__.py`, `updev -V`)

## [rpi5] — 2026-08-10

최초 공개. `upp_linux_rpi_only_maybe_sorry_v1.zip`로 배포.
