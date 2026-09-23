"""Content for `updev help`.

Click's own `--help` lists flags; this covers the things flags can't explain —
what the classifications mean, which signature drives which verdict, why a
USB-C monitor shows up under `display` and not `usb`, and what is safe to run.

Topics are plain data so `updev --json help` can hand the whole thing to
something else.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Topic:
    name: str
    title: str
    blurb: str
    body: str = ""
    commands: list[tuple[str, str]] = field(default_factory=list)
    table: tuple[tuple[str, ...], list[tuple[str, ...]]] | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "blurb": self.blurb,
            "body": self.body,
            "commands": [{"command": c, "description": d} for c, d in self.commands],
        }


QUICKSTART: list[tuple[str, str]] = [
    ("updev", "전체 스캔 — 뭐가 붙어있나"),
    ("updev doctor", "문제 진단 + 고치는 명령까지"),
    ("updev watch", "라이브 대시보드"),
    ("updev activity", "착탈 이벤트 실시간 로그"),
    ("updev usb zone", "USB 꽂으면 뭔지 판정하고 맞는 툴까지 주는 체험존"),
    ("updev tools", "지금 붙어있는 걸로 뭘 할 수 있는지"),
    ("updev nfc wiring", "점퍼선 NFC 리더 배선표"),
    ("updev gui", "기기별 에디터 GUI"),
    ("updev tree", "물리적 연결 구조"),
    ("updev panel boot", "ST7735S에 부팅 상태 표시"),
]


TOPICS: list[Topic] = [
    Topic(
        name="overview",
        title="전체 구조",
        blurb="updev가 뭘 보고 어떻게 묶는지",
        body=(
            "updev는 보드에 붙은 모든 것을 하나의 모델(Device)로 통일한다. "
            "lsusb·i2cdetect·lsblk·ip·vcgencmd·pinctrl·v4l2-ctl를 따로 치고 "
            "머릿속에서 합치던 걸 대신 해준다.\n\n"
            "백엔드 12개가 각자 자기 영역을 훑고, 스레드 풀에서 개별 타임아웃을 "
            "갖고 병렬로 돈다. 하나가 죽어도 스캔 전체는 죽지 않는다 — "
            "`updev backends`로 누가 돌았고 누가 실패했는지 볼 수 있다.\n\n"
            "모든 명령에 --json이 붙는다."
        ),
        commands=[
            ("updev scan", "종류별로 묶어서 전부 나열"),
            ("updev scan -k usb -k i2c", "종류로 필터"),
            ("updev scan -t SUSB", "태그로 필터"),
            ("updev scan --deep", "느리지만 철저하게 (버스 스캔 + LAN 스윕)"),
            ("updev show <아무거나>", "uid·이름·주소·/dev 경로 아무거나로 조회"),
            ("updev tree", "물리적 토폴로지"),
            ("updev backends", "백엔드별 상태와 소요시간"),
            ("updev export out.json", "전체 인벤토리 파일로"),
        ],
    ),
    Topic(
        name="tools",
        title="판정 다음에 오는 것",
        blurb="뭔지 알아냈으면, 그걸로 뭘 하나",
        body=(
            "updev는 장치가 뭔지 판정하고 거기서 멈추지 않는다. 판정 결과마다 "
            "실제로 실행할 수 있는 명령이 붙는다. `updev usb zone`은 꽂는 순간 "
            "이 둘을 같이 보여주고, `updev tools`는 같은 걸 화면 안 잡고 "
            "출력한다.\n\n"
            "플로피 툴(`updev floppy`)은 1.44MB 이미지를 쓰는 물건이라 FUSB "
            "에서만 대표 툴이 된다. 나머지는 각자 맞는 게 따로 붙는다:\n\n"
            "  NUSB·HUSB·SUSB  실제 읽기 속도 + 랜덤 접근 지연 측정\n"
            "  ODD             디스크 상태\n"
            "  카메라           camtoy · v4l2 포맷 목록\n"
            "  키보드·마우스     evdev 이벤트 실시간\n"
            "  시리얼           포트 열어서 흘려보기\n"
            "  무선랜·유선랜     그 인터페이스로 서브넷 스윕\n"
            "  휴대폰           ADB 연결 확인\n"
            "  허브             아래쪽 속도·병목\n"
            "  I2C·SPI 칩       그 백엔드가 붙여둔 명령 + NFC 리더 확인\n\n"
            "★ 표시가 그 장치의 대표 툴이다. 설치가 필요한 명령은 목록에 "
            "남되 뭘 깔아야 하는지 같이 나온다. 매체를 덮어쓰는 명령은 "
            "'덮어씀'으로 표시되고 updev가 대신 실행하지 않는다."
        ),
        commands=[
            ("updev usb zone", "꽂으면 판정 + 툴 (라이브)"),
            ("updev tools", "붙어있는 것 전부"),
            ("updev tools 3-2", "하나만"),
            ("updev tools --brief", "한 줄씩"),
            ("updev --json tools", "그대로 파이프로"),
        ],
    ),
    Topic(
        name="gui",
        title="기기별 에디터 GUI",
        blurb="장치를 고르면 그 장치의 속을 여는 창",
        body=(
            "`updev gui` 는 왼쪽에 `updev scan` 과 같은 목록, 오른쪽에 그 장치의 "
            "에디터를 띄운다. 장치를 앞에 써도 된다 — `updev 3-2 gui`.\n\n"
            "  USB 플로피 (FUSB)   FAT12 에디터 — 파일·부트섹터 고쳐서 다시 굽기\n"
            "  NFC 리더            태그 에디터 — 전체 덤프·접근조건·NDEF·블록 쓰기\n"
            "  블록 장치            섹터 뷰어 (읽기 전용)\n"
            "  I2C 칩              레지스터 에디터\n"
            "  GPIO                핀 에디터 (출력은 잠금 해제 후)\n"
            "  키보드·마우스        evdev 이벤트 실시간\n"
            "  카메라               포맷 목록 + 한 장 찍기\n"
            "  USB 시리얼           터미널 (보기 + 한 줄 보내기)\n"
            "  무선랜·유선랜         링크 정보 + 서브넷 훑기\n"
            "  허브                 포트별로 뭐가 걸렸고 뭘 협상했는지\n"
            "  그 외 전부           장치 정보 + 실행할 수 있는 명령\n\n"
            "USB 장치에는 역할별 패널 뒤에 항상 세 개가 더 붙는다 — 정체(판정 "
            "근거), 디스크립터(장치가 말하는 그대로), 경로(병목). 자리를 못 잡는 "
            "장치야말로 디스크립터를 읽어봐야 하는 장치라서 조건부가 아니다.\n\n"
            "한 장치에 패널이 여러 개면 탭으로 다 준다. USB 플로피는 FAT12 "
            "디스크이면서 블록 장치라, 둘 다 열린다.\n\n"
            "쓰기 규칙은 터미널과 같다. CLI가 --yes를 요구하는 자리에서 창은 "
            "나가는 바이트를 전부 보여주는 확인 창을 띄우고, 기본값은 항상 "
            "'아니오'다. 마운트된 디스크의 섹터 쓰기처럼 고칠 방법이 없는 건 "
            "아예 만들지 않았다.\n\n"
            "tkinter만 쓴다 — 파이 OS에 이미 있고, 따로 깔 게 없다. 디스플레이가 "
            "없으면 이유를 말하고 `updev tools`를 안내한다."
        ),
        commands=[
            ("updev gui", "전부 훑어보기"),
            ("updev gui 3-2", "이 장치로 바로"),
            ("updev 3-2 gui", "장치를 앞에 써도 된다"),
            ("updev tools", "같은 걸 터미널에서"),
        ],
    ),
    Topic(
        name="usb",
        title="USB",
        blurb="장치 역할 인식, 저장장치 분류, 경로 추적",
        body=(
            "USB 장치는 자기가 뭔지 직접 말해주지 않는다. 그래서 두 가지를 본다.\n\n"
            "1) 커널이 실제로 무엇을 붙였는가 — 인터페이스 밑에 net/이 생기고 "
            "phy80211이 있으면 그건 해석의 여지 없이 Wi-Fi 어댑터다. "
            "video4linux/는 카메라, sound/는 오디오, tty/는 시리얼.\n\n"
            "2) 인터페이스 디스크립터 — 장치가 스스로 주장하는 것. "
            "0x0E는 UVC 카메라, 0x03/0x01/0x01은 부트 프로토콜 키보드, "
            "0xFF/0x42/0x01은 안드로이드 ADB, 0x11은 USB-C Billboard.\n\n"
            "복합 장치는 역할이 여러 개인 게 사실이라 전부 보여준다 — "
            "무선 리시버는 정말로 키보드이면서 마우스다."
        ),
        commands=[
            ("updev usb path <주소>", "루트허브→허브→장치 경로 + 병목 지점"),
            ("updev usb classify", "저장장치 분류 + 근거 전체"),
            ("updev usb zone", "체험존 — 꽂으면 즉시 판정"),
            ("updev usb signatures", "분류 규칙표"),
            ("updev scan -k usb", "USB 장치 목록"),
        ],
        table=(
            ("역할", "무엇으로 알아내나"),
            [
                ("키보드 / 마우스", "HID 부트 프로토콜 0x01 / 0x02"),
                ("카메라", "video4linux 바인딩, 또는 클래스 0x0E (UVC)"),
                ("오디오 / AUX 어댑터", "sound 바인딩, 또는 클래스 0x01"),
                ("무선랜", "net 바인딩 + phy80211 존재"),
                ("유선랜", "net 바인딩 (phy80211 없음)"),
                ("휴대폰", "ADB(0xFF/0x42/0x01), PTP(0x06), Apple(0xFF/0xFE/0x02)"),
                ("저장장치", "클래스 0x08 → 다시 NUSB/FUSB/HUSB/SUSB/ODD로 세분"),
                ("디스플레이 어댑터", "DisplayLink 등 벤더 ID"),
                ("Billboard", "클래스 0x11 — USB-C alt mode 협상 실패 알림"),
            ],
        ),
    ),
    Topic(
        name="storage",
        title="USB 저장장치 분류",
        blurb="NUSB · FUSB · HUSB · SUSB · ODD",
        body=(
            "썸드라이브와 외장SSD는 둘 다 USB 브릿지 뒤의 플래시라 겉보기로 "
            "똑같다. 그래서 규격이 강제하는 시그니처를 읽어 가중치를 매긴다.\n\n"
            "규격이 명확히 못박은 규칙(decisive)이 하나라도 걸리면 즉시 확정하고 "
            "나머지는 보지 않는다. 나머지는 점수를 합산해 최고점을 고르고, "
            "2위와의 격차로 신뢰도를 매긴다.\n\n"
            "판정은 항상 근거를 달고 나온다. 틀려도 어느 시그니처가 잘못 걸렸는지 "
            "따질 수 있게 하는 게 요점이다."
        ),
        commands=[
            ("updev usb classify", "지금 붙은 것 전부 판정"),
            ("updev usb classify sda", "블록 장치명으로도 지정 가능"),
            ("updev usb signatures", "규칙표 + 가중치"),
        ],
        table=(
            ("분류", "뜻", "결정적 시그니처"),
            [
                ("NUSB", "단순 USB 플래시", "RMB=1 + VPD 0xB1 미구현 + Bulk-Only"),
                ("FUSB", "플로피", "bInterfaceSubClass 0x04 = UFI"),
                ("HUSB", "외장 HDD", "VPD 0xB1 회전수 ≥ 0x0401 (실제 RPM)"),
                ("SUSB", "외장 SSD", "VPD 0xB1 회전수 = 0x0001 (비회전)"),
                ("ODD", "광학 드라이브", "SCSI peripheral type 0x05"),
            ],
        ),
    ),
    Topic(
        name="display",
        title="디스플레이 / 휴대용 모니터",
        blurb="USB-C 모니터가 USB가 아닌 이유",
        body=(
            "휴대용 USB-C 모니터를 찾는다면 `updev scan -k display`를 봐야 한다. "
            "usb 쪽이 아니다. 이유:\n\n"
            "· DisplayPort alt mode — 모니터가 USB 장치로 아예 안 나타난다. "
            "커넥터가 DP 레인을 직접 나른다. 그리고 라즈베리파이 5의 USB-C 포트는 "
            "전원 전용이라 alt mode가 없다. 그래서 파이에선 이 경우가 안 생긴다.\n\n"
            "· HDMI — 파이 5에 붙은 휴대용 모니터가 실제로 쓰는 방식이다. "
            "케이블 끝이 USB-C든 아니든 상관없다. DRM 커넥터로 잡힌다.\n\n"
            "· DisplayLink — 영상을 USB 3으로 인코딩해 보낸다. 이건 진짜 USB "
            "장치라서 usb 쪽에도 잡히고, 벤더 ID로 식별한다.\n\n"
            "alt mode를 원했는데 못 받은 장치는 USB Billboard(클래스 0x11)로 "
            "자기를 알린다. updev가 이걸 잡아서 왜 안 되는지 설명한다.\n\n"
            "연결된 모니터는 EDID를 파싱해 제조사·모델·제조연도·물리 크기·"
            "대각선 인치까지 보여준다."
        ),
        commands=[
            ("updev scan -k display", "모든 DRM 커넥터"),
            ("updev show display:HDMI-A-1", "EDID 전체 + 지원 모드"),
        ],
    ),
    Topic(
        name="buses",
        title="I2C · SPI · GPIO · 시리얼",
        blurb="40핀 헤더에 물린 것들",
        body=(
            "I2C 주소 스캔은 i2cdetect와 같은 전략을 쓰되, 결과가 물리적으로 "
            "가능한지 한 번 더 검증한다. 파이 5의 내부 HDMI DDC 버스는 아무것도 "
            "없는 주소에도 ACK를 돌려주기 때문에, i2cdetect는 존재하지 않는 장치 "
            "117개를 보고한다. updev는 이걸 감지해서 읽기 모드로 재검사한다.\n\n"
            "SPI는 탐지 프로토콜이 없다. 버스에 뭐가 있는지 물어볼 방법이 아예 "
            "없으므로, 설정을 정확히 보고하고 루프백 테스트를 제공한다.\n\n"
            "I2C나 SPI가 꺼져 있으면 장치가 안 보이는 게 아니라 왜 안 보이는지를 "
            "보고한다."
        ),
        commands=[
            ("updev i2c scan", "모든 버스 주소 스캔 (--deep 포함됨)"),
            ("updev i2c read 1 0x76 0xd0", "레지스터 읽기"),
            ("updev i2c dump 1 0x50", "레지스터 공간 헥스 덤프"),
            ("updev spi test 0.0", "MOSI-MISO 점퍼 물리고 루프백"),
            ("updev gpio pins", "40핀 헤더를 실물 배치대로"),
            ("updev serial ports", "USB-시리얼 + 온보드 UART"),
            ("updev serial monitor ttyUSB0 -b 115200", "수신 전용 모니터"),
        ],
    ),
    Topic(
        name="nfc",
        title="NFC 리더 (점퍼선)",
        blurb="RC522 · PN532를 40핀에 물려서 태그 읽기",
        body=(
            "USB NFC 리더는 자기가 뭔지 밝히지만, 점퍼선으로 무는 모듈은 "
            "아무것도 밝히지 않는다. SPI에는 열거라는 게 없고 I2C에는 고정 "
            "주소 하나뿐이다. 그래서 순서가 반대다 — 먼저 배선표를 보고, "
            "물리고, 그 다음에 물어본다.\n\n"
            "MFRC522는 SPI 전용이고 RST 선이 필수다. PN532는 DIP 스위치로 "
            "I2C·SPI·HSU 중 하나가 되는데, 스위치와 실제 배선이 다르면 "
            "조용히 아무 대답도 안 한다. PN532의 SPI는 LSB first라 파이 "
            "컨트롤러로는 못 맞추고, updev가 바이트를 뒤집어서 보낸다.\n\n"
            "CLI는 읽기만 한다 — UID·ATQA·SAK와 섹터 덤프. 쓰기는 GUI 태그 "
            "에디터에만 있고(`updev gui`), 거기서도 block 0과 섹터 트레일러는 "
            "막혀 있다. 트레일러의 접근 비트를 잘못 쓰면 그 섹터는 영구히 "
            "잠기기 때문이다.\n\n"
            "에디터는 섹터마다 접근 비트(C1/C2/C3)를 풀어서 어느 블록을 어느 "
            "키로 읽고 쓸 수 있는지 보여주고, NDEF 레코드를 해독하고, 덤프를 "
            ".mfd 파일로 저장하거나 연다.\n\n"
            "SPI는 주소가 없어서 CE0에 뭐가 물려있든 그 바이트를 본다. "
            "그래서 일반 스캔은 SPI를 건드리지 않고, --deep이나 "
            "`updev nfc detect`로 명시했을 때만 물어본다."
        ),
        commands=[
            ("updev nfc wiring", "모듈별 배선표 — 어느 핀에 뭘"),
            ("updev nfc wiring rc522", "하나만"),
            ("updev nfc detect -v", "붙었는지 확인 (조용한 버스까지)"),
            ("updev nfc read", "태그 하나 읽기"),
            ("updev nfc poll", "올릴 때마다 한 줄 — NFC판 체험존"),
            ("updev nfc dump --sector 1", "MIFARE Classic 섹터 덤프"),
            ("updev gui", "태그 에디터 — 전체 덤프·접근조건·NDEF·편집"),
        ],
    ),
    Topic(
        name="network",
        title="네트워크",
        blurb="인터페이스 + LAN 스윕",
        body=(
            "LAN 스윕은 권한 상승 없이 동작한다 — ping 바이너리로 병렬 스윕한 뒤 "
            "커널의 ARP 테이블에서 MAC을 읽는다. scapy도 root도 필요 없다. "
            "/24 기준 약 1.2초.\n\n"
            "MAC의 OUI로 제조사를 찾고, 로컬 관리 주소(랜덤화된 MAC)는 그렇다고 "
            "표시한다. --ports를 켜면 흔한 포트에 TCP connect를 시도한다.\n\n"
            "능동적으로 패킷을 보내는 동작이다. 본인이 책임지는 네트워크에서만 "
            "쓸 것."
        ),
        commands=[
            ("updev net scan", "기본 인터페이스의 서브넷 스윕"),
            ("updev net scan --cidr 192.168.0.0/24", "대상 지정"),
            ("updev net scan --no-ports", "포트 확인 생략"),
            ("updev scan -k net-iface", "인터페이스만"),
        ],
    ),
    Topic(
        name="floppy",
        title="플로피 아스키 아트 이미지",
        blurb="부팅 대신 아트가 뜨는 1.44MB 이미지",
        body=(
            "1.44MB FAT12 이미지를 손으로 조립한다. 두 겹이다.\n\n"
            "부트섹터는 진짜 16비트 x86이다. BIOS INT 10h로 아스키 아트를 찍고 "
            "halt한다. 부팅시키면 OS 대신 아트가 나온다.\n\n"
            "파일시스템은 같은 아트를 텍스트 파일로 담고 있어서, 마운트해도 "
            "똑같이 보인다.\n\n"
            "USB 플로피 드라이브가 있으면 이 이미지를 디스크에 쓰고 "
            "`updev usb classify`를 돌려보면 FUSB로 확정 판정이 나온다."
        ),
        commands=[
            ("updev floppy show", "터미널에 아트만 출력 (파일 안 만듦)"),
            ("updev floppy show pi5", "특정 조각만"),
            ("updev floppy make -o disk.img", "이미지 생성"),
            ("updev floppy info disk.img", "BPB + 루트 디렉터리 읽어보기"),
            ("qemu-system-i386 -fda disk.img -boot a", "실제로 부팅해보기"),
        ],
    ),
    Topic(
        name="safety",
        title="안전",
        blurb="뭐가 읽기 전용이고 뭐가 아닌지",
        body=(
            "기본은 읽기 전용이다. 버스에 쓰거나 송신하는 동작은 전부 별도 "
            "서브커맨드로 빼고, 잘못하면 하드웨어가 상할 수 있는 건 --yes를 "
            "명시적으로 요구한다.\n\n"
            "I2C 주소 스캔은 EEPROM 구간(0x30-0x37, 0x50-0x5F)에서 쓰기 대신 "
            "읽기로 탐지한다. HAT EEPROM에 실수로 쓰는 사고를 막기 위해서다.\n\n"
            "root 권한은 필요 없다. I2C 주소 스캔에는 i2c 그룹, SPI에는 spi 그룹 "
            "멤버십이 필요하고, 없으면 그 사실을 알려준다.\n\n"
            "능동적으로 뭔가 하는 명령: net scan(패킷 전송), bt scan(무선 탐색), "
            "spi test/xfer(버스 구동), i2c write(칩에 쓰기), cam capture(촬영)."
        ),
        commands=[
            ("updev i2c write ... --yes", "쓰기는 --yes 필수"),
            ("updev spi xfer ... --yes", "송신은 --yes 필수"),
        ],
    ),
    Topic(
        name="panel",
        title="ST7735S 프론트 패널",
        blurb="스캔 결과를 물리 디스플레이에 띄우기",
        body=(
            "128x160 ST7735S를 updev의 세 번째 프론트엔드로 쓴다. CLI는 터미널에, "
            "`updev gui`는 Tk에, `updev panel`은 유리 위에 같은 스캔을 그린다.\n\n"
            "`updev panel boot`는 화면 세 개를 순서대로 보여준다. 스플래시(스캔 "
            "전에 바로 뜬다), 백엔드별 진행 상황(리포트가 도착할 때마다 한 줄씩 "
            "채워진다), 그리고 요약. 진행 화면이 핵심이다 — 버스 하나가 멎으면 "
            "끝까지 채워지지 않는 줄이 범인을 지목한다.\n\n"
            "요약 화면은 프로세스가 끝나도 그대로 남는다. 부팅 때 한 번 돌려두면 "
            "아무것도 실행하지 않은 채로 상태판이 된다 — "
            "`./install.sh --panel-service` 가 systemd 유닛을 만들어 등록한다. "
            "updev 가 어디 설치돼 있는지(체크아웃이냐 --user 휠이냐)에 따라 "
            "실행 경로가 달라지므로 유닛은 설치 시점에 생성된다.\n\n"
            "배선: VCC→3V3, GND→GND, SCL→GPIO11, SDA→GPIO10, RES→GPIO25, "
            "DC→GPIO24, CS→GPIO8(CE0), BLK→3V3.\n\n"
            "주의: 패널은 SPI0 CE0에 물려 있고 그 SPI0는 updev가 보고하는 버스이기도 "
            "하다. 패널이 그리는 동안 SPI0는 패널 자신 때문에 바쁘다. 실제로 "
            "관찰하고 싶은 장치는 CE1이나 SPI1로 분리해야 한다."
        ),
        commands=[
            ("updev panel test", "컬러바 + 모서리 마크 — 배선/BGR 확인"),
            ("updev panel boot", "스플래시 → 진행 → 요약"),
            ("updev panel boot --capture ~/frames", "PNG로도 저장 (패널 없이 확인)"),
            ("updev panel boot --bgr", "빨강/파랑이 바뀌어 보이면"),
            ("updev panel off", "화면 지우고 놓아주기"),
            ("./install.sh --panel-service", "부팅 시 자동 실행 등록"),
        ],
    ),
    Topic(
        name="json",
        title="JSON / 자동화",
        blurb="파이프로 넘기기",
        body=(
            "모든 명령에 --json이 붙는다. activity는 이벤트당 JSON 한 줄을 "
            "흘려보내므로 그대로 파이프에 물릴 수 있다.\n\n"
            "doctor는 --fail-on으로 종료 코드를 제어한다. 기본은 error 이상일 때 "
            "1을 반환하므로 CI나 헬스체크에 그대로 쓸 수 있다."
        ),
        commands=[
            ("updev --json scan | jq '.devices[] | select(.status==\"degraded\")'",
             "상태로 거르기"),
            ("updev --json doctor | jq '.counts'", "문제 개수만"),
            ("updev --json usb classify | jq '.devices[].class'", "분류 결과만"),
            ("updev --json activity | jq -r '.kind + \" \" + .label'", "이벤트 스트림"),
            ("updev doctor --fail-on warn", "경고만 있어도 종료코드 1"),
        ],
    ),
]


def topic_names() -> list[str]:
    return [t.name for t in TOPICS]


def find_topic(name: str) -> Topic | None:
    want = name.strip().lower()
    exact = next((t for t in TOPICS if t.name == want), None)
    if exact:
        return exact
    return next((t for t in TOPICS if t.name.startswith(want)), None)
