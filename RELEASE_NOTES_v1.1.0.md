# updev 1.1.0

라즈베리파이(그리고 웬만한 리눅스 보드) 하나에 물린 **모든 것**을 한 곳에서 보는 장치관리자.
LAN · USB · I2C · SPI · Serial/UART · 카메라 · GPIO · 스토리지 · 블루투스, 그리고 보드 자신까지.

```bash
bin/updev            # 전체 스캔
bin/updev watch      # 라이브 대시보드
bin/updev doctor     # 뭐가 잘못됐는지 + 고치는 명령어까지
```

## 이번 릴리스

`rpi5`(버전_1)에서 zip으로만 돌던 걸 설치 가능한 패키지로 정리했다.

- **MIT 라이선스** — 저장소를 오픈소스로 공개
- **PACKAGES.md** — 실제로 import·호출하는 것만 추린 의존성 목록. 필수 2개(rich, click) /
  선택 하드웨어 7개 / 외부 CLI 도구로 나누고, apt·pip 이름과 검증된 버전,
  각 패키지가 빠질 때 잃는 기능까지 적었다
- **install.sh** — `--core` / `--tools` / `--dry-run`. 이미 깔린 apt 패키지는 건너뛴다
- **휠과 sdist** — `pyproject.toml`로 빌드. 이 릴리스에 첨부
- **README.md** — 설치부터 USB 판정 근거, 플로피 VM 부팅, 안전 수칙, 구조까지
- **CHANGELOG.md** — 변경 이력

## 설치

```bash
# 가장 짧게 — 설치 없이 체크아웃에서 바로
git clone https://github.com/yunsee-jicsee-dev/updev && cd updev
sudo apt install python3-rich python3-click
./bin/updev

# 또는 휠로
pip install --user updev-1.1.0-py3-none-any.whl

# 또는 백엔드 전부 + updev 명령 등록
./install.sh
```

기준 환경: Raspberry Pi 5 · Debian 13 (trixie) · Python 3.13.5
테스트 147개 통과, 5개 건너뜀(하드웨어 필요).
