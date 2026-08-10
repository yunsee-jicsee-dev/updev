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
    ("updev usb zone", "USB 꽂으면 뭔지 판정해주는 체험존"),
    ("updev tree", "물리적 연결 구조"),
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
