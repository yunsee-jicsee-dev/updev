# 설치 패키지 목록

이 문서는 **실제로 코드가 import 하거나 호출하는 것만** 담았다. 프로젝트 전체를
`ast`로 훑어서 서드파티 import를 뽑고, `have()`/`run()`으로 부르는 외부 명령을
grep한 뒤, 각각을 `dpkg -S`로 소유 패키지까지 확인한 결과다.

기준 환경: **Raspberry Pi 5 · Debian 13 (trixie) · Python 3.13.5**

---

## TL;DR

```bash
# 1. 필수 (이것만 있으면 돈다)
sudo apt install python3-rich python3-click

# 2. 하드웨어 백엔드를 다 켜려면
sudo apt install python3-psutil python3-smbus2 python3-spidev \
                 python3-serial python3-lgpio python3-picamera2

# 3. 외부 도구 (대부분 Pi OS에 이미 있음)
sudo apt install iproute2 iputils-ping iw bluez util-linux \
                 v4l-utils usb.ids pci.ids udev

# 4. updev 설치
pip install --user dist/updev-1.1.0-py3-none-any.whl
```

설치 없이 그냥 쓰려면 `bin/updev`로 바로 실행해도 된다.

---

## 1. 파이썬 패키지

### 필수 — 2개

| 모듈 | apt | pip | 버전(확인됨) | 없으면 |
|---|---|---|---|---|
| `rich` | `python3-rich` | `rich>=13.0` | 13.9.4 | **동작 불가** — 모든 출력이 rich |
| `click` | `python3-click` | `click>=8.1` | 8.4.2 | **동작 불가** — CLI 전체가 click |

> 이 시스템의 `click`은 apt가 아니라 `~/.local`(pip --user)에서 왔다.
> 새 환경이라면 `python3-click`을 apt로 넣는 게 깔끔하다.

### 선택 — 하드웨어 백엔드

**전부 선택이다.** 없으면 그 백엔드의 상세 정보만 빠지고 나머지는 정상 동작한다.
`updev backends`로 뭐가 돌았는지 확인할 수 있다.

| 모듈 | apt | pip | 버전 | 빠지면 잃는 것 |
|---|---|---|---|---|
| `psutil` | `python3-psutil` | `psutil>=5.9` | 7.0.0 | 인터페이스 주소·NIC 카운터·LAN 서브넷 자동탐지 |
| `smbus2` | `python3-smbus2` | `smbus2>=0.4` | 0.4.3 | I2C 주소 스캔, 레지스터 읽기/쓰기 |
| `spidev` | `python3-spidev` | `spidev>=3.5` | 3.6 | SPI 모드/클럭 상세, 루프백 테스트 |
| `serial` | `python3-serial` | `pyserial>=3.5` | 3.5 | USB-시리얼 출처 추적, 시리얼 모니터 |
| `lgpio` | `python3-lgpio` | `lgpio>=0.2` | 0.2.2.0 | gpiochip 라인 점유 정보 |
| `picamera2` | `python3-picamera2` | **apt만** | 0.3.36 | libcamera CSI 카메라 열거, 촬영 |
| `PIL` | `python3-pil` | `Pillow>=10.0` | 11.1.0 | `floppy boot` 스크린샷 PNG 변환 (없으면 .ppm) |

> **`picamera2`는 pip로 넣지 말 것.** libcamera 바인딩이 필요한데 pip가 빌드하지
> 못한다. 반드시 apt로.

### 개발용

| 용도 | apt | pip |
|---|---|---|
| 테스트 | — | `pytest>=7.0` (stdlib `unittest`로도 전부 돌아감) |
| 휠 빌드 | `python3-setuptools` `python3-wheel` | `build>=1.0` |

---

## 2. 외부 명령

코드가 `shutil.which`로 존재를 확인하고 없으면 조용히 건너뛴다. **하나도 필수가 아니다.**

| 명령 | apt 패키지 | 쓰는 곳 |
|---|---|---|
| `vcgencmd` | `raspi-utils-core` | PMIC 레일 전력, 클럭, throttle 플래그, 부트로더 |
| `pinctrl` | `raspi-utils-core` | 40핀 헤더 mux/풀업/레벨 |
| `lsblk` | `util-linux` | 블록 장치 열거 (**storage 백엔드의 유일한 필수**) |
| `ip` | `iproute2` | 기본 라우트 (없으면 `/proc/net/route`로 대체) |
| `ping` | `iputils-ping` | LAN 스윕 (**lan 백엔드 필수**) |
| `iw` | `iw` | Wi-Fi SSID·신호세기·대역 |
| `bluetoothctl` | `bluez` | 블루투스 컨트롤러 상태, 페어링 목록 |
| `v4l2-ctl` | `v4l-utils` | V4L2 포맷·해상도 목록 |
| `rpicam-hello` | `rpicam-apps-core` | CSI 센서 모드 목록 |
| `rpi-eeprom-update` | `rpi-eeprom` | EEPROM 업데이트 여부 |
| `systemd-hwdb` | `udev` | MAC OUI → 제조사 조회 |
| `qemu-system-i386` | `qemu-system-x86` | `floppy boot` — VM 부팅 |

### 데이터 파일

| 경로 | apt 패키지 | 용도 |
|---|---|---|
| `/usr/share/misc/usb.ids` | `usb.ids` | USB 벤더/제품 이름 |
| `/usr/share/misc/pci.ids` | `pci.ids` | PCI 벤더/제품 이름 |
| systemd hwdb (`20-OUI.hwdb`) | `udev` | IEEE OUI 등록부 |

---

## 3. 권한 — 설치가 아니라 그룹

| 하려는 것 | 필요한 것 | 확인 |
|---|---|---|
| 스캔·디스크립터 읽기 | **없음** | `/dev/bus/usb`가 `crw-rw-r--` |
| I2C 주소 스캔 | `i2c` 그룹 | `sudo usermod -aG i2c $USER` |
| SPI 접근 | `spi` 그룹 | `sudo usermod -aG spi $USER` |
| GPIO 라인 정보 | `gpio` 그룹 | `sudo usermod -aG gpio $USER` |
| 시리얼 포트 | `dialout` 그룹 | `sudo usermod -aG dialout $USER` |
| QEMU USB 패스스루 | `/dev/bus/usb` **쓰기** | udev 규칙 (아래) |

```bash
# USB 패스스루용 쓰기 권한 (이것만 별도 설정이 필요하다)
echo 'SUBSYSTEM=="usb", MODE="0660", GROUP="plugdev"' \
  | sudo tee /etc/udev/rules.d/70-updev-usb.rules
sudo udevadm control --reload && sudo udevadm trigger
```

`updev usb permissions` 로 현재 상태를 확인할 수 있다.

### 버스 활성화

I2C·SPI는 기본적으로 꺼져 있다. `updev doctor`가 이걸 잡아내고 명령까지 알려준다.

```bash
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_spi 0
sudo reboot
```

---

## 4. 휠

```
dist/updev-1.1.0-py3-none-any.whl      # 순수 파이썬, 아키텍처 무관
dist/updev-1.1.0.tar.gz                # 소스 배포본
```

### 빌드

```bash
python3 -m pip wheel . --no-deps --no-build-isolation -w dist/
```

`--no-build-isolation`을 쓰는 이유: 시스템에 `setuptools`·`wheel`이 이미 있고,
격리 빌드는 PyPI에서 다시 받으려 하기 때문이다. 네트워크 없이도 빌드된다.

### 설치

```bash
pip install --user dist/updev-1.1.0-py3-none-any.whl        # 사용자 홈에
pipx install dist/updev-1.1.0-py3-none-any.whl              # 격리 설치 (pipx 필요)
```

> 시스템 파이썬이 **externally-managed**라 `sudo pip install`은 거부된다.
> `--user`, `pipx`, 또는 venv를 쓸 것.

venv에 넣을 때는 apt로 깔린 하드웨어 라이브러리를 보이게 해야 한다:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install dist/updev-1.1.0-py3-none-any.whl
```

`--system-site-packages` 없이 만들면 `picamera2`·`spidev`·`lgpio`가 안 보여서
해당 백엔드가 조용히 빠진다.

### 오프라인 번들

의존성 휠까지 통째로 받아두려면 (네트워크 있는 곳에서):

```bash
pip download -d wheels/ rich click psutil smbus2 spidev pyserial lgpio Pillow
pip install --user --no-index --find-links wheels/ dist/updev-1.1.0-py3-none-any.whl
```

`wheels/`는 `.gitignore`에 있으니 저장소에 딸려가지 않는다.

---

## 5. 검증

```bash
python3 -m unittest discover -s tests   # 147개, 하드웨어 없이 돌아감
bin/updev backends                       # 어느 백엔드가 살아있나
bin/updev doctor                         # 빠진 것 + 꺼진 것 진단
```

`updev backends` 출력에서 `○`는 그 백엔드가 못 돌았다는 뜻이고, note 칸에 이유가 나온다.
