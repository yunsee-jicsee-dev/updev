"""Displays: DRM connectors, and the monitor identity parsed out of EDID.

Worth being precise about how a portable USB-C monitor actually attaches,
because the obvious guess is wrong on a Raspberry Pi:

  * **DisplayPort alt mode over USB-C** — the monitor never appears as a USB
    device at all; the connector carries DisplayPort lanes directly. The Pi 5's
    USB-C port is *power only* and has no alt mode, so this never happens here.
    A device that wanted alt mode and didn't get it announces itself as a USB
    Billboard, which `usbrole.py` recognises and explains.
  * **HDMI** — what a portable monitor on a Pi 5 is really using, whether or
    not the cable ends in USB-C. It shows up here, as a DRM connector.
  * **DisplayLink** — video encoded over USB 3. That *is* a USB device, and
    `usbrole.py` flags it from the vendor id.

So the monitor lives in DRM, and this backend reads it: connection state,
negotiated mode, physical size, and the manufacturer/model/serial that EDID
carries.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core.model import Device, Kind, Severity, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import glob, read_int, read_text

DRM_ROOT = Path("/sys/class/drm")

#: Connector name prefix -> what it physically is.
_CONNECTOR_KINDS: dict[str, str] = {
    "HDMI-A": "HDMI",
    "HDMI-B": "HDMI",
    "DP": "DisplayPort",
    "eDP": "embedded DisplayPort",
    "DSI": "MIPI DSI (ribbon)",
    "VGA": "VGA",
    "DVI-D": "DVI",
    "DVI-I": "DVI",
    "Composite": "composite video",
    "Writeback": "writeback (virtual)",
    "Virtual": "virtual",
    "USB": "USB (DisplayLink)",
}


class DisplayBackend(Backend):
    name = "display"
    title = "Display"
    kinds = (Kind.DISPLAY,)

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        if not DRM_ROOT.is_dir():
            return False, "no DRM subsystem"
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        devices: list[Device] = []
        for path in sorted(glob("/sys/class/drm/card*-*")):
            if not (path / "status").exists():
                continue
            dev = self._connector(path, ctx)
            if dev:
                devices.append(dev)
        return devices

    def _connector(self, path: Path, ctx: ProbeContext) -> Device | None:
        name = path.name                       # e.g. "card1-HDMI-A-1"
        connector = name.split("-", 1)[1] if "-" in name else name
        status = read_text(path / "status")
        kind = _connector_kind(connector)

        # Writeback connectors are internal compositor plumbing, not outputs.
        if kind.startswith("writeback") and not ctx.deep:
            return None

        dev = Device(
            uid=f"display:{connector}",
            kind=Kind.DISPLAY,
            name=connector,
            bus="drm",
            address=connector,
            node=str(path),
            parent="host:board",
        )
        dev.detail["connector_type"] = kind
        dev.detail["status"] = status
        dev.detail["enabled"] = read_text(path / "enabled")

        if status == "connected":
            dev.status = Status.ONLINE
        elif status == "disconnected":
            dev.status = Status.IDLE
        else:
            dev.status = Status.UNKNOWN

        modes = _modes(path)
        if modes:
            dev.detail["current_best_mode"] = modes[0]
            dev.detail["modes_available"] = len(modes)
            dev.detail["modes"] = modes[:12]
            width, height = _parse_mode(modes[0])
            if width:
                dev.metrics["width"] = float(width)
                dev.metrics["height"] = float(height)
                dev.metrics["pixels"] = float(width * height)

        edid = _read_edid(path)
        if edid:
            info = parse_edid(edid)
            dev.detail.update({k: v for k, v in info.items() if not k.startswith("_")})
            dev.vendor = info.get("manufacturer_name") or info.get("manufacturer", "")
            dev.model = info.get("monitor_name", "")
            dev.serial = info.get("serial", "")
            if info.get("_diagonal_in"):
                dev.metrics["diagonal_in"] = info["_diagonal_in"]

        dev.summary = self._summary(dev, status, kind, modes)

        if status == "connected" and not modes:
            dev.status = Status.DEGRADED
            dev.issue(
                Severity.WARN,
                "connected but reporting no modes",
                doc="Usually a marginal HDMI cable, or a display that answered "
                    "hot-plug detect without delivering a valid EDID.",
            )
        if status == "connected" and not edid:
            dev.issue(
                Severity.INFO,
                "no EDID could be read",
                doc="The display is detected but isn't identifying itself. Some "
                    "HDMI switches and long cables drop the DDC channel while "
                    "still passing video.",
            )
        if status == "connected":
            dev.act("modes", "All supported modes", f"updev show display:{connector}")
        return dev

    @staticmethod
    def _summary(dev: Device, status: str, kind: str, modes: list[str]) -> str:
        if status != "connected":
            return f"{kind} · {status}"
        bits = [kind]
        name = dev.detail.get("monitor_name") or dev.vendor
        if name:
            bits.append(name)
        if modes:
            bits.append(modes[0])
        size = dev.detail.get("screen_size")
        if size:
            bits.append(size)
        return " · ".join(bits)


# --------------------------------------------------------------------------
# EDID
# --------------------------------------------------------------------------

def _read_edid(path: Path) -> bytes:
    """sysfs binary attributes report size 0, so read rather than stat."""
    try:
        with open(path / "edid", "rb") as fh:
            return fh.read(512)
    except OSError:
        return b""


def parse_edid(edid: bytes) -> dict:
    """Pull the identity fields out of an EDID 1.x block.

    Layout used here (all offsets from the EDID spec):
      8-9    manufacturer, three 5-bit letters packed big-endian
      10-11  product code, little-endian
      12-15  serial number, little-endian
      16     week of manufacture, 17: year - 1990
      18-19  EDID version.revision
      21-22  physical size in cm
      54,72,90,108  18-byte descriptors; 0xFC is the monitor name
    """
    out: dict = {}
    if len(edid) < 128:
        return out
    if edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        out["edid"] = "header magic missing — not a valid EDID block"
        return out

    packed = (edid[8] << 8) | edid[9]
    letters = "".join(chr(((packed >> shift) & 0x1F) + 64) for shift in (10, 5, 0))
    out["manufacturer"] = letters
    out["manufacturer_name"] = PNP_VENDORS.get(letters, letters)
    out["product_code"] = f"0x{edid[10] | (edid[11] << 8):04x}"

    serial = int.from_bytes(edid[12:16], "little")
    if serial:
        out["serial_number"] = str(serial)
    week, year = edid[16], edid[17]
    if year:
        made = f"{1990 + year}"
        if 1 <= week <= 53:
            made += f" week {week}"
        out["manufactured"] = made
    out["edid_version"] = f"{edid[18]}.{edid[19]}"

    width_cm, height_cm = edid[21], edid[22]
    if width_cm and height_cm:
        out["screen_size"] = f"{width_cm}×{height_cm} cm"
        diagonal = ((width_cm ** 2 + height_cm ** 2) ** 0.5) / 2.54
        out["screen_diagonal"] = f'{diagonal:.1f}"'
        out["_diagonal_in"] = round(diagonal, 1)

    for offset in (54, 72, 90, 108):
        block = edid[offset:offset + 18]
        if len(block) < 18 or block[0:3] != b"\x00\x00\x00":
            continue
        tag = block[3]
        text = block[5:].split(b"\n")[0].decode("ascii", "replace").strip()
        if tag == 0xFC and text:
            out["monitor_name"] = text
        elif tag == 0xFF and text:
            out["serial"] = text
        elif tag == 0xFE and text:
            out.setdefault("edid_text", []).append(text)

    out["extension_blocks"] = edid[126]
    return out


#: PNP manufacturer ids, limited to the ones you actually meet on a desk.
PNP_VENDORS: dict[str, str] = {
    "SAM": "Samsung", "LGD": "LG Display", "GSM": "LG Electronics",
    "AUO": "AU Optronics", "BOE": "BOE", "CMN": "Chi Mei / Innolux",
    "DEL": "Dell", "ACI": "Asus", "ACR": "Acer", "AOC": "AOC",
    "BNQ": "BenQ", "HWP": "HP", "LEN": "Lenovo", "PHL": "Philips",
    "VSC": "ViewSonic", "MSI": "MSI", "GBT": "Gigabyte", "HPN": "HP",
    "APP": "Apple", "SHP": "Sharp", "SNY": "Sony", "TSB": "Toshiba",
    "NEC": "NEC", "IVM": "Iiyama", "EIZ": "EIZO", "HIT": "Hitachi",
    "CTX": "CTX", "ENC": "Eizo Nanao", "RTK": "Realtek (adapter)",
    "LNX": "Linux (virtual)", "BRQ": "Braun", "KTC": "KTC",
}


def _connector_kind(connector: str) -> str:
    for prefix, label in _CONNECTOR_KINDS.items():
        if connector.startswith(prefix):
            return label
    return connector.rsplit("-", 1)[0]


def _modes(path: Path) -> list[str]:
    raw = read_text(path / "modes")
    seen: list[str] = []
    for mode in raw.split():
        if mode not in seen:
            seen.append(mode)
    return seen


def _parse_mode(mode: str) -> tuple[int, int]:
    m = re.match(r"(\d+)x(\d+)", mode)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
