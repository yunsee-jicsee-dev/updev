# updev

라즈베리파이(그리고 웬만한 리눅스 보드) 하나에 물린 **모든 것**을 한 곳에서 보는 장치관리자.

LAN · USB · I2C · SPI · Serial/UART · 카메라 · GPIO · 스토리지 · 블루투스 · NFC,
그리고 보드 자신까지. `lsusb`, `i2cdetect`, `lsblk`, `ip`, `vcgencmd`, `pinctrl`,
`v4l2-ctl`을 따로 치고 머릿속에서 합치던 걸 하나의 모델로 묶었다.

그리고 판정에서 멈추지 않는다 — **뭔지 알아낸 다음, 그걸로 뭘 할 수 있는지까지 준다.**

```bash
bin/updev            # 전체 스캔
bin/updev usb zone   # 꽂으면 뭔지 판정하고, 맞는 툴까지
bin/updev tools      # 붙어있는 것들로 지금 할 수 있는 것
bin/updev gui        # 기기별 에디터 GUI
bin/updev watch      # 라이브 대시보드
bin/updev doctor     # 뭐가 잘못됐는지 + 고치는 명령어까지
```

장치를 앞에 써도 됩니다 — `updev 3-2 gui`, `updev sda tools`, `updev eth0 show`.

---

## 왜 이게 필요한가

기존 도구들은 각자 자기 버스만 안다. `updev`는 세 가지를 더 한다.

**1. 진단하고 해결책을 준다.** 장치 목록만 뱉는 게 아니라 문제를 짚고 실행할 명령을 같이 준다.

```
▲ WARN
 40-pin SPI    SPI is not enabled
               Without it, GPIO 7-11 stay general-purpose and nothing you wire
               to the SPI pins can be reached.
               $ sudo raspi-config nonint do_spi 0 && sudo reboot
```

**2. 거짓말하는 하드웨어를 잡아낸다.** 파이 5의 내부 I2C 컨트롤러는 아무것도 없는 주소에도
ACK를 돌려준다. 그래서 `i2cdetect -y 13`은 존재하지 않는 장치 **117개**를 보고한다.
`updev`는 결과가 물리적으로 불가능하다는 걸 알아채고 읽기 모드로 자동 재검사한 뒤,
왜 숫자가 달라졌는지 설명한다.

**3. USB 장치가 뭔지 알아낸다.** 카메라·키보드·마우스·무선랜·휴대폰·AUX 어댑터·허브를
가려내고, 저장장치는 다시 썸드라이브·플로피·외장HDD·외장SSD·광학드라이브로 세분한다.
USB에는 "나는 플로피다"라고 말하는 필드가 없으므로, 규격이 강제하는 시그니처를 읽어
가중치를 매기고 **판단 근거를 전부 보여준다**. → [USB 체험존](#usb-체험존)

**4. 물리적 토폴로지를 복원한다.** USB 디스크는 자기가 꽂힌 포트 아래에 붙고,
`updev usb path`는 루트허브부터 장치까지의 경로와 **어디가 병목인지** 짚어준다.

```
├── ● usb4 root hub (USB 3)  usb4  root hub · 1 ports · SuperSpeed (5 Gbps)
│   └── ● USB 3.1 Device  4-1  mass storage · SuperSpeed (5 Gbps) · driver uas
│       └── ● sda  465.8G · disk · SHGP31-500GM · USB
│           ├── ● sda1  512.0M · part · → /boot/firmware
│           └── ● sda2  465.3G · part · → /
```

**5. 판정하고 끝내지 않는다.** 뭔지 알아낸 다음 그 장치에 맞는 명령을 같이 준다.
USB 플로피면 플로피 툴, 썸드라이브면 실측 벤치, 키보드면 evdev 탭, 카메라면 camtoy.
점퍼선으로 무는 NFC 리더처럼 **스스로를 알릴 방법이 없는 장치**는 반대로 — 배선표를
먼저 주고, 물린 다음에 물어본다. → [판정 다음](#판정-다음--뭘-할-수-있는가) ·
[NFC](#nfc--점퍼선으로-무는-리더)

---

## 설치

추가 설치 없이 그냥 돈다. 파이 OS에 이미 있는 것만 쓴다.

```bash
bin/updev scan
```

자주 쓸 거면 alias 하나 걸어두면 편하다:

```bash
echo "alias updev='$PWD/bin/updev'" >> ~/.bashrc && . ~/.bashrc
```

`rich`와 `click`만 필수고, 하드웨어 라이브러리는 **전부 선택**이다. 없으면 그 백엔드만
조용히 빠지고 나머지는 정상 동작한다. 더 쓰고 싶으면 (pip 말고 apt로 — 시스템 파이썬은
externally-managed다):

```bash
sudo apt install python3-psutil python3-smbus2 python3-spidev python3-serial python3-lgpio
```

시스템 전역 명령으로 깔려면:

```bash
pipx install .        # 또는: pip install --user .
```

---

## 명령어

### 전체 조망

| 명령 | 하는 일 |
|---|---|
| `updev scan` | 종류별로 묶어서 전부 나열 |
| `updev tree` | 물리적 연결 구조를 트리로 |
| `updev show <무엇이든>` | 한 장치의 모든 것 (uid·이름·주소·/dev 경로 아무거나) |
| `updev doctor` | 문제 진단 + 해결 명령 |
| `updev watch` | 라이브 대시보드 |
| `updev activity` | 장치 착탈 이벤트 실시간 로그 |
| `updev usb zone` | USB 체험존 — 꽂으면 분류하고, 맞는 툴까지 |
| `updev tools [무엇이든]` | 이 장치로 지금 할 수 있는 명령들 |
| `updev gui [무엇이든]` | 기기별 에디터 GUI (`updev <장치> gui` 도 됨) |
| `updev usb path <주소>` | 물리 경로 + 병목 지점 |
| `updev floppy make` | 부팅 대신 아트가 뜨는 플로피 이미지 |
| `updev floppy boot` | QEMU로 띄우고 화면 캡처 |
| `updev disk bench <노드>` | 실제 읽기 속도 + 랜덤 접근 지연 (읽기 전용) |
| `updev hid watch <대상>` | 키보드·마우스가 보내는 이벤트 그대로 |
| `updev nfc wiring` | 점퍼선 NFC 리더 배선표 |
| `updev nfc read` · `poll` · `dump` | 태그 UID · MIFARE 섹터 (읽기 전용) |
| `updev usb descriptors <주소>` | 원시 디스크립터 (엔드포인트·alt·IAD) |
| `updev usb permissions` | USB 접근 권한 현황 |
| `updev help [주제]` | 주제별 설명 (분류가 뭔지, 왜 그런지) |
| `updev export out.json` | 전체 인벤토리 파일로 |

```bash
updev scan -k usb -k i2c        # 종류로 필터
updev scan -s degraded          # 상태로 필터
updev scan --deep               # 느리지만 철저하게 (버스 주소 스캔 포함)
updev show eth0
updev show 4-1
updev show /dev/i2c-14
```

### 버스별

```bash
updev i2c scan              # 모든 버스 주소 스캔
updev i2c scan 1            # 특정 버스만
updev i2c read 1 0x76 0xd0  # BME280 chip-id 레지스터 읽기
updev i2c dump 1 0x50       # 레지스터 공간 통째로 헥스 덤프

updev spi test 0.0          # MOSI-MISO 점퍼 물리고 루프백 테스트
updev spi xfer 0.0 0x9f --read 3 --yes   # 플래시 JEDEC ID 읽기

updev cam list              # 카메라 (ISP/코덱 노드는 걸러냄)
updev cam capture 0 -o shot.jpg
updev cam modes 0

updev net scan              # 서브넷 스윕 (/24 기준 약 1.2초)
updev net scan --cidr 192.168.0.0/24
updev bt scan

updev nfc wiring            # RC522 / PN532 배선표 — 어느 핀에 뭘
updev nfc detect -v         # 물린 리더가 대답하는지
updev nfc poll              # 태그 올릴 때마다 한 줄

updev gpio pins             # 40핀 헤더를 실물 배치대로
updev serial ports
updev serial monitor ttyUSB0 -b 115200
updev power                 # PMIC 레일별 전압/전류/전력
```

모든 명령에 `--json`이 붙는다.

```bash
updev --json scan | jq '.devices[] | select(.status=="degraded")'
updev --json doctor | jq '.counts'
```

---

## 스크린샷 몇 개

### `updev gpio pins`

40핀 헤더를 실제 물리 배치 그대로 그린다. 전원·GND·현재 mux 상태·풀업/다운·핀 레벨까지.

```
 state         function             name     pin       pin   name     function            state
               power                3V3       1    │    2    5V       power
 -             SDA1 / I2C           GPIO2     3    │    4    5V       power
 -             SCL1 / I2C           GPIO3     5    │    6    GND      ground
 in high ↑     ID_SD / HAT EEPROM   GPIO0    27    │   28    GPIO1    ID_SC / HAT EEPROM  in high ↑
```

### `updev power`

파이 5 PMIC에서 레일별로 읽어 실제 소비 전력을 계산한다. 추정이 아니라 측정값이다.

```
rails  VDD_CORE  0.888V @   785.8mA =  0.698W
       3V3_SYS   3.313V @   154.2mA =  0.511W
       1V8_SYS   1.821V @   259.6mA =  0.473W
       DDR_VDD2  1.108V @   350.4mA =  0.388W
       ...
rail total  3.00W
  input 5v  5.036V
```

### `updev net scan`

권한 상승 없이(`ping` 바이너리 + 커널 ARP 테이블) 254개 주소를 1.2초에 훑는다.
scapy도 root도 필요 없다.

```
 ●  172.30.1.58   48:d8:90:e5:36:66  FN-LINK TECHNOLOGY  22 (ssh), 80 (http), 443 (https)
 ●  172.30.1.81                      this Pi             22 (ssh), 80 (http)
 ●  172.30.1.254  28:4e:e9:18:5f:69  mercury corperation 80 (http)
```

### `updev watch`

vitals(온도/전력/부하/메모리 게이지) · trends(스파크라인) · devices · **activity**.
activity 패널은 스캔 간 차이를 계산해서, USB를 꽂는 순간 그 줄이 뜬다.

---

## USB 체험존

```bash
updev usb zone          # 꽂으면 바로 판정 + 그걸로 할 수 있는 것
updev usb zone --storage-only   # 예전처럼 저장장치만
updev usb classify      # 지금 붙어있는 것 판정
updev usb path 1-2.4    # 물리 경로 + 병목
updev usb signatures    # 규칙표 보기
```

저장장치면 아래의 시그니처 분류기를 타고, 그 외의 장치는 **역할 판정**(키보드·카메라·
무선랜·휴대폰·허브…)을 탄다. 어느 쪽이든 판정 카드 밑에 **그 장치로 실행할 수 있는
명령**이 따라붙는다.

### 먼저: 이게 뭔 장치인가

저장장치 분류 이전에 **역할**부터 가린다. 근거는 두 가지고, 신뢰 순서가 있다.

**1순위 — 커널이 실제로 붙인 것.** 인터페이스 밑에 `net/`이 생기고 `phy80211`이 있으면
그건 해석의 여지 없이 Wi-Fi 어댑터다. `video4linux/`는 카메라, `sound/`는 오디오,
`tty/`는 시리얼. 드라이버가 하드웨어를 실제로 잡았다는 증거라 디스크립터보다 강하다.

**2순위 — 인터페이스 디스크립터.** 장치가 스스로 주장하는 것.

| 역할 | 시그니처 |
|---|---|
| 키보드 / 마우스 | HID 부트 프로토콜 `0x01` / `0x02` |
| 카메라 | `video4linux` 바인딩, 또는 클래스 `0x0E` (UVC) |
| 오디오 / **AUX 어댑터** | `sound` 바인딩, 또는 클래스 `0x01` (USB Audio Class) |
| **무선랜** | `net` 바인딩 **+ `phy80211` 존재** |
| 유선랜 | `net` 바인딩 (phy80211 없음) |
| **휴대폰** | ADB `0xFF/0x42/0x01`, PTP `0x06`, Apple `0xFF/0xFE/0x02` |
| 저장장치 | 클래스 `0x08` → 아래에서 다시 세분 |
| 디스플레이 어댑터 | DisplayLink 등 벤더 ID |
| **USB-C Billboard** | 클래스 `0x11` — alt mode 협상 실패 알림 |

복합 장치는 역할이 여러 개인 게 **사실**이라 전부 보여준다. 무선 리시버는 정말로
키보드이면서 마우스다:

```
1-2.2  YICHIP 2.4G Receiver    키보드 + 마우스
         [interface descriptor] 0x03/0x01/0x01 — HID boot keyboard
         [interface descriptor] 0x03/0x01/0x02 — HID boot mouse
```

### 그다음: 저장장치라면 어떤 저장장치인가

| 코드 | 뜻 |
|---|---|
| `NUSB` | 단순 USB — 일반 플래시 드라이브 |
| `FUSB` | 플로피 — USB FDD |
| `HUSB` | 외장 HDD — 회전 디스크 |
| `SUSB` | 외장 SSD — 솔리드 스테이트 |
| `ODD` | 광학 드라이브 |

### 어떻게 구분하나

USB에는 "나는 외장SSD다"라고 알려주는 필드가 **없다**. 썸드라이브와 외장SSD는 둘 다
USB 브릿지 뒤의 플래시라서 겉보기로는 똑같다. 그래서 규격이 강제하는 시그니처 6개를 읽는다.
전부 root 없이 sysfs에서 읽힌다.

| # | 시그니처 | 출처 | 왜 쓸모있나 |
|---|---|---|---|
| 1 | `bInterfaceSubClass` | USB 인터페이스 디스크립터 | `0x04`가 문자 그대로 **UFI = USB Floppy Interface**. `0x02`는 ATAPI/MMC-5라 광학드라이브만 씀 |
| 2 | `bInterfaceProtocol` | 〃 | CBI(`0x00/0x01`)는 플로피 전용 유물, UAS(`0x62`)는 싸구려 스틱이 못 하는 기능 |
| 3 | SCSI peripheral type | INQUIRY byte 0 | `0x05`면 CD/DVD, 논쟁 끝 |
| 4 | **RMB** (removable medium) | INQUIRY byte 1 bit 7 | 스틱·플로피는 1, 드라이브를 품은 인클로저는 0 |
| 5 | **Medium Rotation Rate** | **VPD page 0xB1** bytes 4-5 | 드라이브가 자기 회전수를 직접 신고. `0x0001`=비회전, `0x0401` 이상=실제 RPM |
| 6 | **제품 문자열** | USB 디스크립터 + SCSI INQUIRY | 인클로저가 "M.2 SATA"라고 스스로 밝히는 경우가 많다 |
| 7 | 용량 · form factor | block layer / VPD 0xB1 | 보조 근거로만 씀 |

6번은 실물 검증 중에 추가됐다. `rotational=1`을 믿고 어떤 1TB 외장을 HUSB로 판정했는데,
SCSI INQUIRY 문자열이 **"M.2 SATA"**였다 — 인클로저가 스스로 SSD라고 말하고 있었고
회전 플래그가 거짓말을 한 거다. 규칙 설명에 "브릿지가 이 플래그를 거짓말한다"고
써놓고 정작 내가 거기 걸렸다. 지금은 제품 문자열이 회전 플래그를 이긴다.

**5번이 HDD/SSD를 가르는 결정타다.** 그리고 어려운 건 `NUSB` vs `SUSB`인데, 여기선 RMB(4번)와
VPD 0xB1의 존재 자체(5번)가 갈라준다 — 싸구려 스틱은 선택적인 VPD 페이지를 아예 구현하지 않는다.

규격이 명확히 못박은 규칙은 `decisive`로 표시되고, 하나라도 걸리면 **즉시 확정하고 나머지는
보지 않는다**. 나머지는 가중치를 더해 최고점을 고르고, 2위와의 격차로 신뢰도를 매긴다.

### 판정 결과는 항상 근거를 달고 나온다

```
  SUSB     외장 SSD (solid-state)
  confidence: high   margin 90   (2nd: HUSB)

  USB 3.1 Device   465.8 GB · /dev/sda · usb 4-1

  근거 (signature → verdict)
      USB bInterfaceProtocol          0x62 — UAS              SUSB+45 HUSB+22 NUSB-22
    UAS needs a real SCSI-capable bridge chip. Enclosures ship it; commodity
    flash drives don't.

      SCSI RMB bit                    0 — fixed medium        SUSB+22 HUSB+22 NUSB-9
    A fixed medium behind a USB bridge is the signature of an enclosure holding
    a real drive.

      VPD 0xB1 medium rotation rate   0x0001 — non-rotating   SUSB+22 NUSB+9 HUSB-45
    Explicitly solid-state. Implementing this page at all points to a real
    controller rather than a commodity stick.
```

틀린 답이 나와도 **왜 틀렸는지 따질 수 있게** 만든 게 요점이다. 점수판도 같이 나온다.

분류 결과는 `scan`에도 반영된다:

```bash
updev scan -t SUSB      # 태그로 필터
updev scan -k usb       # SUSB · mass storage · SuperSpeed (5 Gbps)
```

### `updev usb path` — 경로와 병목

루트허브부터 장치까지 거쳐온 허브와 포트를 그리고, **어디서 느려졌는지** 짚는다.
중요한 건 범인을 잎이 아니라 **원인 홉**으로 지목한다는 것 — 아래 예에서 느린 건
카메라가 아니라 허브고, 카메라는 그냥 상속받았을 뿐이다.

```
╭─ USB 경로 · 1-2.4 ──────────────────────────────────────────────────────────╮
│         Raspberry Pi (host)                                                 │
│  └─     usb1 root hub                usb1    High-Speed 480 Mbps  root hub  │
│   └─    USB2.0 HUB                   1-2     Full-Speed 12 Mbps   ◀ 병목    │
│     └─  Generic USB2.0 PC CAMERA ←   1-2.4   Full-Speed 12 Mbps   port 4    │
│                                                                             │
│   ▲ USB2.0 HUB declares USB 2.00 but negotiated only Full-Speed 12 Mbps.    │
│     Everything below it is capped there — usually a cable, a worn           │
│     connector, or a port that only wires USB 2 pins.                        │
│                                                                             │
│        역할   camera                                                        │
│       input  /dev/input/input29                                             │
│ video4linux  /dev/video0, /dev/video1                                       │
│       sysfs  /sys/bus/usb/devices/1-2.4                                     │
╰─────────────────────────────────────────────────────────────────────────────╯
```

판정 기준은 **선언한 규격(bcdUSB) 대비 실제 협상 속도**다. Low-Speed 키보드처럼
원래 느린 장치는 경고하지 않는다.

### `updev usb descriptors` — 원시 디스크립터

sysfs는 장치를 **요약**하지만, 디스크립터는 장치 **그 자체**다. sysfs가 안 보여주는 것들:

```bash
updev usb descriptors 1-2.4
updev usb permissions          # 뭐가 되고 뭐가 안 되는지
```

```
source  /dev/bus/usb/001/006  (usbfs, 253 bytes)
access  read · write no  (control transfers and QEMU passthrough need it)

configuration 1   2 interfaces · 256 mA
  ⇥ association: interfaces 0..1 = one function (class 0x0e)
 iface   class/sub/proto   endpoints                          class-specific
 0.0     0x0e/0x01/0x00    EP1 IN interrupt 10B interval 5    VC Header · Input Terminal · …
 1.0     0x0e/0x02/0x00    none                               VS Input Header · Uncompressed …
 1.1     0x0e/0x02/0x00    EP2 IN isochronous 768B interval 1
```

- **모든 alternate setting** — sysfs는 활성 alt만 보여준다. 외장SSD를 보면 alt 0은 BOT,
  **alt 1은 UAS**로 둘 다 제공한다는 게 드러난다
- **전체 엔드포인트 맵** — 전송 타입·패킷 크기·폴링 간격, SuperSpeed면 burst 깊이까지
- **Interface Association Descriptor** — 복합 장치를 실제로 묶는 단위
- **class-specific 디스크립터** — UVC의 VideoControl/VideoStreaming, HID report 크기
- **구성별 실제 전력 예산**

**isochronous 엔드포인트**가 특히 유용하다. USB 규격상 데이터가 흐르든 말든 마이크로프레임마다
대역폭을 예약하므로, **진짜 스트리밍 하드웨어만 요청할 수 있다.** 아무도 흉내낼 수 없는 신호라
카메라/오디오 판정의 보강 근거로 쓴다.

#### 권한

| | 필요한 것 | 이유 |
|---|---|---|
| 디스크립터 읽기 | **없음** | `/dev/bus/usb`가 `crw-rw-r--` |
| 컨트롤 전송 · QEMU 패스스루 | 쓰기 권한 | sudo 또는 udev 규칙 |

```bash
echo 'SUBSYSTEM=="usb", MODE="0660", GROUP="plugdev"' | sudo tee /etc/udev/rules.d/70-updev-usb.rules
sudo udevadm control --reload && sudo udevadm trigger
```

### `updev activity`

착탈 이벤트만 흘려보는 스크롤 로그. 전체화면을 안 잡으므로 파이프가 된다.

```bash
updev activity                    # ctrl-c 까지
updev activity -d 30              # 30초만
updev --json activity | jq .      # 이벤트당 JSON 한 줄
```

---

## 판정 다음 — 뭘 할 수 있는가

분류기는 "이건 FUSB다"까지 말하고 멈춘다. 그 다음 질문이 진짜 질문이다: **그래서 뭘 하지?**

```bash
updev tools             # 붙어있는 것 전부
updev tools 3-2         # 하나만
updev tools --brief     # 한 줄씩
```

```
 FUSB   TEACV0.0   플로피 (floppy / UFI)
★     아트 플로피 이미지 만들기             updev floppy make -o updev-art.img
      쓰기 전에 아트만 보기                 updev floppy show
      실물 디스켓에 굽기                    sudo dd if=updev-art.img of=/dev/sdb bs=512
      디스크를 되읽어 BPB 확인              sudo updev floppy info /dev/sdb
      QEMU로 부팅시켜 보기                  updev floppy boot --usb 3-2

 SUSB   USB 3.1 Device   외장 SSD (solid-state)
★     실제 읽기 속도 측정                   updev disk bench /dev/sda
      SMART 건강 상태                       sudo smartctl -a /dev/sda   (needs smartctl)
      UAS/BOT alt setting 확인              updev usb descriptors 4-1

 keyboard+mouse   YICHIP 2.4G Receiver   키보드 + 마우스
★     누르는 키·움직임 실시간으로 보기      updev hid watch 1-2
```

**플로피 툴은 플로피 것이다.** `updev floppy`는 1.44MB 이미지를 물리 매체에 쓰는 물건이라,
그걸 대표 툴로 받는 건 FUSB뿐이다 — UFI 드라이브만이 그 이미지를 실제 디스크로 되돌려받을
수 있으니까. 나머지 분류는 각자 맞는 게 따로 붙는다.

| 장치 | 대표 툴 | 왜 그게 맞는가 |
|---|---|---|
| **FUSB** 플로피 | `updev floppy` | 이 버스에서 1.44MB 이미지를 실물로 받을 수 있는 유일한 장치 |
| **NUSB/HUSB/SUSB** | `updev disk bench` | 협상 속도는 천장일 뿐, 매체가 실제로 주는 값은 재봐야 안다 |
| **ODD** | `updev show` + bench | 트레이에 뭐가 들었고 읽히는지가 전부 |
| 카메라 | `camtoy` | UVC 노드는 프레임 소스고, 소비자는 같은 저장소 안에 있다 |
| 키보드·마우스 | `updev hid watch` | 커널이 이미 evdev로 디코드해놨다 |
| 시리얼 | `updev serial monitor` | tty가 붙었다는 건 이미 바이트가 흐른다는 뜻 |
| 무선랜·유선랜 | `updev net scan --iface` | 어댑터는 뭘 닿을 수 있는지부터가 본론 |
| 휴대폰 | `adb devices` | 0xFF/0x42/0x01이 ADB니까, 반대편도 동의하는지 물어본다 |
| 허브 | `updev usb path` | 아래쪽 전부의 속도를 정하는 게 허브다 |
| USB-C Billboard | `updev usb descriptors` | 이 클래스는 alt mode 실패를 알리려고만 존재한다 |

- **★** 가 그 장치의 대표 툴이다. `updev show <장치>` 밑에도 같은 목록이 붙는다.
- 안 깔린 명령도 목록에서 빼지 않는다 — 대신 `needs smartctl`처럼 뭘 깔아야 하는지 붙는다.
- 매체를 **덮어쓰는 명령은 `덮어씀`으로 표시되고, updev가 대신 실행하지 않는다.** 명령줄을
  보여주고 사람이 직접 치게 한다.

규칙표는 `usbclass.classify()`와 같은 방식으로 **순수 함수**다 (`toolkit.tools_for()`).
플로피도 광학드라이브도 NFC 모듈도 없이 전 분기를 테스트할 수 있다.

### `updev disk bench` — 링크 속도 말고 매체 속도

```bash
updev disk bench /dev/sda
updev disk bench updev-art.img      # 이미지 파일도 된다
```

```
              device  /dev/sda   465.8G
     sequential read  412.7 MB/s   (median chunk 419.0 MB/s, 8 × 4 MiB)
     versus the link  83% of the 5000 Mbps link — medium and bus are in the same league
random read (median)  0.14 ms   → solid-state
                      median random read 0.14 ms — no mechanism can move a head
                      that fast, so this is flash
                  io  O_DIRECT

  분류기와 실측: SUSB says solid-state  ·  measured solid-state   일치
```

랜덤 접근 지연이 재미있는 쪽이다. 분류기는 **VPD 0xB1** — 드라이브 자기 주장 — 으로
HUSB/SUSB를 가른다. 실측 지연은 그 주장을 뒷받침하거나, **브릿지가 거짓말했다는 걸 잡아낸다.**
플래터는 회전 지연만으로 4ms를 못 내려가고, 플래시는 거기서 한두 자릿수 아래다.

읽기 전용이다. `O_DIRECT`로 페이지 캐시를 우회하고, 커널이 거부하면
`posix_fadvise(DONTNEED)`로 캐시를 떨군 뒤 읽는다. 캐시가 답한 게 뻔한 수치(0.01ms 미만)는
`cached`로 표시하고 **매체에 대해 아무 말도 하지 않는다.**

### `updev hid watch` — 키보드가 실제로 보내는 것

```bash
updev hid list
updev hid watch 1-2          # USB 주소 · input29 · event5 · 이름 조각 다 됨
```

```
watching /dev/input/event5   YICHIP 2.4G Receiver
watching /dev/input/event7   YICHIP 2.4G Receiver Mouse
이 장치는 키 입력을 보냅니다 — 여기 찍히는 건 실제로 입력되는 내용입니다.

19:25:41 event7  REL_X -3
19:25:41 event7  REL_Y +2
19:25:43 event5  KEY_A press
```

라이브러리는 안 쓴다. `input_event`는 64비트 커널에서 24바이트고 `struct`가 폭을 알아서
맞춘다. 복합 리시버는 실제로 event 노드가 여러 개라 전부 같이 본다.

키보드 이벤트를 읽는다는 건 **거기 입력되는 모든 것을 읽는다는 뜻이다.** 그래서 대상이
필수고("전부 보기" 모드는 없다), 시작하기 전에 뭘 열었는지 먼저 찍는다.

---

## `updev gui` — 기기별 에디터

```bash
updev gui              # 전부 훑어보기
updev gui 3-2          # 이 장치로 바로
updev 3-2 gui          # 장치를 앞에 써도 된다
```

왼쪽은 `updev scan`과 같은 목록, 오른쪽은 **그 장치의 속을 여는 패널**입니다. 창은 두 번째
프로그램이 아니라 같은 모델을 보는 두 번째 화면입니다 — 목록도, 순서도, 분류도 CLI와 같은
것에서 나옵니다.

| 장치 | 열리는 패널 | 하는 일 |
|---|---|---|
| **USB 플로피 (FUSB)** | **플로피 (FAT12) 에디터** | 파일 추가·수정·삭제, 부트섹터 메시지 편집, 다시 구워서 저장하거나 디스켓에 쓰기 |
| **NFC 리더** | **태그 에디터** | 카드 전체 덤프, 섹터별 접근 조건 해독, 블록 편집, NDEF, .mfd 저장/열기 |
| 블록 장치 | 섹터 뷰어 | 512바이트 단위 hex — **읽기 전용** |
| I2C 칩 | 레지스터 에디터 | 레지스터 공간 읽기, 한 바이트씩 쓰기 |
| GPIO | 핀 에디터 | 40핀 현재 상태, (잠금 해제 시) 출력 토글 |
| 키보드·마우스 | 입력 이벤트 | evdev 이벤트 실시간 |
| 카메라 | 카메라 | 포맷 목록, 한 장 찍어서 바로 보기 |
| USB 시리얼 | 시리얼 터미널 | 포트 열어서 보고, 한 줄 보내기 |
| 무선랜·유선랜 | 네트워크 | 링크 정보 + 서브넷 훑기 |
| 허브 | 허브 포트 | 포트별로 뭐가 걸렸고 뭘 협상했는지 |
| **모든 USB 장치** | **정체 · 디스크립터 · 경로** | 역할/분류 근거, 디스크립터 트리, 병목 |
| **그 외 전부** | 장치 정보 | 아는 것 전부 + 실행할 수 있는 명령 |

USB 장치는 역할별 패널 **뒤에 항상 세 개가 더 붙습니다** — 정체(왜 그렇게 판정했는지),
디스크립터(장치가 말하는 그대로), 경로(어디서 느려졌는지). updev가 자리를 못 잡는 장치야말로
디스크립터를 읽어봐야 하는 장치라서, 이 셋은 조건부가 아닙니다.

**한 장치에 패널이 여러 개면 탭으로 다 줍니다.** USB 플로피 드라이브는 FAT12 디스크이면서
동시에 블록 장치라서, 둘 중 하나를 골라주는 대신 [플로피 에디터] [섹터 뷰어] [장치 정보]가
나란히 뜹니다.

### 꼬깔 에디터

이미지나 드라이브에 든 디스켓을 열어서 루트 디렉터리를 그대로 보여주고, 파일 내용을 고치고,
**부트섹터가 찍는 메시지까지** 편집합니다. 고친 뒤에는 패치가 아니라 `build_image()`로
**1.44MB 전체를 다시 조립**합니다 — CLI가 만드는 것과 같은 경로로 나오므로, 나온 이미지는
같은 검증을 통과한 이미지입니다.

부트섹터 메시지 상자 밑에는 **남은 바이트가 실시간으로** 나옵니다. 코드+메시지가 448바이트
안에 들어가야 하는데, 그걸 굽는 순간에 아는 건 너무 늦습니다.

### 태그 에디터

꼬깔 에디터와 **같은 구조**입니다. 이미지 파일 ↔ 카드 덤프, 루트 디렉터리 ↔ 섹터 목록,
파일 내용 ↔ 블록 hex. 그래서 덤프(`.mfd`)를 리더 없이 열어서 보고 고칠 수 있고, 카드를
올린 뒤 되쓸 수 있습니다.

디스크에 없고 카드에만 있는 게 하나 있습니다 — **매체 자체에 권한이 적혀 있다는 것.**

```
접근 조건 (트레일러가 정하는 것)
C1C2C3  [(0,0,0), (0,0,0), (0,0,0), (0,0,1)]   raw FF0780
blk   4          읽기 A|B  쓰기 A|B  증가 A|B  감소 A|B
blk   7 트레일러  keyA쓰기 A  접근비트 읽기 A/쓰기 A  keyB쓰기 A
```

섹터 트레일러의 9비트(C1/C2/C3 × 4그룹)를 풀어서 **어느 블록을 어느 키로 읽고 쓸 수 있는지**
그대로 보여줍니다. 이게 있으면 "쓰기가 실패했다"가 아니라 **"그 블록은 포맷된 날부터 읽기
전용이었고, 그렇게 적어놓은 바이트가 이것"** 이라고 말할 수 있습니다.

- 접근 비트는 반전 사본과 **교차 검증**합니다. 안 맞으면 카드가 그 섹터를 거부할 수 있다고
  경고합니다 — 섹터가 벽돌이 되는 경로가 정확히 그겁니다.
- 4K 카드의 큰 섹터에서는 **그룹 하나가 블록 다섯 개**를 덮습니다. 1K만 보고 짠 해독기가
  조용히 틀리는 지점이라 따로 테스트합니다.
- `(1,1,1)` 은 **일방통행 문**입니다 — 접근 비트를 다시는 바꿀 수 없게 됩니다. 그렇게 표시합니다.

그 외에:

- **카드 전체 읽기** — 섹터마다 진행 상황을 보여주며 훑고, 열린 섹터는 `●`, 인증 실패는 `✕`.
- **기본 키로 찾기** — 규격·벤더가 공개한 기본 키(`FFFFFFFFFFFF`, MAD의 `A0A1A2A3A4A5`,
  NDEF 공개키 `D3F7D3F7D3F7` …)를 섹터별로 시도합니다. 공격 도구가 아닙니다 — 목록에 없는
  키는 아무것도 복구하지 않습니다.
- **NDEF** — TLV를 찾아서 텍스트·URI 레코드를 풀어 보여줍니다. URI 접두사 테이블까지 복원하므로
  `0x04` + `anthropic.com` 은 `https://anthropic.com` 으로 나옵니다.
- **Ultralight / NTAG** — 섹터도 키도 없는 카드는 페이지 모드로 붙습니다.

카드를 망가뜨리는 블록은 드라이버 레벨에서 막혀 있습니다:

- **block 0** — 제조사 블록. UID가 들어있고 정품 카드에선 읽기 전용입니다. 무조건 거부.
- **섹터 트레일러** — 키와 접근 비트. 명시적 override + 확인 창 두 번.
- **Ultralight 0-3 페이지** — UID·lock·OTP. OTP는 쓰면 OR로 들어가서 되돌릴 수 없습니다.

### 쓰기에 대한 태도는 CLI와 같습니다

터미널 쪽이 `--yes`를 요구하는 자리에서, 창은 **나가는 바이트를 전부 보여주는 확인 창**을
띄웁니다. 기본값은 항상 '아니오'입니다. 그리고 만들지 않은 것도 있습니다 — 마운트된
파일시스템 밑의 섹터 쓰기는 권한 문제가 아니라 고칠 방법이 없는 문제라서, 섹터 뷰어에는
쓰기 버튼 자체가 없습니다.

### 설치할 게 없습니다

tkinter는 표준 라이브러리고 파이 OS에 이미 깔려 있습니다. updev 전체가 rich·click 말고는
런타임 의존성이 없는데, GUI 때문에 그걸 깨는 건 앞뒤가 안 맞습니다.

디스플레이가 없으면 창 대신 이유를 말하고 `updev tools`를 안내합니다.

---

## 휴대용 USB-C 모니터는 왜 USB가 아닌가

찾는 곳이 다르다. `updev scan -k display`를 봐야 한다.

- **DisplayPort alt mode** — 모니터가 USB 장치로 **아예 안 나타난다**. 커넥터가 DP 레인을
  직접 나른다. 그리고 **라즈베리파이 5의 USB-C 포트는 전원 전용**이라 alt mode가 없다.
  파이에선 이 경우가 성립하지 않는다.
- **HDMI** — 파이 5에 붙은 휴대용 모니터가 실제로 쓰는 방식. 케이블 끝이 USB-C든 아니든
  상관없다. DRM 커넥터로 잡힌다.
- **DisplayLink** — 영상을 USB 3으로 인코딩해서 보낸다. 이건 진짜 USB 장치라서 벤더 ID로
  식별한다.

alt mode를 원했는데 못 받은 장치는 **USB Billboard(클래스 `0x11`)**로 자기를 알린다.
`updev`가 이걸 잡아서 왜 안 되는지 설명한다.

연결된 디스플레이는 **EDID를 파싱**해서 제조사·모델·제조연도·물리 크기·대각선 인치까지 낸다:

```bash
updev scan -k display
updev show display:HDMI-A-1
```

```
●  Samsung HDMI-A-1   HDMI · SAMSUNG · 3840x2160 · 121×68 cm

   manufacturer name  Samsung          screen size      121×68 cm
   product code       0x0f13           screen diagonal  54.6"
   manufactured       2018 week 1      modes available  21
```

---

## 플로피 아스키 아트 이미지

```bash
updev floppy show                 # 터미널에 아트만 (파일 안 만듦)
updev floppy make -o disk.img     # 1.44MB 이미지 생성
updev floppy info disk.img        # BPB + 루트 디렉터리 읽어보기
```

두 겹이다.

**부트섹터는 진짜 16비트 x86**이다. BIOS `INT 10h`로 아스키 아트를 찍고 `hlt`한다.
부팅시키면 OS 대신 아트가 나온다. **"부팅되는 대신"이 문자 그대로다.**

```bash
qemu-system-i386 -fda disk.img -boot a
```

**파일시스템은 손으로 조립한 FAT12**로, 같은 아트를 텍스트 파일로 담고 있다.
마운트해도 똑같이 보인다. `fsck.vfat` 통과 확인됨 (7 files, 오류 0).

### VM으로 바로 부팅

```bash
updev floppy boot                       # 헤드리스로 띄우고 화면 캡처까지
updev floppy boot --display gtk         # 창 띄우기
updev floppy boot --dry-run             # QEMU 명령만 보기
```

헤드리스가 기본이다. QEMU 모니터로 프레임버퍼를 긁어와 **화면에 뭐가 그려졌는지** 알려준다:

```
screenshot  /tmp/updev-boot-24220.png

12 row(s) drawn — the guest put something on screen:
  ####### ######## #.##.# ###### #.##.# ##
  #### ######.######.#### ##.##.# #### ####.## ### ###.########.######## ####
    # # # #    ##    ##     # #   # #
    # # # # # # # # # #   #  # # # #
```

### USB 패스스루

`--usb <주소>`로 호스트의 USB 장치를 게스트에 넘긴다. QEMU가 `usb-host`로 **호스트 커널에서
장치를 떼어내** 게스트에 붙이는 방식이라, 넘기는 동안 호스트는 그 장치를 잃는다.

그래서 QEMU를 띄우기 **전에** 검사한다:

| 상황 | 처리 |
|---|---|
| 마운트된 파일시스템을 담은 디스크 | **거부** — sudo로도 안 됨 |
| 허브 | **거부** — 뒤에 달린 전부가 딸려감 |
| 키보드·마우스 | 경고 + `--force` 필요 |
| 쓰기 권한 없음 | 거부 + udev 규칙 안내 |

```
✖ /dev/sda is backing a mounted filesystem (sda1, sda2) — handing it to a guest
  would pull it out from under the host

sudo 로는 해결되지 않는다 — 권한 문제가 아니라 그 장치를 넘기면 호스트가 쓰던 것을
뺏기기 때문이다.
```

권한만이 유일한 장애물일 때만 sudo/udev를 안내한다. 마운트된 디스크에 대해 sudo를
권하는 건 해결책이 아니라 시스템을 부수는 방법이라서다.

> **주의**: 512바이트 아트 부트섹터는 USB를 열거할 수 없다. QEMU는 장치를 붙여주지만
> 게스트에서 쓰는 건 없다. 게스트가 실제로 USB를 감지하게 하려면 `--image`로 진짜 OS를
> 띄워야 한다.

USB 플로피 드라이브가 있으면 이 이미지를 디스크에 쓰고 `updev usb classify`를 돌려보면
**FUSB로 확정 판정**이 나온다 — `bInterfaceSubClass 0x04`가 문자 그대로 UFI니까.

반대 방향으로도 이어져 있다. 플로피 드라이브를 꽂으면 `updev usb zone`이 FUSB로 판정하고
**이 툴을 알아서 내민다** — 이미지 만들기부터 디스켓에 굽는 명령까지. 다른 분류에는 안
붙는다. → [판정 다음](#판정-다음--뭘-할-수-있는가)

### 검증

부트섹터를 검증할 기계가 없으므로 **기계어를 직접 실행하는 미니 8086 인터프리터**를
테스트에 넣었다. `build_boot_code`가 뱉는 opcode만 정확히 구현해서, 실제 바이트를 돌리고
BIOS teletype이 출력했을 내용을 잡아낸다. 점프 변위나 `mov si` 오퍼랜드가 틀리면
실물에서 미스터리가 되는 대신 여기서 잡힌다.

```
halted cleanly = True   instructions = 2092   bytes printed = 297
exact match with boot_message(): True
```

---

## NFC — 점퍼선으로 무는 리더

USB 리더는 자기가 뭔지 밝힌다. 디스크립터가 있고, CCID 클래스가 있고, 벤더 ID가 있다.
**점퍼선으로 무는 모듈은 아무것도 밝히지 않는다.** SPI에는 열거라는 개념 자체가 없고,
I2C에는 고정 주소 하나가 있을 뿐이다.

그래서 순서가 반대다. 다른 장치는 "꽂았다 → 뭔지 알아낸다"인데, 이건 **"어디에 무는지
알려준다 → 물린다 → 물어본다"** 가 된다.

```bash
updev nfc wiring            # 1. 배선표
updev nfc detect -v         # 2. 대답하는지
updev nfc poll              # 3. 태그 올리기
```

### 1. 배선

```
╭─ MFRC522 (RC522)    [spi] ───────────────────────────────────────────────────╮
│  모듈 핀         40핀 헤더   파이 쪽 이름                                    │
│  3.3V        →       pin 1   3V3 power                                       │
│  RST         →      pin 22   GPIO25                                          │
│  GND         →       pin 6   GND (ground)                                    │
│  MISO        →      pin 21   GPIO9 / SPI0 MISO                               │
│  MOSI        →      pin 19   GPIO10 / SPI0 MOSI                              │
│  SCK         →      pin 23   GPIO11 / SPI0 SCLK                              │
│  SDA (=CS)   →      pin 24   GPIO8 / SPI0 CE0                                │
│  IRQ         →      pin 18   GPIO24 — optional, unused by updev              │
│                                                                              │
│ SPI only. 3.3V — the 5V pin will kill it. RST is not optional: the chip      │
│ comes up in an undefined state and needs the reset line held high.           │
│                                                                              │
│ 확인 방법  reads VersionReg (0x37): 0x91/0x92 is a genuine MFRC522           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

| 모듈 | 모드 | 헤더 | 주의 |
|---|---|---|---|
| **MFRC522** | SPI 전용 | 19/21/23/24 + RST 22 | RST는 선택이 아니다. 5V 물리면 죽는다 |
| **PN532** | I2C | 3 (SDA) / 5 (SCL) | 7비트 주소 `0x24` — `updev i2c scan`에도 뜬다 |
| **PN532** | SPI | 19/21/23/24 | **LSB first** — 파이 컨트롤러가 못 해서 소프트웨어로 뒤집는다 |
| **PN532** | HSU | 8 (TXD) / 10 (RXD) | TX↔RX 교차. 콘솔이 물려있으면 프레임을 다 먹는다 |

SPI나 I2C가 꺼져 있으면 배선표 밑에 **켜는 명령까지 같이** 나온다. 배선은 맞는데 아무것도
안 보이는 경우의 절반은 그냥 버스가 꺼져 있는 것이다.

### 2. 물어보기

```bash
updev nfc detect -v
```

```
 probed                     result
 PN532 · i2c-1 0x24         nothing acknowledged at 0x24 on i2c-1 — no PN532 wired
                            to this bus, or its DIP switches are not on I2C
 MFRC522 · /dev/spidev0.0   VersionReg read back 0x00 — that is an idle bus,
                            not a chip. Check 3V3, GND and the RST wire.
```

**침묵의 이유까지 말한다.** `0x00`은 "칩이 없다"가 아니라 "버스가 놀고 있다"는 뜻이고,
`0xFF`도 마찬가지다. errno 숫자를 그대로 뱉는 대신 그 주소에서의 침묵이 무슨 뜻인지 쓴다.

RC522는 `VersionReg`(0x37)를 읽어서 확인한다 — `0x91`/`0x92`가 진품, `0x88`은 흔한 클론.
PN532는 `GetFirmwareVersion`(0x02)에 답하는지로 확인한다.

### 3. 태그

```bash
updev nfc read              # 하나만
updev nfc poll              # 올릴 때마다 한 줄 — NFC판 체험존
updev nfc dump --sector 1   # MIFARE Classic 섹터
```

```
        UID  DE:AD:BE:EF
       kind  MIFARE Classic 1K
       ATQA  ATQA 0x0004 — 4-byte UID
        SAK  0x08
```

UID·ATQA·SAK를 읽고, **SAK 한 바이트로 제품군을 판정한다** (`0x08` 1K, `0x18` 4K,
`0x20` DESFire·JCOP·휴대폰 HCE). ATQA는 제품이 아니라 UID 길이와 충돌방지 방식을 말한다 —
둘을 섞지 않는 게 요점이다.

### 읽기 전용, 그리고 SPI를 함부로 건드리지 않는 이유

카드에 **쓰는 기능은 없다.** 기본 키는 공장 출하 `FFFFFFFFFFFF`이고, 키가 바뀐 카드는
인증 실패로 그렇게 말한다 — 뭘 더 시도하지 않는다.

**SPI에는 주소가 없다.** CE0으로 나간 바이트는 거기 물린 게 뭐든 그게 본다. RC522한테는
레지스터 읽기지만 OLED한테는 디스플레이 명령일 수 있다. 그래서 일반 스캔은 SPI를 건드리지
않고, `--deep`이거나 `updev nfc detect`를 직접 쳤을 때만 물어본다. I2C는 주소 하나에 대한
읽기라 그 위험이 없어서 먼저 시도한다. UART는 기본으로 꺼져 있다 — `/dev/serial0`은 보통
로그인 콘솔이 쓰고 있다.

### 순수 함수로 남긴 것

CRC_A(ISO 14443-A), PN532 프레임 조립·해석, LSB 반전, SAK/ATQA 디코딩, BCC 검증은 전부
하드웨어 없이 도는 순수 함수다. 리더가 손에 없어도 **틀리기 쉬운 부분은 전부 테스트로
덮인다** — CRC는 규격 부록의 기준값과, 프레임은 문서에 박힌 바이트열과 대조한다.

```
crc_a(00 00)          = A0 1E     ISO/IEC 14443-3 Annex B
GetFirmwareVersion    = 00 00 FF 02 FE D4 02 2A 00
```

배선표도 테스트한다. `updev nfc wiring`이 말하는 핀 번호를 **GPIO 백엔드의 헤더 맵과
교차 검증**하므로, 둘 중 한쪽에 오타가 나면 실물 앞이 아니라 여기서 잡힌다.

---

## 안전에 대해

읽기 전용이 기본이다. 버스에 뭔가 쓰거나 송신하는 동작은 전부 별도 서브커맨드로 빼고,
잘못하면 하드웨어가 상할 수 있는 건 `--yes`를 명시적으로 요구한다.

```
$ updev i2c write 1 0x50 0x00 0xff
refusing to write 0xff → i2c-1 0x50 reg 0x00
a bad register write can misconfigure or brick a chip. Re-run with --yes if
that's what you want.
```

주소 스캔은 i2cdetect와 같은 전략을 쓴다 — EEPROM 구간(0x30-0x37, 0x50-0x5F)에서는
쓰기 대신 읽기로 탐지한다. HAT EEPROM에 실수로 쓰는 사고를 막기 위해서다.

`net scan`은 능동적으로 패킷을 보낸다. 본인이 책임지는 네트워크에서만 쓸 것.

새로 붙은 것들도 같은 선을 지킨다.

- **`disk bench`** 는 읽기만 한다. 쓰기로 여는 코드 경로가 없다.
- **`nfc`** 는 카드에 쓰지 않는다. UID를 읽고 섹터를 덤프하는 것까지다.
- **SPI 탐색은 기본 스캔에서 빠진다.** SPI에는 주소가 없어서 CE0으로 나간 바이트를
  거기 물린 게 뭐든 보게 된다. `--deep`이나 `updev nfc detect`로 명시했을 때만 물어본다.
- **`hid watch`** 는 대상을 반드시 받는다. 키보드 이벤트를 읽는 건 거기 입력되는 모든 걸
  읽는 것이라, "전부 보기" 모드를 두지 않았고 시작 전에 뭘 열었는지 먼저 찍는다.
- **매체를 덮어쓰는 명령은 updev가 실행하지 않는다.** 플로피에 이미지를 굽는 `dd`는
  '덮어씀'으로 표시해서 명령줄만 보여주고, 치는 건 사람이 한다.
- **`disk` 그룹은 권하지 않는다.** 원시 섹터가 필요할 때 updev가 안내하는 건 sudo, 또는
  장치 하나에만 걸리는 udev 규칙이다. `disk` 멤버십은 모든 블록 장치를 원시로 읽고 쓰게
  해주는데, 원시 읽기는 파일 권한을 전부 우회하고 원시 쓰기는 루트 파일시스템을 직접
  고칠 수 있다는 뜻이다 — sudo보다 약한 권한이 아니라 비밀번호도 로그도 없는 root다.

  ```bash
  # USB 플로피 드라이브에만 걸리는 규칙. /dev/sda 나 SD카드에는 안 붙는다.
  echo 'SUBSYSTEM=="block", ENV{ID_BUS}=="usb", ENV{ID_TYPE}=="floppy", GROUP="plugdev", MODE="0660"' \
    | sudo tee /etc/udev/rules.d/99-updev-floppy.rules
  sudo udevadm control --reload && sudo udevadm trigger
  ```

---

## 구조

```
updev/
├── core/
│   ├── model.py      Device / Issue / Action / ScanResult — 유일한 공용 언어
│   ├── registry.py   Backend 계약 + 병렬 스캔 오케스트레이터 (백엔드별 타임아웃)
│   ├── changes.py    스캔 간 diff (대시보드·activity·체험존이 공유)
│   └── util.py       sysfs 읽기, subprocess, usb.ids/pci.ids/OUI 조회
├── usbclass.py       USB 저장장치 시그니처 분류기 (순수 함수 — 하드웨어 불필요)
├── usbrole.py        USB 장치 역할 인식 + 물리 경로 추적
├── toolkit.py        판정 → 그 장치에 맞는 툴 (순수 함수)
├── usbdesc.py        원시 USB 디스크립터 파서 (/dev/bus/usb)
├── vm.py             QEMU 런처 + USB 패스스루 안전검사
├── floppy.py         FAT12 + 16비트 부트섹터 조립기
├── nfc.py            RC522 레지스터 · PN532 프레임 · ISO 14443-A (순수 함수)
├── mifare.py         카드 기하 · 접근 비트 해독 · NDEF (순수 함수)
├── hid.py            evdev 이벤트 디코딩 (라이브러리 없이)
├── bench.py          블록 장치 읽기 속도 · 랜덤 지연 (읽기 전용)
├── helptopics.py     `updev help` 주제별 내용
├── backends/         하드웨어를 아는 유일한 곳
│   ├── host.py       보드·SoC·PMIC·써멀·팬·HAT·펌웨어
│   ├── usb.py        sysfs USB 트리 (pyusb 불필요)
│   ├── i2c.py        버스 + 주소 스캔 + 칩 식별
│   ├── spi.py        컨트롤러·CS·모드/클럭·루프백
│   ├── serial_.py    USB-serial + 온보드 UART
│   ├── camera.py     libcamera + V4L2 (파이프라인 노드 구분)
│   ├── gpio.py       gpiochip·라인 점유·40핀 헤더·1-Wire·PWM
│   ├── network.py    인터페이스 + LAN 스윕
│   ├── display.py    DRM 커넥터 + EDID 파싱
│   ├── storage.py    블록 장치·SD CID·NVMe PCIe 링크
│   ├── nfc.py        리더 탐지 + SPI/I2C/UART 트랜스포트
│   └── bluetooth.py  컨트롤러 + 페어링 목록
└── ui/
    ├── render.py     테이블·트리·상세·doctor 리포트
    ├── dash.py       라이브 대시보드
    └── zone.py       USB 체험존 (판정 + 툴) + activity 로그
gui/                  tkinter 에디터 (표준 라이브러리만)
├── app.py            창 · 장치 목록 · 어느 패널을 열지
├── base.py           Editor 계약 · hex 뷰 · 쓰기 확인 창
├── floppy.py         FAT12 에디터 (꼬깔)
├── tag.py            MIFARE 태그 에디터
├── usbpanels.py      정체·디스크립터·경로·카메라·시리얼·네트워크·허브
└── panels.py         섹터·레지스터·핀·이벤트·정보
```

`usbclass.classify()`는 **`StorageFacts` 하나만 받는 순수 함수**다. 그래서 플로피도 광학드라이브도
없이 5개 분류 전부를 테스트할 수 있다 — 규격에서 뽑은 합성 팩트를 넣으면 된다.

**백엔드 하나가 죽어도 스캔은 안 죽는다.** 각 백엔드는 스레드 풀에서 개별 타임아웃을 갖고
돌고, 예외는 잡혀서 리포트로 변환된다. `updev backends`로 확인할 수 있다.

새 백엔드 추가는 `Backend` 상속 후 `available()` / `probe()` 두 개 구현하고
`backends/__init__.py`의 `BACKEND_CLASSES`에 등록하면 끝이다. 렌더링·필터링·JSON 출력·
대시보드는 자동으로 따라온다.

---

## 테스트

```bash
python3 -m unittest discover -s tests    # pytest도 됨
```

하드웨어 없이 검증 가능한 부분을 **237개 테스트**로 덮는다:

- 리비전 디코딩, BOOT_ORDER 파싱, SCSI 필드 결합, 백엔드 격리, 핫플러그 diff, 필터링, 직렬화
- **USB 저장장치 5개 분류 전부** — 규격에서 뽑은 합성 시그니처로 각각 검증
- **USB 역할 인식** — 키보드·마우스·카메라·오디오·Wi-Fi·유선랜·휴대폰(ADB/Apple/PTP)·
  Billboard·DisplayLink·허브·복합장치
- **경로 추적** — 병목이 허브로 지목되는지, 원래 느린 장치를 오탐하지 않는지
- **EDID 파싱** — 제조사·모델·제조일·물리 크기·대각선, 잘못된 헤더 거부
- **플로피 이미지** — BPB 값, FAT12 체인 왕복(멀티 클러스터 포함), 볼륨 레이블 일치,
  그리고 **부트섹터 기계어를 미니 8086 인터프리터로 실행**해서 출력이 예상과 정확히 같은지
- **디스크립터 파싱** — 실물 웹캠에서 캡처한 블롭으로 엔드포인트·alt setting·IAD·
  class-specific·잘린 블롭 처리까지. 합성 블롭이 실물과 일치하는지 교차 검증됨
- **패스스루 안전검사** — 루트 디스크·허브가 실제로 거부되는지 (실물 대상, 없으면 skip)
- **툴 매칭** — 플로피 툴이 FUSB에만 붙는지, 덮어쓰는 명령이 그거 하나뿐인지, 분류·역할
  전부가 최소한 경로 추적은 받는지, 같은 명령이 두 번 나오지 않는지
- **NFC** — CRC_A를 규격 부록 기준값과, PN532 프레임을 문서 바이트열과 대조. 깨진 프레임
  거부, ACK 구분, LSB 반전, BCC 검증, SAK/ATQA 디코딩. 가짜 SPI로 RC522 레지스터 접근까지
- **배선표** — `updev nfc wiring`의 핀 번호를 GPIO 백엔드의 헤더 맵과 교차 검증
- **evdev 디코딩** — 키 누름/뗌, 마우스 상대 이동의 부호, 프레이밍 이벤트 걸러내기,
  짧은 read를 추측하지 않고 에러로 만드는지
- **벤치** — 회전/비회전 판정 경계, 캐시가 답한 수치를 매체 판정으로 쓰지 않는지
- **GUI** — 어느 장치에 어느 패널이 열리는지, 모든 종류가 최소한 정보 패널은 받는지,
  쓰기 가능한 패널이 그렇다고 선언하는지, hex 셀 좌표 계산, 그리고 USB 장치의 sysfs
  경로를 블록 장치로 착각하지 않는지
- **꼬깔 왕복** — 파일을 고쳐서 다시 조립한 이미지가 여전히 부팅 시그니처를 갖고, 고친
  내용이 FAT12 체인으로 되읽히는지
- **MIFARE 쓰기 가드** — block 0 거부, 트레일러 override 요구, 16바이트 아닌 블록 거부,
  NAK을 성공으로 보고하지 않는지, Ultralight 0-3 페이지 거부
- **접근 비트** — 공장 트레일러가 transport configuration으로 풀리는지, 반전 사본 검증이
  실제로 도는지, 어떤 조합에서도 key A는 읽히지 않는지, `(1,1,1)` 일방통행을 그렇게 보고하는지,
  4K 큰 섹터에서 그룹이 블록 다섯 개를 덮는지
- **NDEF** — 텍스트 레코드의 언어 헤더 제거, URI 접두사 복원, 덤프 중간에 있는 TLV 찾기,
  다중 레코드 체인, 잘린 덤프에서 예외 없이 건질 수 있는 만큼만

---

## 알려진 것들

- **파이 5 내부 I2C 버스(i2c-13/14)는 quick-write에 무조건 ACK한다.** `updev`가 감지해서
  읽기 모드로 재검사한다. `i2cdetect -y 13`은 그냥 117개를 보고한다.
- **`/dev/gpiochip4`는 `gpiochip0`의 별칭이다.** 장치 번호로 중복을 제거한다.
- SPI/I2C가 꺼져 있으면 장치가 안 보이는 게 아니라 **왜 안 보이는지**를 보고한다.
- 루트 권한은 필요 없다. I2C 주소 스캔에는 `i2c` 그룹, SPI에는 `spi` 그룹 멤버십이 필요하고,
  없으면 그 사실을 알려준다.
- **USB 분류기는 실물로는 외장SSD 한 종류만 검증됐다.** 나머지 4개 분류는 규격에서 뽑은
  합성 시그니처로 테스트했다. 플로피나 ODD가 손에 있으면 `updev usb zone`으로 꽂아보고,
  판정이 틀리면 근거 표에 어느 시그니처가 잘못 걸렸는지 그대로 나온다.
- 갓 꽂은 디스크는 USB 열거가 끝난 뒤에야 SCSI/block 계층이 올라온다. 체험존은 매 폴링마다
  다시 읽어서 근거가 확보되는 대로 판정을 갱신한다. 역할 판정도 같다 — 드라이버 바인딩은
  열거 뒤에 일어나고, 바인딩은 디스크립터보다 강한 증거다.
- **NFC는 리더까지 실물로 검증됐고, 태그 교환은 아직이다.** MFRC522 v2.0을 40핀에 물려서
  확인한 것: SPI 트랜스포트, 레지스터 읽기/쓰기, 초기화 시퀀스(TxControl `0x83` — 안테나
  켜짐, ModeReg `0x3D`, TxASK `0x40`), 그리고 **칩의 하드웨어 CRC 코프로세서 결과가
  `crc_a()` 순수 구현과 세 벡터 모두 일치**. 필드에 태그가 없을 때 타이머 IRQ로 빠져나오는
  경로도 확인됐다.
  아직 실물로 안 밟아본 건 태그 교환 자체 — REQA · anticollision · SELECT · 인증 · 블록
  읽기/쓰기. 카드를 리더에 올리고 `updev nfc poll` 하면 그 경로가 처음 돌아간다.
  PN532는 세 트랜스포트 모두 아직 가짜 SPI로만 돌려봤다.
- **`disk bench`의 seek 값은 버스가 한가할 때 재야 의미가 있다.** 1-4ms 구간은 판정하지
  않고 `unclear`로 남기고, 캐시가 답한 게 뻔한 값(0.01ms 미만)은 `cached`로 표시해서
  매체에 대한 근거로 쓰지 않는다.
