# updev

라즈베리파이(그리고 웬만한 리눅스 보드) 하나에 물린 **모든 것**을 한 곳에서 보는 장치관리자.

LAN · USB · I2C · SPI · Serial/UART · 카메라 · GPIO · 스토리지 · 블루투스, 그리고 보드 자신까지.
`lsusb`, `i2cdetect`, `lsblk`, `ip`, `vcgencmd`, `pinctrl`, `v4l2-ctl`을 따로 치고 머릿속에서
합치던 걸 하나의 모델로 묶었다.

```bash
bin/updev            # 전체 스캔
bin/updev watch      # 라이브 대시보드
bin/updev doctor     # 뭐가 잘못됐는지 + 고치는 명령어까지
```

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
| `updev usb zone` | USB 체험존 — 꽂으면 분류해준다 |
| `updev usb path <주소>` | 물리 경로 + 병목 지점 |
| `updev floppy make` | 부팅 대신 아트가 뜨는 플로피 이미지 |
| `updev floppy boot` | QEMU로 띄우고 화면 캡처 |
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
updev usb zone          # 꽂으면 바로 판정
updev usb classify      # 지금 붙어있는 것 판정
updev usb path 1-2.4    # 물리 경로 + 병목
updev usb signatures    # 규칙표 보기
```

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
├── usbdesc.py        원시 USB 디스크립터 파서 (/dev/bus/usb)
├── vm.py             QEMU 런처 + USB 패스스루 안전검사
├── floppy.py         FAT12 + 16비트 부트섹터 조립기
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
│   └── bluetooth.py  컨트롤러 + 페어링 목록
└── ui/
    ├── render.py     테이블·트리·상세·doctor 리포트
    ├── dash.py       라이브 대시보드
    └── zone.py       USB 체험존 + activity 로그
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

하드웨어 없이 검증 가능한 부분을 **121개 테스트**로 덮는다:

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
  다시 읽어서 근거가 확보되는 대로 판정을 갱신한다.
