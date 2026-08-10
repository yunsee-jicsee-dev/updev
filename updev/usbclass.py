"""Signature-based classification of USB mass-storage devices.

The hard part of telling a thumb drive from an external SSD is that both are
flash behind a USB bridge, and neither announces what it is. Nothing in USB
says "I am a floppy" or "I am an enclosure" — so we read the signatures the
specs *do* mandate, weigh them, and show our work.

Six signatures, all readable from sysfs without root:

  1. USB bInterfaceSubClass   — the command set. 0x04 is literally UFI,
                                "USB Floppy Interface"; 0x02 is ATAPI/MMC-5,
                                which only optical drives speak.
  2. USB bInterfaceProtocol   — the transport. CBI (0x00/0x01) was only ever
                                used by floppies; UAS (0x62) is an enclosure
                                feature that no cheap stick implements.
  3. SCSI peripheral type     — INQUIRY byte 0. Type 5 is a CD/DVD, full stop.
  4. RMB, removable medium    — INQUIRY byte 1 bit 7. Thumb drives and floppies
                                set it; a bridge fronting a fixed disk doesn't.
  5. Medium rotation rate     — VPD page 0xB1 bytes 4-5. The drive states its
                                own RPM: 0x0001 means non-rotating, anything
                                from 0x0401 up is a literal spindle speed.
                                This is the single best HDD/SSD discriminator.
  6. Capacity and form factor — corroborating, never decisive.

Rules that the spec makes unambiguous are marked `decisive` and end the
argument. Everything else contributes weighted evidence, and the caller gets
the full trail so a wrong answer is arguable rather than mysterious.

The classifier itself is a pure function of `StorageFacts`, so every device
category can be tested without owning one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .core.util import read_hex, read_int, read_text, usb_address_from_path

__all__ = [
    "Confidence",
    "Evidence",
    "StorageFacts",
    "UsbClass",
    "Verdict",
    "classify",
    "gather_facts",
    "signature_reference",
]


class UsbClass(StrEnum):
    """The taxonomy, in the caller's own vocabulary."""

    NUSB = "NUSB"          # plain USB flash drive
    FUSB = "FUSB"          # floppy / UFI removable
    HUSB = "HUSB"          # external spinning hard disk
    SUSB = "SUSB"          # external solid-state disk
    ODD = "ODD"            # optical disc drive
    UNKNOWN = "?USB"       # mass storage we can't place


CLASS_LABEL: dict[UsbClass, str] = {
    UsbClass.NUSB: "단순 USB (flash drive)",
    UsbClass.FUSB: "플로피 (floppy / UFI)",
    UsbClass.HUSB: "외장 HDD (spinning)",
    UsbClass.SUSB: "외장 SSD (solid-state)",
    UsbClass.ODD: "광학 드라이브 (optical)",
    UsbClass.UNKNOWN: "미분류 mass storage",
}

CLASS_STYLE: dict[UsbClass, str] = {
    UsbClass.NUSB: "bright_cyan",
    UsbClass.FUSB: "bright_magenta",
    UsbClass.HUSB: "bright_yellow",
    UsbClass.SUSB: "bright_green",
    UsbClass.ODD: "bright_blue",
    UsbClass.UNKNOWN: "dim",
}

# Evidence weights. DECISIVE means the spec leaves no room for argument.
DECISIVE = 100
STRONG = 45
MEDIUM = 22
WEAK = 9


class Confidence(StrEnum):
    CERTAIN = "certain"     # a decisive signature fired
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# --------------------------------------------------------------------------
# descriptor tables
# --------------------------------------------------------------------------

#: USB mass-storage subclass = which command set the device speaks.
SUBCLASSES: dict[int, str] = {
    0x00: "SCSI command set not reported",
    0x01: "RBC (Reduced Block Commands)",
    0x02: "MMC-5 / ATAPI (optical)",
    0x03: "QIC-157 (tape)",
    0x04: "UFI (USB Floppy Interface)",
    0x05: "SFF-8070i (ATAPI removable)",
    0x06: "SCSI transparent",
    0x07: "LSD FS",
    0x08: "IEEE 1667",
}

#: USB mass-storage protocol = how commands are wrapped on the wire.
PROTOCOLS: dict[int, str] = {
    0x00: "CBI with completion interrupt",
    0x01: "CBI without completion interrupt",
    0x50: "BBB (Bulk-Only Transport)",
    0x62: "UAS (USB Attached SCSI)",
}

#: SCSI peripheral device type, INQUIRY byte 0 bits 0-4.
PERIPHERAL_TYPES: dict[int, str] = {
    0x00: "direct-access block device",
    0x01: "sequential-access (tape)",
    0x04: "write-once",
    0x05: "CD/DVD",
    0x07: "optical memory",
    0x08: "medium changer",
    0x0E: "simplified direct-access (RBC)",
}

#: VPD 0xB1 byte 7, low nibble.
FORM_FACTORS: dict[int, str] = {
    0x00: "not reported",
    0x01: "5.25 inch",
    0x02: "3.5 inch",
    0x03: "2.5 inch",
    0x04: "1.8 inch",
    0x05: "under 1.8 inch",
}

#: Exact capacities that only a floppy has.
FLOPPY_SIZES: dict[int, str] = {
    368_640: "360 KB (5.25\" DD)",
    737_280: "720 KB (3.5\" DD)",
    1_228_800: "1.2 MB (5.25\" HD)",
    1_474_560: "1.44 MB (3.5\" HD)",
    2_949_120: "2.88 MB (3.5\" ED)",
}

_GB = 1024 ** 3

# Product strings often name the medium outright — an enclosure that calls
# itself "M.2 SATA" is telling you there is no spindle in there. Checked in
# this order: a solid-state match wins outright, because HDD family patterns
# can collide with SSD model numbers ("WD_BLACK SN770" is an NVMe drive).
_SOLID_STATE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bM\.?\s?2\b", "M.2 — a form factor only solid-state drives use"),
    (r"\bNVM[eE]?\b", "NVMe — solid-state by definition"),
    (r"\bSSD\b", "the product name says SSD"),
    (r"\bsolid[\s-]?state\b", "the product name says solid-state"),
    (r"\bSN\d{3}\b", "WD SN-series NVMe family"),
    (r"\b(MX|BX)\d{3}\b", "Crucial MX/BX SSD family"),
    (r"\b8[6-7]0\s?(EVO|QVO|PRO)\b", "Samsung EVO/PRO SSD family"),
)

_SPINNING_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bHDD\b", "the product name says HDD"),
    (r"\bhard[\s-]?disk\b", "the product name says hard disk"),
    (r"\bWDC?\s*WD\d{2,}[A-Z]", "Western Digital hard-disk family"),
    (r"\bST\d{3,}[A-Z]", "Seagate hard-disk family"),
    (r"\bHTS\d{3}", "HGST mobile hard-disk family"),
    (r"\bMQ0[12]ABF", "Toshiba mobile hard-disk family"),
    (r"\bbarracuda\b", "Seagate Barracuda hard-disk line"),
)


def _match_product(strings: str) -> tuple[UsbClass, str, str] | None:
    """(class, matched text, why) from the device's own product strings."""
    import re

    for pattern, why in _SOLID_STATE_PATTERNS:
        m = re.search(pattern, strings, re.I)
        if m:
            return UsbClass.SUSB, m.group(0), why
    for pattern, why in _SPINNING_PATTERNS:
        m = re.search(pattern, strings, re.I)
        if m:
            return UsbClass.HUSB, m.group(0), why
    return None


# --------------------------------------------------------------------------
# facts
# --------------------------------------------------------------------------

@dataclass(slots=True)
class StorageFacts:
    """Everything we could read. `None` means "not reported", which is itself
    informative — a device that doesn't implement VPD 0xB1 is telling you
    something about how cheap its controller is."""

    usb_address: str = ""
    block_name: str = ""
    vendor: str = ""
    model: str = ""
    # Kept separately because they disagree, and the disagreement is useful:
    # a bridge often advertises a marketing name over USB ("Best USB Device")
    # while the SCSI layer names what is actually inside it ("M.2 SATA").
    usb_vendor: str = ""
    usb_product: str = ""
    scsi_vendor: str = ""
    scsi_model: str = ""

    # USB interface descriptor
    interface_subclass: int | None = None
    interface_protocol: int | None = None
    driver: str = ""

    # SCSI INQUIRY
    peripheral_type: int | None = None
    removable_medium: bool | None = None        # RMB, INQUIRY byte 1 bit 7
    scsi_level: int | None = None

    # VPD page 0xB1, Block Device Characteristics
    has_vpd_b1: bool = False
    rotation_rate: int | None = None            # 0 unreported, 1 non-rotating, else RPM
    form_factor: int | None = None

    # block layer
    size_bytes: int = 0
    rotational: bool | None = None
    block_removable: bool | None = None

    @property
    def is_mass_storage(self) -> bool:
        return self.interface_subclass is not None or self.block_name != ""

    @property
    def product_strings(self) -> str:
        """Every name the device gave us, for pattern matching."""
        return " ".join(
            s for s in (self.usb_vendor, self.usb_product,
                        self.scsi_vendor, self.scsi_model) if s
        )

    def describe(self) -> dict[str, str]:
        """Human-readable dump of the raw readings, for the evidence panel."""
        out: dict[str, str] = {}
        if self.interface_subclass is not None:
            out["bInterfaceSubClass"] = (
                f"0x{self.interface_subclass:02x} — "
                f"{SUBCLASSES.get(self.interface_subclass, 'unknown')}"
            )
        if self.interface_protocol is not None:
            out["bInterfaceProtocol"] = (
                f"0x{self.interface_protocol:02x} — "
                f"{PROTOCOLS.get(self.interface_protocol, 'unknown')}"
            )
        if self.driver:
            out["kernel driver"] = self.driver
        if self.peripheral_type is not None:
            out["SCSI peripheral type"] = (
                f"0x{self.peripheral_type:02x} — "
                f"{PERIPHERAL_TYPES.get(self.peripheral_type, 'unknown')}"
            )
        if self.removable_medium is not None:
            out["RMB (removable medium)"] = (
                "1 — medium can be removed" if self.removable_medium
                else "0 — medium is fixed"
            )
        if self.has_vpd_b1:
            rate = self.rotation_rate
            if rate == 1:
                shown = "0x0001 — non-rotating (flash)"
            elif rate in (0, None):
                shown = "0x0000 — not reported"
            else:
                shown = f"0x{rate:04x} — {rate} RPM (spinning)"
            out["VPD 0xB1 rotation rate"] = shown
            if self.form_factor is not None:
                out["VPD 0xB1 form factor"] = FORM_FACTORS.get(
                    self.form_factor, f"0x{self.form_factor:02x}"
                )
        else:
            out["VPD 0xB1"] = "not implemented by this device"
        if self.rotational is not None:
            out["block layer rotational"] = "1" if self.rotational else "0"
        if self.size_bytes:
            out["capacity"] = _human_capacity(self.size_bytes)
        if self.usb_product or self.usb_vendor:
            out["USB descriptor"] = f"{self.usb_vendor} {self.usb_product}".strip()
        if self.scsi_vendor or self.scsi_model:
            out["SCSI INQUIRY strings"] = f"{self.scsi_vendor} {self.scsi_model}".strip()
        return out


@dataclass(slots=True)
class Evidence:
    """One signature that fired, and what it argues for."""

    signature: str
    observed: str
    scores: dict[UsbClass, int]
    reason: str
    decisive: bool = False

    @property
    def favours(self) -> UsbClass:
        return max(self.scores, key=lambda k: self.scores[k])

    def as_dict(self) -> dict:
        return {
            "signature": self.signature,
            "observed": self.observed,
            "scores": {str(k): v for k, v in self.scores.items()},
            "reason": self.reason,
            "decisive": self.decisive,
        }


@dataclass(slots=True)
class Verdict:
    usb_class: UsbClass
    confidence: Confidence
    scores: dict[UsbClass, int] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    facts: StorageFacts | None = None
    margin: int = 0

    @property
    def label(self) -> str:
        return CLASS_LABEL.get(self.usb_class, str(self.usb_class))

    @property
    def runner_up(self) -> UsbClass | None:
        ranked = sorted(self.scores.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[1][0] if len(ranked) > 1 and ranked[1][1] > 0 else None

    def as_dict(self) -> dict:
        return {
            "class": str(self.usb_class),
            "label": self.label,
            "confidence": str(self.confidence),
            "margin": self.margin,
            "scores": {str(k): v for k, v in self.scores.items()},
            "evidence": [e.as_dict() for e in self.evidence],
            "facts": self.facts.describe() if self.facts else {},
        }


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------

def classify(facts: StorageFacts) -> Verdict:
    """Pure function: facts in, verdict + evidence trail out."""
    evidence: list[Evidence] = []

    def fire(signature, observed, scores, reason, decisive=False):
        evidence.append(Evidence(signature, observed, scores, reason, decisive))

    # -- decisive signatures ------------------------------------------------

    if facts.peripheral_type in (0x05, 0x07):
        fire(
            "SCSI peripheral type",
            f"0x{facts.peripheral_type:02x} — {PERIPHERAL_TYPES[facts.peripheral_type]}",
            {UsbClass.ODD: DECISIVE},
            "Only an optical drive reports this type. Nothing else can.",
            decisive=True,
        )

    if facts.interface_subclass == 0x02:
        fire(
            "USB bInterfaceSubClass",
            "0x02 — MMC-5 / ATAPI",
            {UsbClass.ODD: DECISIVE},
            "MMC-5 is the optical-media command set; only ODDs speak it.",
            decisive=True,
        )

    if facts.interface_subclass == 0x04:
        fire(
            "USB bInterfaceSubClass",
            "0x04 — UFI",
            {UsbClass.FUSB: DECISIVE},
            "UFI stands for USB Floppy Interface. It was written for exactly "
            "this device and nothing else uses it.",
            decisive=True,
        )

    if facts.size_bytes in FLOPPY_SIZES:
        fire(
            "capacity",
            f"{facts.size_bytes:,} bytes — {FLOPPY_SIZES[facts.size_bytes]}",
            {UsbClass.FUSB: DECISIVE},
            "An exact standard floppy geometry. No other medium is this size.",
            decisive=True,
        )

    if facts.rotation_rate is not None and facts.rotation_rate >= 0x0401:
        fire(
            "VPD 0xB1 medium rotation rate",
            f"0x{facts.rotation_rate:04x} — {facts.rotation_rate} RPM",
            {UsbClass.HUSB: DECISIVE},
            "The drive is reporting its own spindle speed. It has a spindle.",
            decisive=True,
        )

    decisive_hits = [e for e in evidence if e.decisive]
    if decisive_hits:
        return _tally(evidence, facts, forced=True)

    # -- strong signatures --------------------------------------------------

    if facts.interface_subclass == 0x05:
        fire(
            "USB bInterfaceSubClass",
            "0x05 — SFF-8070i",
            {UsbClass.FUSB: STRONG, UsbClass.ODD: MEDIUM},
            "SFF-8070i is the ATAPI removable command set — LS-120 and floppy "
            "drives mostly, occasionally an optical drive.",
        )

    if facts.interface_protocol in (0x00, 0x01):
        fire(
            "USB bInterfaceProtocol",
            f"0x{facts.interface_protocol:02x} — "
            f"{PROTOCOLS.get(facts.interface_protocol, 'CBI')}",
            {UsbClass.FUSB: STRONG},
            "CBI was deprecated for everything except full-speed floppy drives. "
            "Modern storage uses Bulk-Only or UAS.",
        )

    if facts.interface_protocol == 0x62:
        fire(
            "USB bInterfaceProtocol",
            "0x62 — UAS",
            {UsbClass.SUSB: STRONG, UsbClass.HUSB: MEDIUM, UsbClass.NUSB: -MEDIUM},
            "UAS needs a real SCSI-capable bridge chip. Enclosures ship it; "
            "commodity flash drives don't.",
        )
    elif facts.interface_protocol == 0x50:
        fire(
            "USB bInterfaceProtocol",
            "0x50 — Bulk-Only Transport",
            {UsbClass.NUSB: MEDIUM},
            "The cheap, universal transport — what a plain flash drive uses.",
        )

    if facts.removable_medium is True:
        fire(
            "SCSI RMB bit",
            "1 — removable medium",
            {UsbClass.NUSB: STRONG, UsbClass.FUSB: MEDIUM, UsbClass.ODD: MEDIUM,
             UsbClass.SUSB: -MEDIUM, UsbClass.HUSB: -MEDIUM},
            "The device says its medium can be taken out. An enclosure with a "
            "disk bolted inside reports the opposite.",
        )
    elif facts.removable_medium is False:
        fire(
            "SCSI RMB bit",
            "0 — fixed medium",
            {UsbClass.SUSB: MEDIUM, UsbClass.HUSB: MEDIUM, UsbClass.NUSB: -WEAK},
            "A fixed medium behind a USB bridge is the signature of an "
            "enclosure holding a real drive.",
        )

    # -- rotation, the HDD/SSD split ---------------------------------------

    if facts.rotation_rate == 0x0001:
        fire(
            "VPD 0xB1 medium rotation rate",
            "0x0001 — non-rotating",
            {UsbClass.SUSB: MEDIUM, UsbClass.NUSB: WEAK, UsbClass.HUSB: -STRONG},
            "Explicitly solid-state. Implementing this page at all points to a "
            "real controller rather than a commodity stick.",
        )
    elif not facts.has_vpd_b1 and facts.peripheral_type is not None:
        # Only meaningful once we've actually reached the SCSI layer. Absence
        # of a reading is not the same as a device that answered "no page".
        fire(
            "VPD 0xB1",
            "not implemented",
            {UsbClass.NUSB: MEDIUM, UsbClass.FUSB: WEAK},
            "Cheap mass-produced sticks skip the optional characteristics page.",
        )
    elif facts.has_vpd_b1 and facts.rotation_rate in (0, None):
        fire(
            "VPD 0xB1 medium rotation rate",
            "0x0000 — not reported",
            {UsbClass.NUSB: WEAK},
            "The page exists but the field is blank — common on bridges that "
            "don't pass the query through to the drive.",
        )

    # The device's own product strings, which routinely name the medium when
    # every structured field has given up. This outranks the rotational flag
    # on purpose — see the rule below for why that flag can't be trusted.
    named = _match_product(facts.product_strings)
    if named:
        cls, matched, why = named
        other = UsbClass.HUSB if cls is UsbClass.SUSB else UsbClass.SUSB
        fire(
            "product strings",
            f"“{matched}” in {facts.product_strings.strip()!r}",
            {cls: STRONG, other: -STRONG},
            why,
        )

    if facts.rotation_rate is None and facts.rotational is True:
        fire(
            "block layer rotational flag",
            "1 — kernel treats it as spinning",
            {UsbClass.HUSB: MEDIUM if not named else WEAK},
            "Weak on its own: USB bridges default this to 1 whether or not "
            "there is a spindle behind them."
            + (" The product strings above are the better evidence." if named else ""),
        )

    if facts.form_factor in (0x01, 0x02, 0x03):
        implies = UsbClass.HUSB if facts.rotation_rate not in (1,) else UsbClass.SUSB
        fire(
            "VPD 0xB1 nominal form factor",
            FORM_FACTORS[facts.form_factor],
            {implies: MEDIUM},
            "A declared drive form factor means a drive, not raw flash.",
        )

    # -- capacity -----------------------------------------------------------

    if facts.size_bytes:
        if facts.size_bytes >= 240 * _GB:
            fire(
                "capacity",
                _human_capacity(facts.size_bytes),
                {UsbClass.SUSB: MEDIUM, UsbClass.HUSB: MEDIUM, UsbClass.NUSB: -WEAK},
                "Large enough that an enclosure is far more likely than a stick.",
            )
        elif facts.size_bytes <= 128 * _GB:
            fire(
                "capacity",
                _human_capacity(facts.size_bytes),
                {UsbClass.NUSB: MEDIUM},
                "Squarely in commodity flash-drive territory.",
            )

    if facts.driver == "uas":
        fire(
            "kernel driver",
            "uas",
            {UsbClass.SUSB: WEAK, UsbClass.HUSB: WEAK},
            "Corroborates the UAS protocol reading.",
        )
    elif facts.driver == "usb-storage":
        fire(
            "kernel driver",
            "usb-storage",
            {UsbClass.NUSB: WEAK},
            "The generic Bulk-Only driver.",
        )

    return _tally(evidence, facts, forced=False)


def _tally(evidence: list[Evidence], facts: StorageFacts, forced: bool) -> Verdict:
    scores: dict[UsbClass, int] = {c: 0 for c in UsbClass if c is not UsbClass.UNKNOWN}
    for item in evidence:
        for cls, value in item.scores.items():
            scores[cls] = scores.get(cls, 0) + value

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    margin = best_score - second_score

    if best_score <= 0:
        return Verdict(UsbClass.UNKNOWN, Confidence.LOW, scores, evidence, facts, 0)

    if forced:
        confidence = Confidence.CERTAIN
    elif margin >= 40:
        confidence = Confidence.HIGH
    elif margin >= 15:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.LOW

    return Verdict(best, confidence, scores, evidence, facts, margin)


# --------------------------------------------------------------------------
# reading the facts off real hardware
# --------------------------------------------------------------------------

USB_ROOT = Path("/sys/bus/usb/devices")
BLOCK_ROOT = Path("/sys/block")


def gather_facts(usb_address: str = "", block_name: str = "") -> StorageFacts:
    """Read every signature for one device. Either identifier will do —
    we resolve the other by walking sysfs."""
    facts = StorageFacts(usb_address=usb_address, block_name=block_name)

    if usb_address and not block_name:
        block_name = _block_for_usb(usb_address)
        facts.block_name = block_name
    if block_name and not usb_address:
        usb_address = _usb_for_block(block_name)
        facts.usb_address = usb_address

    if usb_address:
        _read_usb_interface(facts, usb_address)
    if block_name:
        _read_block(facts, block_name)
    return facts


def _read_usb_interface(facts: StorageFacts, address: str) -> None:
    """Find the mass-storage interface (bInterfaceClass 0x08) on this device."""
    try:
        names = sorted(p for p in USB_ROOT.iterdir() if p.name.startswith(address + ":"))
    except OSError:
        return
    for iface in names:
        if read_hex(iface / "bInterfaceClass") != 0x08:
            continue
        facts.interface_subclass = read_hex(iface / "bInterfaceSubClass")
        facts.interface_protocol = read_hex(iface / "bInterfaceProtocol")
        link = iface / "driver"
        if link.exists():
            try:
                facts.driver = link.resolve().name
            except OSError:
                pass
        break
    dev = USB_ROOT / address
    facts.usb_vendor = read_text(dev / "manufacturer")
    facts.usb_product = read_text(dev / "product")
    facts.vendor = facts.vendor or facts.usb_vendor
    facts.model = facts.model or facts.usb_product


def _read_block(facts: StorageFacts, name: str) -> None:
    base = BLOCK_ROOT / name
    device = base / "device"

    facts.peripheral_type = read_int(device / "type")
    facts.scsi_level = read_int(device / "scsi_level")
    facts.scsi_vendor = read_text(device / "vendor")
    facts.scsi_model = read_text(device / "model")
    facts.vendor = facts.vendor or facts.scsi_vendor
    facts.model = facts.model or facts.scsi_model

    sectors = read_int(base / "size")
    logical = read_int(base / "queue/logical_block_size") or 512
    if sectors is not None:
        # `size` is always in 512-byte units regardless of the real block size.
        facts.size_bytes = sectors * 512
    rot = read_int(base / "queue/rotational")
    if rot is not None:
        facts.rotational = bool(rot)
    rem = read_int(base / "removable")
    if rem is not None:
        facts.block_removable = bool(rem)

    inquiry = _read_bytes(device / "inquiry")
    if inquiry and len(inquiry) >= 2:
        facts.peripheral_type = inquiry[0] & 0x1F
        facts.removable_medium = bool(inquiry[1] & 0x80)
    elif facts.block_removable is not None:
        facts.removable_medium = facts.block_removable

    vpd = _read_bytes(device / "vpd_pgb1")
    # Layout: [0] peripheral type, [1] page code 0xB1, [2:4] length,
    #         [4:6] medium rotation rate, [7] low nibble = form factor.
    if vpd and len(vpd) >= 8 and vpd[1] == 0xB1:
        facts.has_vpd_b1 = True
        facts.rotation_rate = (vpd[4] << 8) | vpd[5]
        facts.form_factor = vpd[7] & 0x0F


def _read_bytes(path: Path) -> bytes:
    """sysfs binary attributes report size 0, so read() rather than stat()."""
    try:
        with open(path, "rb") as fh:
            return fh.read(96)
    except OSError:
        return b""


def _block_for_usb(address: str) -> str:
    """Find the block device that arrived on this USB address."""
    for base in _iter_block():
        if _usb_for_block(base.name) == address:
            return base.name
    return ""


def _usb_for_block(name: str) -> str:
    try:
        real = str((BLOCK_ROOT / name).resolve())
    except OSError:
        return ""
    return usb_address_from_path(real)


def _iter_block():
    try:
        return sorted(p for p in BLOCK_ROOT.iterdir() if not p.name.startswith("loop"))
    except OSError:
        return []


def _human_capacity(size: int) -> str:
    for div, unit in ((1024 ** 4, "TB"), (1024 ** 3, "GB"), (1024 ** 2, "MB"), (1024, "KB")):
        if size >= div:
            return f"{size / div:.1f} {unit}".replace(".0 ", " ")
    return f"{size} bytes"


# --------------------------------------------------------------------------
# documentation of the rule set, for `updev usb signatures`
# --------------------------------------------------------------------------

def signature_reference() -> list[dict[str, str]]:
    """The rule table, so the classifier can explain itself without source."""
    return [
        {
            "signature": "SCSI peripheral type = 0x05 / 0x07",
            "verdict": "ODD",
            "weight": "decisive",
            "why": "Only optical drives report the CD/DVD device type.",
        },
        {
            "signature": "bInterfaceSubClass = 0x02 (MMC-5/ATAPI)",
            "verdict": "ODD",
            "weight": "decisive",
            "why": "MMC-5 is the optical command set.",
        },
        {
            "signature": "bInterfaceSubClass = 0x04 (UFI)",
            "verdict": "FUSB",
            "weight": "decisive",
            "why": "UFI is literally the USB Floppy Interface spec.",
        },
        {
            "signature": "capacity is an exact floppy geometry",
            "verdict": "FUSB",
            "weight": "decisive",
            "why": "1.44 MB and friends are unique to floppy media.",
        },
        {
            "signature": "VPD 0xB1 rotation rate >= 0x0401",
            "verdict": "HUSB",
            "weight": "decisive",
            "why": "The drive is reporting a real spindle speed in RPM.",
        },
        {
            "signature": "bInterfaceProtocol = 0x00/0x01 (CBI)",
            "verdict": "FUSB",
            "weight": "strong",
            "why": "CBI survives only on full-speed floppy drives.",
        },
        {
            "signature": "bInterfaceProtocol = 0x62 (UAS)",
            "verdict": "SUSB",
            "weight": "strong",
            "why": "UAS requires a real bridge chip — enclosures only.",
        },
        {
            "signature": "RMB = 1 (removable medium)",
            "verdict": "NUSB",
            "weight": "strong",
            "why": "Sticks and floppies say the medium comes out; enclosures don't.",
        },
        {
            "signature": "VPD 0xB1 rotation rate = 0x0001",
            "verdict": "SUSB",
            "weight": "medium",
            "why": "Explicitly non-rotating, and the page's presence implies a real controller.",
        },
        {
            "signature": "VPD 0xB1 absent",
            "verdict": "NUSB",
            "weight": "medium",
            "why": "Commodity sticks skip the optional page.",
        },
        {
            "signature": "capacity >= 240 GB / <= 128 GB",
            "verdict": "SUSB+HUSB / NUSB",
            "weight": "medium",
            "why": "Corroborating only — capacities overlap at the edges.",
        },
        {
            "signature": "block rotational = 1, no VPD",
            "verdict": "HUSB",
            "weight": "medium",
            "why": "Weak: bridges default the flag to 1 regardless of the truth.",
        },
    ]
