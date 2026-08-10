"""What *kind* of thing a USB device is: keyboard, camera, Wi-Fi, phone, …

`usbclass.py` answers "which sort of storage is this". This module answers the
broader question first: is it storage at all, or a webcam, a mouse, a Wi-Fi
dongle, a phone, a USB-C audio dongle, a display adapter?

Two evidence sources, in this order of trust:

  1. **What the kernel actually bound.** An interface with a `net/` child that
     has `phy80211` *is* a Wi-Fi adapter — there is no interpretation left to
     do. Likewise `video4linux/` means frames, `sound/` means audio, `tty/`
     means a serial port. This beats every descriptor because it reflects a
     driver that successfully claimed the hardware.

  2. **The interface descriptor**, which the device asserts about itself.
     Class 0x0E is UVC video; 0x03/0x01/0x01 is a boot-protocol keyboard;
     0xFF/0x42/0x01 is Android's ADB; 0x11 is a USB-C Billboard, which is how
     a device tells you it wanted an alternate mode the host wouldn't give it.

A composite device honestly has several roles, so we return all of them —
a wireless receiver really is a keyboard *and* a mouse, and saying so is more
useful than picking one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .core.util import read_hex, read_text

__all__ = [
    "DeviceFacts",
    "InterfaceFacts",
    "RoleVerdict",
    "UsbRole",
    "describe_class",
    "gather_device_facts",
    "identify",
]

USB_ROOT = Path("/sys/bus/usb/devices")


class UsbRole(StrEnum):
    HUB = "hub"
    KEYBOARD = "keyboard"
    MOUSE = "mouse"
    GAMEPAD = "gamepad"
    HID = "hid"
    TOUCHSCREEN = "touchscreen"
    CAMERA = "camera"
    AUDIO = "audio"
    STORAGE = "storage"
    WIFI = "wifi"
    ETHERNET = "ethernet"
    BLUETOOTH = "bluetooth"
    PHONE = "phone"
    PRINTER = "printer"
    SCANNER = "scanner"
    DISPLAY = "display"
    SERIAL = "serial"
    SMARTCARD = "smartcard"
    BILLBOARD = "billboard"
    CHARGING = "charging"
    UNKNOWN = "unknown"


ROLE_LABEL: dict[UsbRole, str] = {
    UsbRole.HUB: "USB 허브",
    UsbRole.KEYBOARD: "키보드",
    UsbRole.MOUSE: "마우스",
    UsbRole.GAMEPAD: "게임패드",
    UsbRole.HID: "HID 입력장치",
    UsbRole.TOUCHSCREEN: "터치스크린",
    UsbRole.CAMERA: "카메라 (UVC)",
    UsbRole.AUDIO: "오디오 / AUX 어댑터",
    UsbRole.STORAGE: "저장장치",
    UsbRole.WIFI: "무선랜 (Wi-Fi)",
    UsbRole.ETHERNET: "유선랜",
    UsbRole.BLUETOOTH: "블루투스",
    UsbRole.PHONE: "휴대폰 / 모바일기기",
    UsbRole.PRINTER: "프린터",
    UsbRole.SCANNER: "스캐너",
    UsbRole.DISPLAY: "디스플레이 어댑터",
    UsbRole.SERIAL: "시리얼 포트",
    UsbRole.SMARTCARD: "스마트카드 리더",
    UsbRole.BILLBOARD: "USB-C Billboard (alt mode 협상)",
    UsbRole.CHARGING: "충전 전용",
    UsbRole.UNKNOWN: "미확인",
}

ROLE_STYLE: dict[UsbRole, str] = {
    UsbRole.HUB: "white",
    UsbRole.KEYBOARD: "bright_cyan",
    UsbRole.MOUSE: "cyan",
    UsbRole.GAMEPAD: "cyan",
    UsbRole.HID: "cyan",
    UsbRole.TOUCHSCREEN: "cyan",
    UsbRole.CAMERA: "bright_magenta",
    UsbRole.AUDIO: "bright_yellow",
    UsbRole.STORAGE: "bright_green",
    UsbRole.WIFI: "bright_blue",
    UsbRole.ETHERNET: "blue",
    UsbRole.BLUETOOTH: "blue",
    UsbRole.PHONE: "magenta",
    UsbRole.PRINTER: "yellow",
    UsbRole.SCANNER: "yellow",
    UsbRole.DISPLAY: "bright_red",
    UsbRole.SERIAL: "yellow",
    UsbRole.SMARTCARD: "white",
    UsbRole.BILLBOARD: "bright_red",
    UsbRole.CHARGING: "dim",
    UsbRole.UNKNOWN: "dim",
}

#: Base class codes, for display.
USB_CLASSES: dict[int, str] = {
    0x00: "per-interface", 0x01: "Audio", 0x02: "Communications (CDC)",
    0x03: "HID", 0x05: "Physical", 0x06: "Image (PTP)", 0x07: "Printer",
    0x08: "Mass storage", 0x09: "Hub", 0x0A: "CDC-Data", 0x0B: "Smart card",
    0x0D: "Content security", 0x0E: "Video (UVC)", 0x0F: "Personal healthcare",
    0x10: "Audio/Video", 0x11: "Billboard", 0x12: "USB-C bridge",
    0xDC: "Diagnostic", 0xE0: "Wireless", 0xEF: "Miscellaneous",
    0xFE: "Application specific", 0xFF: "Vendor specific",
}

#: Vendor IDs whose devices are phones often enough to be worth naming.
PHONE_VENDORS: dict[str, str] = {
    "18d1": "Google / Android", "04e8": "Samsung", "05ac": "Apple",
    "2717": "Xiaomi", "1004": "LG", "2a70": "OnePlus", "12d1": "Huawei",
    "0fce": "Sony", "22b8": "Motorola", "0bb4": "HTC", "19d2": "ZTE",
    "2916": "Android (generic)", "1f3a": "Allwinner", "2d95": "Vivo",
    "22d9": "Oppo",
}

#: Vendors that make USB display adapters.
DISPLAY_VENDORS: dict[str, str] = {
    "17e9": "DisplayLink",
    "1d5c": "Fresco Logic (USB display)",
    "0711": "Magic Control Technology (USB video)",
}


@dataclass(slots=True)
class InterfaceFacts:
    number: str = ""
    cls: int | None = None
    subclass: int | None = None
    protocol: int | None = None
    driver: str = ""
    #: Subsystem directories the kernel created under this interface, e.g.
    #: {"net": ["wlan1"], "video4linux": ["video0", "video1"]}.
    subsystems: dict[str, list[str]] = field(default_factory=dict)

    @property
    def triple(self) -> str:
        def fmt(v):
            return f"0x{v:02x}" if v is not None else "??"
        return f"{fmt(self.cls)}/{fmt(self.subclass)}/{fmt(self.protocol)}"


@dataclass(slots=True)
class DeviceFacts:
    address: str = ""
    vid: str = ""
    pid: str = ""
    vendor: str = ""
    product: str = ""
    serial: str = ""
    speed: str = ""
    device_class: int | None = None
    interfaces: list[InterfaceFacts] = field(default_factory=list)
    is_wireless: bool = False        # a net child that owns a phy80211
    #: Raw descriptor tree, read through the system's USB permissions. Optional
    #: — everything above still works without it, this only adds corroboration.
    descriptors: object | None = None

    @property
    def id(self) -> str:
        return f"{self.vid}:{self.pid}"


@dataclass(slots=True)
class RoleEvidence:
    role: UsbRole
    source: str            # "kernel binding" / "interface descriptor" / "vendor id"
    observed: str
    reason: str
    definitive: bool = False

    def as_dict(self) -> dict:
        return {
            "role": str(self.role),
            "source": self.source,
            "observed": self.observed,
            "reason": self.reason,
            "definitive": self.definitive,
        }


@dataclass(slots=True)
class RoleVerdict:
    roles: list[UsbRole] = field(default_factory=list)
    evidence: list[RoleEvidence] = field(default_factory=list)
    facts: DeviceFacts | None = None

    @property
    def primary(self) -> UsbRole:
        return self.roles[0] if self.roles else UsbRole.UNKNOWN

    @property
    def label(self) -> str:
        if not self.roles:
            return ROLE_LABEL[UsbRole.UNKNOWN]
        names = [ROLE_LABEL[r] for r in self.roles]
        return " + ".join(names)

    @property
    def short(self) -> str:
        return "+".join(str(r) for r in self.roles) or str(UsbRole.UNKNOWN)

    def as_dict(self) -> dict:
        return {
            "roles": [str(r) for r in self.roles],
            "primary": str(self.primary),
            "label": self.label,
            "evidence": [e.as_dict() for e in self.evidence],
        }


# --------------------------------------------------------------------------
# identification
# --------------------------------------------------------------------------

#: Sorting weight: which role leads when a device has several. Lower wins.
_ROLE_PRIORITY: dict[UsbRole, int] = {
    UsbRole.STORAGE: 0, UsbRole.CAMERA: 1, UsbRole.DISPLAY: 2, UsbRole.PHONE: 3,
    UsbRole.WIFI: 4, UsbRole.ETHERNET: 5, UsbRole.BLUETOOTH: 6,
    UsbRole.AUDIO: 7, UsbRole.KEYBOARD: 8, UsbRole.MOUSE: 9,
    UsbRole.TOUCHSCREEN: 10, UsbRole.GAMEPAD: 11, UsbRole.SERIAL: 12,
    UsbRole.PRINTER: 13, UsbRole.SCANNER: 14, UsbRole.SMARTCARD: 15,
    UsbRole.BILLBOARD: 16, UsbRole.HID: 17, UsbRole.HUB: 18,
    UsbRole.CHARGING: 19, UsbRole.UNKNOWN: 99,
}


def identify(facts: DeviceFacts) -> RoleVerdict:
    """Pure function: device facts in, roles + evidence out."""
    evidence: list[RoleEvidence] = []
    found: set[UsbRole] = set()

    def add(role, source, observed, reason, definitive=False):
        evidence.append(RoleEvidence(role, source, observed, reason, definitive))
        found.add(role)

    for iface in facts.interfaces:
        _from_kernel_binding(iface, facts, add)
        _from_descriptor(iface, facts, add)

    _from_vendor(facts, add)
    _from_raw_descriptors(facts, add)

    # A hub that also reports something else is still, mainly, a hub.
    if UsbRole.HUB in found and len(found) > 1:
        found.discard(UsbRole.HUB)
        if not found:
            found.add(UsbRole.HUB)

    roles = sorted(found, key=lambda r: _ROLE_PRIORITY.get(r, 50))
    return RoleVerdict(roles=roles, evidence=evidence, facts=facts)


def _from_kernel_binding(iface: InterfaceFacts, facts: DeviceFacts, add) -> None:
    """What the kernel actually created under this interface. Definitive."""
    subs = iface.subsystems

    if "net" in subs:
        names = ", ".join(subs["net"])
        if facts.is_wireless:
            add(UsbRole.WIFI, "kernel binding", f"net/{names} with phy80211",
                "The kernel registered a wireless PHY for this interface — it is "
                "a Wi-Fi adapter, not merely a network device.", definitive=True)
        else:
            add(UsbRole.ETHERNET, "kernel binding", f"net/{names}",
                "A network interface was created, with no wireless PHY behind it.",
                definitive=True)

    if "video4linux" in subs:
        add(UsbRole.CAMERA, "kernel binding", "video4linux/" + ", ".join(subs["video4linux"]),
            "A V4L2 capture node exists, so the kernel can pull frames from it.",
            definitive=True)

    if "sound" in subs:
        add(UsbRole.AUDIO, "kernel binding", "sound/" + ", ".join(subs["sound"]),
            "An ALSA card was registered — this is an audio device "
            "(a USB-C to 3.5mm dongle looks exactly like this).", definitive=True)

    if "tty" in subs:
        add(UsbRole.SERIAL, "kernel binding", "tty/" + ", ".join(subs["tty"]),
            "A tty node was created, so it presents as a serial port.", definitive=True)

    if "bluetooth" in subs:
        add(UsbRole.BLUETOOTH, "kernel binding", "bluetooth/" + ", ".join(subs["bluetooth"]),
            "An HCI device was registered.", definitive=True)

    if "scsi_host" in subs or "host" in subs:
        add(UsbRole.STORAGE, "kernel binding", "SCSI host attached",
            "A SCSI host was created for this interface.", definitive=True)

    if "input" in subs and iface.cls == 0x03:
        names = subs["input"]
        detail = _input_kind(names)
        if detail:
            role, why = detail
            add(role, "kernel binding", "input/" + ", ".join(names), why, definitive=True)


def _from_descriptor(iface: InterfaceFacts, facts: DeviceFacts, add) -> None:
    """What the device asserts about itself in its interface descriptor."""
    cls, sub, proto = iface.cls, iface.subclass, iface.protocol
    if cls is None:
        return
    triple = iface.triple

    if cls == 0x09:
        add(UsbRole.HUB, "interface descriptor", f"{triple} — Hub",
            "Class 0x09 is a hub.")

    elif cls == 0x03:
        if sub == 0x01 and proto == 0x01:
            add(UsbRole.KEYBOARD, "interface descriptor", f"{triple} — HID boot keyboard",
                "Boot-protocol 0x01 is defined as a keyboard, so a BIOS can use "
                "it before any driver loads.")
        elif sub == 0x01 and proto == 0x02:
            add(UsbRole.MOUSE, "interface descriptor", f"{triple} — HID boot mouse",
                "Boot-protocol 0x02 is defined as a mouse.")
        else:
            add(UsbRole.HID, "interface descriptor", f"{triple} — HID",
                "A human-interface device that doesn't claim the keyboard or "
                "mouse boot protocol.")

    elif cls == 0x0E:
        add(UsbRole.CAMERA, "interface descriptor", f"{triple} — Video (UVC)",
            "The USB Video Class — webcams and capture devices.")

    elif cls == 0x01:
        add(UsbRole.AUDIO, "interface descriptor", f"{triple} — Audio",
            "The USB Audio Class. USB-C headphone dongles and DACs live here.")

    elif cls == 0x08:
        add(UsbRole.STORAGE, "interface descriptor", f"{triple} — Mass storage",
            "Mass storage — run `updev usb classify` for the storage subtype.")

    elif cls == 0x07:
        add(UsbRole.PRINTER, "interface descriptor", f"{triple} — Printer",
            "The printer class.")

    elif cls == 0x06:
        role = UsbRole.PHONE if facts.vid in PHONE_VENDORS else UsbRole.SCANNER
        add(role, "interface descriptor", f"{triple} — Image (PTP)",
            "The still-image class. Phones expose PTP/MTP here; scanners and "
            "cameras use it too.")

    elif cls == 0x0B:
        add(UsbRole.SMARTCARD, "interface descriptor", f"{triple} — Smart card",
            "A CCID smart-card reader.")

    elif cls == 0x11:
        add(UsbRole.BILLBOARD, "interface descriptor", f"{triple} — Billboard",
            "A Billboard device exists to report that a USB-C alternate mode "
            "was requested but not established. A portable USB-C monitor shows "
            "up like this when the host can't do DisplayPort alt mode — which "
            "the Raspberry Pi 5 cannot; its USB-C port is power only.")

    elif cls == 0x02:
        if sub == 0x02:
            add(UsbRole.SERIAL, "interface descriptor", f"{triple} — CDC-ACM",
                "The abstract control model — a serial port over USB.")
        elif sub in (0x06, 0x0C, 0x0E):
            add(UsbRole.ETHERNET, "interface descriptor", f"{triple} — CDC ECM/NCM/MBIM",
                "A CDC networking model — USB Ethernet, or phone tethering.")

    elif cls == 0xE0:
        if sub == 0x01 and proto == 0x01:
            add(UsbRole.BLUETOOTH, "interface descriptor", f"{triple} — Wireless/Bluetooth",
                "The Bluetooth programming interface.")
        elif sub == 0x01 and proto == 0x03:
            add(UsbRole.ETHERNET, "interface descriptor", f"{triple} — RNDIS",
                "RNDIS — usually a phone sharing its connection.")

    elif cls == 0xEF and sub == 0x04 and proto == 0x01:
        add(UsbRole.ETHERNET, "interface descriptor", f"{triple} — RNDIS (IAD)",
            "RNDIS behind an interface association — typically USB tethering.")

    elif cls == 0xFF:
        if sub == 0x42 and proto == 0x01:
            add(UsbRole.PHONE, "interface descriptor", f"{triple} — Android ADB",
                "Subclass 0x42 protocol 0x01 is Android's debug bridge. Only a "
                "device running Android exposes it.")
        elif sub == 0xFE and proto == 0x02:
            add(UsbRole.PHONE, "interface descriptor", f"{triple} — Apple Mobile Device",
                "Apple's usbmuxd interface — an iPhone or iPad.")
        elif sub == 0xFF and iface.driver in _WIFI_DRIVERS:
            add(UsbRole.WIFI, "driver", iface.driver,
                "A vendor-specific interface claimed by a known Wi-Fi driver.")


def _from_vendor(facts: DeviceFacts, add) -> None:
    if facts.vid in DISPLAY_VENDORS:
        add(UsbRole.DISPLAY, "vendor id", f"{facts.vid} — {DISPLAY_VENDORS[facts.vid]}",
            "A USB display adapter. It carries video over USB rather than "
            "DisplayPort alt mode, so it works on hosts without alt mode.")
    if facts.vid in PHONE_VENDORS and any(
        i.cls in (0x06, 0xFF) for i in facts.interfaces
    ):
        add(UsbRole.PHONE, "vendor id", f"{facts.vid} — {PHONE_VENDORS[facts.vid]}",
            "A mobile-device vendor, with an image or vendor-specific interface.")


def _from_raw_descriptors(facts: DeviceFacts, add) -> None:
    """Corroboration only the descriptor tree can offer.

    Isochronous endpoints are the interesting one: they reserve bus bandwidth
    every microframe whether or not data flows, so the USB spec only lets you
    ask for them if you genuinely stream. Nothing pretends to have them.
    """
    tree = facts.descriptors
    if tree is None or not getattr(tree, "device", None):
        return

    iso = [ep for ep in tree.endpoints if ep.transfer_type == "isochronous"]
    if iso:
        widest = max(ep.bandwidth_per_frame for ep in iso)
        streaming = next(
            (r for r in (UsbRole.CAMERA, UsbRole.AUDIO)
             if any(i.cls == (0x0E if r is UsbRole.CAMERA else 0x01)
                    for i in facts.interfaces)),
            None,
        )
        if streaming:
            add(streaming, "raw descriptors",
                f"{len(iso)} isochronous endpoint(s), {widest} B/microframe",
                "Isochronous endpoints reserve guaranteed bandwidth. Only real "
                "streaming hardware is allowed to request them.")

    for config in getattr(tree, "configurations", []):
        for assoc in config.associations:
            if assoc.cls == 0x0E:
                add(UsbRole.CAMERA, "raw descriptors",
                    f"interface association, function class 0x{assoc.cls:02x}",
                    f"{assoc.interface_count} interfaces are grouped into one video "
                    "function — VideoControl plus VideoStreaming.")
            elif assoc.cls == 0x01:
                add(UsbRole.AUDIO, "raw descriptors",
                    f"interface association, function class 0x{assoc.cls:02x}",
                    f"{assoc.interface_count} interfaces grouped into one audio function.")


#: Drivers that only ever bind Wi-Fi hardware.
_WIFI_DRIVERS = {
    "rtl8192cu", "rtl8188eu", "rtl8xxxu", "rtw88_8821cu", "rtw88_8822bu",
    "mt7601u", "mt76x0u", "mt76x2u", "mt7921u", "carl9170", "ath9k_htc",
    "brcmfmac", "rndis_wlan", "r8188eu", "88XXau", "rtl88x2bu",
}


def _input_kind(names: list[str]) -> tuple[UsbRole, str] | None:
    """Read the input device names the kernel registered, which spell out
    what the HID descriptor actually declared."""
    joined = " ".join(_input_name(n) for n in names).lower()
    if not joined:
        return None
    if "touchscreen" in joined or "touch screen" in joined:
        return UsbRole.TOUCHSCREEN, "The kernel named the input device a touchscreen."
    if "gamepad" in joined or "joystick" in joined or "controller" in joined:
        return UsbRole.GAMEPAD, "The kernel named the input device a gamepad/joystick."
    if "keyboard" in joined:
        return UsbRole.KEYBOARD, "The kernel named the registered input device a keyboard."
    if "mouse" in joined or "touchpad" in joined:
        return UsbRole.MOUSE, "The kernel named the registered input device a mouse."
    return None


def _input_name(node: str) -> str:
    return read_text(f"/sys/class/input/{node}/name")


def describe_class(cls: int | None) -> str:
    if cls is None:
        return "unknown"
    return USB_CLASSES.get(cls, f"0x{cls:02x}")


# --------------------------------------------------------------------------
# reading facts from sysfs
# --------------------------------------------------------------------------

#: Subsystem directories worth noticing under an interface.
_WATCHED_SUBSYSTEMS = (
    "net", "video4linux", "sound", "tty", "bluetooth", "input",
    "host", "scsi_host", "hidraw", "usbmisc",
)


def gather_device_facts(address: str) -> DeviceFacts:
    """Read a USB device and all of its interfaces from sysfs."""
    base = USB_ROOT / address
    facts = DeviceFacts(address=address)
    if not base.is_dir():
        return facts

    facts.vid = read_text(base / "idVendor").lower()
    facts.pid = read_text(base / "idProduct").lower()
    facts.vendor = read_text(base / "manufacturer")
    facts.product = read_text(base / "product")
    facts.serial = read_text(base / "serial")
    facts.speed = read_text(base / "speed")
    facts.device_class = read_hex(base / "bDeviceClass")

    try:
        names = sorted(p for p in os.listdir(USB_ROOT) if p.startswith(address + ":"))
    except OSError:
        return facts

    for name in names:
        path = USB_ROOT / name
        iface = InterfaceFacts(number=name.split(":", 1)[1])
        iface.cls = read_hex(path / "bInterfaceClass")
        iface.subclass = read_hex(path / "bInterfaceSubClass")
        iface.protocol = read_hex(path / "bInterfaceProtocol")
        link = path / "driver"
        if link.exists():
            try:
                iface.driver = link.resolve().name
            except OSError:
                pass
        for sub in _WATCHED_SUBSYSTEMS:
            entries = _list_dir(path / sub)
            if entries:
                iface.subsystems[sub] = entries
        # `host0/` style SCSI attachment sits directly under the interface.
        hosts = [e for e in _list_dir(path) if e.startswith("host")]
        if hosts:
            iface.subsystems.setdefault("host", hosts)
        facts.interfaces.append(iface)

        for net in iface.subsystems.get("net", []):
            if Path(f"/sys/class/net/{net}/phy80211").exists():
                facts.is_wireless = True

    # Read through the system's USB permissions. Best effort — usbfs is
    # world-readable on Debian, but a locked-down system may say no, and
    # everything above still works without it.
    try:
        from .usbdesc import parse_descriptors, read_raw_descriptors

        blob, source, error = read_raw_descriptors(address)
        if blob and not error:
            facts.descriptors = parse_descriptors(blob, source)
    except Exception:
        pass

    return facts


def _list_dir(path: Path) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


# --------------------------------------------------------------------------
# physical path tracing
# --------------------------------------------------------------------------

#: sysfs `speed` (Mbps) -> (marketing name, USB generation)
_SPEEDS: dict[str, tuple[str, int]] = {
    "1.5": ("Low-Speed 1.5 Mbps", 1),
    "12": ("Full-Speed 12 Mbps", 1),
    "480": ("High-Speed 480 Mbps", 2),
    "5000": ("SuperSpeed 5 Gbps", 3),
    "10000": ("SuperSpeed+ 10 Gbps", 3),
    "20000": ("SuperSpeed+ x2 20 Gbps", 3),
}


@dataclass(slots=True)
class PathHop:
    address: str
    label: str = ""
    port: int | None = None          # port number on the parent hub
    speed: str = ""                  # raw Mbps
    speed_label: str = ""
    generation: int = 0              # what it actually negotiated
    declared: str = ""               # bcdUSB, e.g. "2.00"
    declared_generation: int = 0     # what it says it is capable of
    is_root_hub: bool = False
    is_target: bool = False
    node: str = ""
    ports: int | None = None         # how many downstream ports (hubs)

    @property
    def underperforming(self) -> bool:
        """Negotiated below what its bcdUSB actually promises.

        Restricted to 3.x on purpose: bcdUSB 2.00 states spec compliance, not
        supported speed, and Full-Speed-only devices report it routinely. Only
        a 3.x device on a sub-SuperSpeed link is unambiguously degraded.
        """
        return bool(
            self.declared_generation >= 3
            and self.generation
            and self.generation < self.declared_generation
        )

    @property
    def depth_hint(self) -> str:
        return "root hub" if self.is_root_hub else (
            f"port {self.port}" if self.port is not None else "")


def parent_address(address: str) -> str:
    """`1-2.3.4` → `1-2.3`; `1-2` → `usb1`; a root hub has no parent."""
    if address.startswith("usb"):
        return ""
    if "." in address:
        return address.rsplit(".", 1)[0]
    if "-" in address:
        return "usb" + address.split("-", 1)[0]
    return ""


def port_number(address: str) -> int | None:
    """The port this device occupies on its immediate parent."""
    tail = address.rsplit(".", 1)[-1] if "." in address else address.split("-")[-1]
    try:
        return int(tail)
    except ValueError:
        return None


def build_path(address: str) -> list[PathHop]:
    """Walk from the root hub down to `address`, one hop per hub crossed."""
    chain: list[str] = []
    cursor = address
    seen: set[str] = set()
    while cursor and cursor not in seen:
        seen.add(cursor)
        chain.append(cursor)
        cursor = parent_address(cursor)
    chain.reverse()

    hops: list[PathHop] = []
    for addr in chain:
        base = USB_ROOT / addr
        if not base.is_dir():
            continue
        speed = read_text(base / "speed")
        label, gen = _SPEEDS.get(speed, (f"{speed} Mbps" if speed else "", 0))
        is_root = addr.startswith("usb")
        product = read_text(base / "product")
        vendor = read_text(base / "manufacturer")
        if is_root:
            # Root hubs call themselves "Linux <kernel> xhci-hcd xHCI Host
            # Controller", which is all noise.
            name = f"{addr} root hub"
        else:
            name = product or (
                f"USB {read_text(base / 'idVendor')}:{read_text(base / 'idProduct')}"
            )
            if vendor and vendor.lower() not in name.lower():
                name = f"{vendor} {name}"
        declared = read_text(base / "version").strip()
        hops.append(PathHop(
            address=addr,
            label=name,
            port=None if is_root else port_number(addr),
            speed=speed,
            speed_label=label,
            generation=gen,
            declared=declared,
            declared_generation=_declared_generation(declared),
            is_root_hub=is_root,
            is_target=(addr == address),
            node=str(base),
            ports=_int_or_none(read_text(base / "maxchild")),
        ))
    return hops


def _declared_generation(version: str) -> int:
    """bcdUSB -> the generation the device claims to support."""
    try:
        major = int(float(version))
    except (TypeError, ValueError):
        return 0
    return {1: 1, 2: 2, 3: 3}.get(major, 0)


def path_bottleneck(hops: list[PathHop]) -> tuple[int, str] | None:
    """Find the hop responsible for a slow link, and explain it.

    The culprit is the hop *closest to the root* that negotiated below its own
    declared capability — everything downstream simply inherits that ceiling,
    so blaming the leaf device would send you chasing the wrong cable.
    """
    for index, hop in enumerate(hops):
        if hop.underperforming:
            what = "This device" if hop.is_target else "Everything below it"
            return index, (
                f"{hop.label} declares USB {hop.declared} but negotiated only "
                f"{hop.speed_label}. {what} is capped there — usually a cable, "
                f"a worn connector, or a port that only wires USB 2 pins."
            )
    if len(hops) < 2:
        return None
    target = hops[-1]
    upstream = [h for h in hops[:-1] if h.generation]
    if target.generation and upstream:
        slowest = min(upstream, key=lambda h: h.generation)
        if slowest.generation < target.generation:
            return hops.index(slowest), (
                f"{slowest.label} only runs at {slowest.speed_label}, so this "
                f"device cannot exceed that no matter what it supports."
            )
    return None


def device_nodes(address: str) -> dict[str, list[str]]:
    """Every /dev node this USB device owns, grouped by subsystem."""
    facts = gather_device_facts(address)
    out: dict[str, list[str]] = {}
    node_prefix = {
        "net": "", "video4linux": "/dev/", "sound": "", "tty": "/dev/",
        "bluetooth": "", "input": "/dev/input/", "hidraw": "/dev/",
        "usbmisc": "/dev/",
    }
    for iface in facts.interfaces:
        for sub, names in iface.subsystems.items():
            if sub in ("host", "scsi_host"):
                continue
            prefix = node_prefix.get(sub, "")
            out.setdefault(sub, []).extend(prefix + n for n in names)
    blocks = _block_devices_for(address)
    if blocks:
        out["block"] = [f"/dev/{b}" for b in blocks]
    out.setdefault("usb", []).append(f"/dev/bus/usb/{_busnum(address)}/{_devnum(address)}")
    return {k: sorted(set(v)) for k, v in out.items() if v}


def _block_devices_for(address: str) -> list[str]:
    from .core.util import usb_address_from_path

    found = []
    try:
        entries = sorted(os.listdir("/sys/block"))
    except OSError:
        return found
    for name in entries:
        if name.startswith(("loop", "ram", "zram")):
            continue
        try:
            real = str(Path(f"/sys/block/{name}").resolve())
        except OSError:
            continue
        if usb_address_from_path(real) == address:
            found.append(name)
    return found


def _busnum(address: str) -> str:
    return read_text(USB_ROOT / address / "busnum").zfill(3) or "???"


def _devnum(address: str) -> str:
    return read_text(USB_ROOT / address / "devnum").zfill(3) or "???"


def _int_or_none(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None
