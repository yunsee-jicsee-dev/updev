"""And then what? — the tool that fits the thing you just plugged in.

`usbrole.py` answers *what is it*, `usbclass.py` answers *which storage class*.
Both stop at the verdict, which is where the interesting question starts: now
that we know it is a USB floppy drive, what can this machine actually do with
it?

So this module is a lookup from a recognised device to the commands worth
running on it. The floppy toy (`updev floppy`) is the answer for exactly one
class — FUSB — and it earns its place there: a real UFI drive is the only
device on the bus that can take the 1.44 MB art image back as a physical disk.
Everything else gets its own answer instead of an apology: a thumb drive gets
a read benchmark, a keyboard gets an evdev tap, a camera gets camtoy, a jumper-
wired NFC module gets the reader.

Kept pure on purpose. `tools_for()` takes verdicts and /dev nodes and returns
data, so the whole table is testable without owning any of the hardware —
same trick `usbclass.classify()` plays.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .core.model import Device, Kind
from .usbclass import UsbClass, Verdict
from .usbrole import RoleVerdict, UsbRole

__all__ = [
    "Tool",
    "Recognition",
    "annotate",
    "recognize",
    "tools_for",
    "tools_for_device",
]


@dataclass(slots=True)
class Tool:
    """One thing you can run against this device.

    `needs` is a binary that has to be on PATH — filled in by `annotate()`
    rather than by the table, so the table stays a pure fact about the device
    and not about this particular machine's install.
    """

    name: str                  # short id: "floppy", "bench", "hid"
    title: str                 # what it does, in one Korean phrase
    command: str
    why: str                   # why this tool and this device belong together
    needs: str = ""            # binary that must exist on PATH
    lead: bool = False         # the headline tool for this device
    destructive: bool = False  # overwrites the medium — never run for the user
    missing: bool = False      # set by annotate(): `needs` is not installed

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "command": self.command,
            "why": self.why,
            "needs": self.needs,
            "lead": self.lead,
            "destructive": self.destructive,
            "available": not self.missing,
        }


@dataclass(slots=True)
class Recognition:
    """Everything the 체험존 knows about one device, in one object."""

    device: Device
    roles: RoleVerdict | None = None
    storage: Verdict | None = None
    nodes: dict[str, list[str]] = field(default_factory=dict)
    tools: list[Tool] = field(default_factory=list)

    @property
    def headline(self) -> str:
        """The one-line identity: storage class if we have one, else the role."""
        if self.storage and self.storage.usb_class != UsbClass.UNKNOWN:
            return self.storage.label
        if self.roles:
            return self.roles.label
        return "미확인"

    @property
    def badge(self) -> str:
        if self.storage and self.storage.usb_class != UsbClass.UNKNOWN:
            return str(self.storage.usb_class)
        return self.roles.short if self.roles else "unknown"

    @property
    def lead(self) -> Tool | None:
        return next((t for t in self.tools if t.lead), None)

    def as_dict(self) -> dict:
        out: dict = {
            "uid": self.device.uid,
            "address": self.device.address,
            "label": self.device.label,
            "badge": self.badge,
            "headline": self.headline,
            "nodes": self.nodes,
            "tools": [t.as_dict() for t in self.tools],
        }
        if self.roles:
            out["roles"] = self.roles.as_dict()
        if self.storage:
            out["storage"] = self.storage.as_dict()
        return out


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

def tools_for(
    address: str,
    roles: RoleVerdict | None = None,
    storage: Verdict | None = None,
    nodes: dict[str, list[str]] | None = None,
) -> list[Tool]:
    """Which tools fit this device. Pure — verdicts and node names in, data out."""
    nodes = nodes or {}
    found: list[Tool] = []
    seen: set[str] = set()

    def add(tool: Tool) -> None:
        # Deduped on the command too: a role often names the same command the
        # generic list would, and offering `usb descriptors` twice under two
        # titles reads as a bug.
        if tool.name in seen or tool.command in seen:
            return
        seen.add(tool.name)
        seen.add(tool.command)
        found.append(tool)

    role_list = list(roles.roles) if roles else []
    if storage and storage.usb_class != UsbClass.UNKNOWN and UsbRole.STORAGE not in role_list:
        role_list.insert(0, UsbRole.STORAGE)

    for role in role_list:
        if role == UsbRole.STORAGE:
            for tool in _storage_tools(address, storage, nodes):
                add(tool)
        else:
            for tool in _role_tools(role, address, nodes):
                add(tool)

    for tool in _always(address):
        add(tool)
    return found


def _storage_tools(
    address: str,
    storage: Verdict | None,
    nodes: dict[str, list[str]],
) -> list[Tool]:
    """The five storage classes, each with the tool that actually applies.

    This is where 꼬깔 stops being universal: `updev floppy` writes a 1.44 MB
    image, so it leads for FUSB and drops to a QEMU-only demo everywhere else.
    """
    node = _first(nodes.get("block"))
    cls = storage.usb_class if storage else UsbClass.UNKNOWN

    if cls == UsbClass.FUSB:
        out = [
            Tool("floppy-make", "아트 플로피 이미지 만들기",
                 "updev floppy make -o updev-art.img",
                 "A UFI drive is the one device on this bus that can hold the "
                 "1.44 MB image as a physical disk.",
                 lead=True),
            Tool("floppy-show", "쓰기 전에 아트만 보기", "updev floppy show",
                 "Prints the boot-sector art without touching a disk."),
        ]
        if node:
            out.append(Tool(
                "floppy-write", "실물 디스켓에 굽기",
                f"sudo dd if=updev-art.img of={node} bs=512 conv=fsync",
                f"{node} is this drive's medium. Writing wipes it, so updev "
                f"prints the command and lets you run it.",
                needs="dd", destructive=True,
            ))
            out.append(Tool(
                "floppy-info", "디스크를 되읽어 BPB 확인",
                f"sudo updev floppy info {node}",
                "Reads the BPB and root directory straight off the medium — "
                "the round trip that proves the image survived the drive.",
            ))
        out.append(Tool(
            "floppy-boot", "QEMU로 부팅시켜 보기",
            f"updev floppy boot --usb {address}" if address else "updev floppy boot",
            "Runs the 16-bit boot sector for real and screenshots what it drew.",
            needs="qemu-system-i386",
        ))
        return out

    if cls == UsbClass.ODD:
        out = [
            Tool("odd-toc", "디스크가 들었는지 · 세션 정보",
                 f"updev show {node}" if node else "updev scan -k storage",
                 "SCSI peripheral type 0x05 means the tray is the interesting "
                 "part: what is in it, and can it be read.",
                 lead=True),
        ]
        if node:
            out.append(Tool("bench", "읽기 속도 측정", f"updev disk bench {node}",
                            "Optical reads are slow by physics — measuring says "
                            "whether it is the disc or the link."))
        out.append(Tool("floppy-boot", "부팅 이미지를 QEMU로 확인",
                        "updev floppy boot", "No physical write path here, so "
                        "the art image only boots virtually.",
                        needs="qemu-system-i386"))
        return out

    # NUSB / HUSB / SUSB — real block devices, so measure them.
    out: list[Tool] = []
    if node:
        out.append(Tool("bench", "실제 읽기 속도 측정", f"updev disk bench {node}",
                        "The negotiated link speed is a ceiling, not a promise. "
                        "This reads the medium and reports what it really gives.",
                        lead=True))
        out.append(Tool("layout", "파티션·마운트·모델", f"updev show {node}",
                        "Partition table, filesystem and mount points as the "
                        "kernel sees them."))
    if cls == UsbClass.HUSB:
        out.append(Tool("smart", "SMART 건강 상태", f"sudo smartctl -a {node or '/dev/sdX'}",
                        "A spinning disk has reallocated sectors and hours "
                        "powered on; ask it for them.",
                        needs="smartctl"))
    if cls == UsbClass.SUSB:
        out.append(Tool("smart", "SMART 건강 상태", f"sudo smartctl -a {node or '/dev/sdX'}",
                        "Wear levelling and written-bytes counters live here.",
                        needs="smartctl"))
        out.append(Tool("uas", "UAS/BOT alt setting 확인",
                        f"updev usb descriptors {address}",
                        "An enclosure usually ships alt 0 = BOT and alt 1 = UAS. "
                        "sysfs only shows the active one; the descriptor shows both."))
    if cls == UsbClass.UNKNOWN:
        out.append(Tool("classify", "왜 아직 판정이 안 되는지 보기",
                        f"updev usb classify {address}",
                        "SCSI and block layers come up after USB enumeration, so "
                        "an early verdict is made on partial evidence."))
    return out


def _role_tools(role: UsbRole, address: str, nodes: dict[str, list[str]]) -> list[Tool]:
    """Non-storage roles. Each one gets the tool its own subsystem provides."""
    video = _first(nodes.get("video4linux"))
    tty = _first(nodes.get("tty"))
    net = _first(nodes.get("net"))
    inputs = nodes.get("input") or []

    if role == UsbRole.CAMERA:
        out = [Tool("camtoy", "카메라 장난감 14개로 놀기",
                    f"python3 -m camtoy live -d {video or '/dev/video0'}",
                    "A UVC node is a frame source, and camtoy is the frame "
                    "consumer that already lives in this repo.",
                    lead=True)]
        out.append(Tool("cam-list", "카메라·ISP 노드 정리해서 보기", "updev cam list",
                        "Separates real capture nodes from the ISP and codec "
                        "nodes V4L2 also exposes."))
        if video:
            out.append(Tool("cam-modes", "센서가 내주는 포맷·해상도",
                            f"v4l2-ctl --device={video} --list-formats-ext",
                            "What the sensor will actually give you, before you "
                            "ask for a mode it cannot do.",
                            needs="v4l2-ctl"))
        out.append(Tool("iso", "isochronous 엔드포인트 확인",
                        f"updev usb descriptors {address}",
                        "Only real streaming hardware can reserve isochronous "
                        "bandwidth — the descriptor proves the camera is one."))
        return out

    if role in (UsbRole.KEYBOARD, UsbRole.MOUSE, UsbRole.GAMEPAD,
                UsbRole.TOUCHSCREEN, UsbRole.HID):
        # Addressed by USB address, not by event node: a composite receiver is
        # several event nodes at once, and `hid watch` resolves all of them.
        target = address or _first(inputs)
        return [
            Tool("hid", "누르는 키·움직임 실시간으로 보기",
                 f"updev hid watch {target}",
                 "The kernel already decoded this into evdev events; this taps "
                 "them and names each code.",
                 lead=True),
            Tool("hid-report", "HID 리포트 디스크립터 크기",
                 f"updev usb descriptors {address}",
                 "Report descriptor length and the boot protocol it claims."),
        ]

    if role == UsbRole.SERIAL:
        port = (tty or "").removeprefix("/dev/") or "ttyUSB0"
        return [
            Tool("monitor", "시리얼 포트 열어서 흘려보기",
                 f"updev serial monitor {port} -b 115200",
                 "A tty binding means bytes are already flowing; this reads "
                 "them without a terminal emulator.",
                 lead=True),
            Tool("ports", "붙은 시리얼 포트 전부", "updev serial ports",
                 "USB-serial bridges and the onboard UART side by side."),
        ]

    if role in (UsbRole.WIFI, UsbRole.ETHERNET):
        iface = net or "wlan0"
        return [
            Tool("net-scan", "이 인터페이스로 서브넷 훑기",
                 f"updev net scan --iface {iface}",
                 "The adapter is only interesting once you ask what it can "
                 "reach — 254 addresses in about a second, no root.",
                 lead=True),
            Tool("iface", "링크·주소·신호 세기", f"updev show {iface}",
                 "Link state, addresses and (for Wi-Fi) signal quality."),
        ]

    if role == UsbRole.AUDIO:
        return [
            Tool("audio-iso", "오디오 스트리밍 엔드포인트 확인",
                 f"updev usb descriptors {address}",
                 "USB Audio Class devices reserve isochronous bandwidth per "
                 "microframe; the endpoint map shows the budget.",
                 lead=True),
            Tool("audio-play", "실제로 소리 내보기",
                 "speaker-test -c 2 -t wav -l 1",
                 "The one check a descriptor cannot do for you.",
                 needs="speaker-test"),
        ]

    if role == UsbRole.PHONE:
        return [
            Tool("adb", "ADB로 붙었는지 확인", "adb devices -l",
                 "The 0xFF/0x42/0x01 interface is ADB; this asks whether the "
                 "daemon on the other end agrees.",
                 needs="adb", lead=True),
            Tool("phone-iface", "ADB / PTP / Apple 인터페이스 보기",
                 f"updev usb descriptors {address}",
                 "Phones expose different function sets per USB mode — the "
                 "descriptor says which mode it is in right now."),
        ]

    if role == UsbRole.HUB:
        return [
            Tool("hub-path", "이 허브 아래 속도·병목",
                 f"updev usb path {address}",
                 "A hub caps everything below it, so the hub is where a slow "
                 "device usually gets its speed from.",
                 lead=True),
            Tool("tree", "물리적 연결 구조 전체", "updev tree",
                 "Everything hanging off this hub, in place."),
        ]

    if role == UsbRole.SMARTCARD:
        return [
            Tool("nfc-usb", "PC/SC 리더로 카드 읽기", "pcsc_scan",
                 "A CCID reader talks through pcscd, not through GPIO.",
                 needs="pcsc_scan", lead=True),
            Tool("nfc-wiring", "점퍼선 NFC 모듈 배선표", "updev nfc wiring",
                 "If you meant the RC522/PN532 module on the 40-pin header, "
                 "that path is `updev nfc` instead."),
        ]

    if role == UsbRole.BLUETOOTH:
        return [
            Tool("bt-scan", "주변 블루투스 장치 훑기", "updev bt scan",
                 "A Bluetooth radio's job starts at discovery.",
                 lead=True),
        ]

    if role == UsbRole.PRINTER:
        return [Tool("printer", "프린터 큐 상태", "lpstat -p -d",
                     "Class 0x07 is a printer; CUPS owns it from here.",
                     needs="lpstat", lead=True)]

    if role == UsbRole.SCANNER:
        return [Tool("scanner", "스캐너 인식 확인", "scanimage -L",
                     "SANE enumerates scanners the kernel does not bind.",
                     needs="scanimage", lead=True)]

    if role == UsbRole.DISPLAY:
        return [Tool("display", "붙은 디스플레이·EDID", "updev scan -k display",
                     "DisplayLink encodes video over USB, but the panel itself "
                     "shows up as a DRM connector with an EDID.",
                     lead=True)]

    if role == UsbRole.BILLBOARD:
        return [Tool("billboard", "왜 alt mode가 실패했는지",
                     f"updev usb descriptors {address}",
                     "Class 0x11 exists only to report a failed alternate-mode "
                     "negotiation. The descriptor carries the reason.",
                     lead=True)]

    return []


def _always(address: str) -> list[Tool]:
    """Three things are worth offering for any USB device at all."""
    return [
        Tool("gui", "이 장치의 에디터 GUI 열기", f"updev gui {address}",
             "Whatever this turns out to be, there is a panel for its insides "
             "— and the info panel when there isn't."),
        Tool("path", "루트허브부터의 물리 경로 · 병목", f"updev usb path {address}",
             "Which port it took, what each hop negotiated, and which hop is "
             "the one holding it back."),
        Tool("descriptors", "원시 디스크립터 (엔드포인트·alt·IAD)",
             f"updev usb descriptors {address}",
             "sysfs summarises the device; the descriptor is the device."),
    ]


# --------------------------------------------------------------------------
# non-USB devices
# --------------------------------------------------------------------------

def tools_for_device(dev: Device) -> list[Tool]:
    """Tools for anything that isn't on the USB bus.

    Backends already hang `Action`s off their devices — those *are* the tools,
    so this reuses them rather than keeping a second, drifting table. NFC gets
    a nudge on the buses it can be wired to, because a module on jumper wires
    has no descriptor to announce itself with.
    """
    tools = [
        Tool(action.name, action.description, action.command,
             "Offered by the backend that found this device.")
        for action in dev.actions
    ]
    if tools:
        tools[0].lead = True

    if dev.kind == Kind.NFC:
        # The reader is a means; the card is the thing you came for.
        tools.insert(0, Tool(
            "editor", "태그 에디터 — 덤프·접근조건·블록 편집",
            f"updev gui {dev.uid}",
            "Reads the whole card, decodes each sector's access bits, and "
            "writes back only the blocks you changed.",
            lead=True,
        ))
        for tool in tools[1:]:
            tool.lead = False
    else:
        tools.append(Tool(
            "gui", "이 장치의 에디터 GUI 열기", f"updev gui {dev.uid}",
            "Registers, pins, sectors — whichever of those this device has.",
        ))
    if dev.kind in (Kind.SPI, Kind.I2C) and "nfc" not in {t.name for t in tools}:
        tools.append(Tool(
            "nfc", "NFC 리더 모듈이면 여기서 태그 읽기", "updev nfc detect",
            "RC522 and PN532 modules hang off exactly these pins and announce "
            "nothing — you have to ask them.",
        ))
    return tools


# --------------------------------------------------------------------------
# hardware-facing helpers
# --------------------------------------------------------------------------

def recognize(dev: Device, storage: Verdict | None = None) -> Recognition:
    """Read the sysfs facts for one USB device and pick its tools.

    The only function here that touches the filesystem, so tests drive
    `tools_for()` directly.
    """
    from .usbclass import classify, gather_facts
    from .usbrole import device_nodes, gather_device_facts, identify

    if dev.kind != Kind.USB:
        return Recognition(device=dev, tools=tools_for_device(dev))

    roles = identify(gather_device_facts(dev.address))
    nodes = device_nodes(dev.address)
    if storage is None and (UsbRole.STORAGE in roles.roles or "storage" in dev.tags):
        storage = classify(gather_facts(usb_address=dev.address))
    tools = tools_for(dev.address, roles, storage, nodes)
    return Recognition(device=dev, roles=roles, storage=storage, nodes=nodes, tools=tools)


def annotate(tools: list[Tool]) -> list[Tool]:
    """Flag the tools whose binary this machine doesn't have."""
    from .core.util import have

    for tool in tools:
        if tool.needs and not have(tool.needs):
            tool.missing = True
    return tools


def _first(values: list[str] | None) -> str:
    return values[0] if values else ""
