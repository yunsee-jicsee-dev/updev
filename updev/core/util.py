"""Small helpers shared by every backend: sysfs reads, subprocess, id lookups.

Everything here is defensive. A device manager that raises because a sysfs
attribute went missing between `listdir` and `open` is a bad device manager.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable


# --------------------------------------------------------------------------
# filesystem
# --------------------------------------------------------------------------

def read_text(path: str | Path, default: str = "") -> str:
    """Read a file, returning `default` on any failure. sysfs races are normal."""
    try:
        return Path(path).read_text(errors="replace").strip()
    except (OSError, UnicodeError):
        return default


def read_int(path: str | Path, default: int | None = None) -> int | None:
    raw = read_text(path)
    if not raw:
        return default
    try:
        return int(raw, 0)
    except ValueError:
        return default


def read_hex(path: str | Path, default: int | None = None) -> int | None:
    """sysfs writes USB descriptor fields as bare hex with leading zeros ("03"),
    which `int(x, 0)` rejects. Parse those explicitly."""
    raw = read_text(path)
    if not raw:
        return default
    try:
        return int(raw, 16)
    except ValueError:
        return default


def read_float(path: str | Path, default: float | None = None) -> float | None:
    raw = read_text(path)
    try:
        return float(raw)
    except ValueError:
        return default


def glob(pattern: str) -> list[Path]:
    """Sorted glob from / that never raises."""
    try:
        root = Path(pattern[0]) if pattern.startswith("/") else Path(".")
        rel = pattern.lstrip("/") if pattern.startswith("/") else pattern
        return sorted(root.glob(rel))
    except OSError:
        return []


def exists(path: str | Path) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False


def readable(path: str | Path) -> bool:
    return os.access(str(path), os.R_OK)


def writable(path: str | Path) -> bool:
    return os.access(str(path), os.W_OK)


# --------------------------------------------------------------------------
# subprocess
# --------------------------------------------------------------------------

def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def run(
    cmd: list[str] | str,
    timeout: float = 5.0,
    check: bool = False,
) -> tuple[int, str, str]:
    """Run a command. Returns (rc, stdout, stderr); rc 124 on timeout, 127 if missing."""
    shell = isinstance(cmd, str)
    try:
        p = subprocess.run(
            cmd,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", "command not found"
    except OSError as e:
        return 126, "", str(e)
    if check and p.returncode != 0:
        raise RuntimeError(f"{cmd}: {p.stderr.strip()}")
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def run_ok(cmd: list[str] | str, timeout: float = 5.0) -> str:
    """Run and return stdout, or "" if it failed at all."""
    rc, out, _ = run(cmd, timeout=timeout)
    return out if rc == 0 else ""


def is_root() -> bool:
    return os.geteuid() == 0


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

_UNITS = ("B", "K", "M", "G", "T", "P")


def human_bytes(n: float | int | None, precision: int = 1) -> str:
    if n is None:
        return "-"
    n = float(n)
    neg = n < 0
    n = abs(n)
    for i, unit in enumerate(_UNITS):
        if n < 1024 or i == len(_UNITS) - 1:
            s = f"{n:.{0 if unit == 'B' else precision}f}{unit}"
            return f"-{s}" if neg else s
        n /= 1024
    return "-"


def human_hz(hz: float | int | None) -> str:
    if not hz:
        return "-"
    hz = float(hz)
    for div, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if hz >= div:
            return f"{hz / div:.6g}{unit}"
    return f"{hz:.0f}Hz"


def human_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def truncate(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


# --------------------------------------------------------------------------
# hardware id databases (usb.ids / pci.ids ship with Debian)
# --------------------------------------------------------------------------

_ID_PATHS = {
    "usb": ("/usr/share/misc/usb.ids", "/usr/share/hwdata/usb.ids", "/var/lib/usbutils/usb.ids"),
    "pci": ("/usr/share/misc/pci.ids", "/usr/share/hwdata/pci.ids"),
}


@functools.lru_cache(maxsize=4)
def _load_ids(which: str) -> dict[str, tuple[str, dict[str, str]]]:
    """Parse a usb.ids/pci.ids file into {vendor_id: (vendor_name, {dev_id: name})}."""
    path = next((p for p in _ID_PATHS.get(which, ()) if os.path.exists(p)), None)
    if not path:
        return {}
    table: dict[str, tuple[str, dict[str, str]]] = {}
    vendor_id = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip() or line.startswith("#"):
                    continue
                if line.startswith("\t\t"):
                    continue                      # interface / subsystem level
                if line.startswith("\t"):
                    if vendor_id is None:
                        continue
                    dev_id, _, dev_name = line.strip().partition("  ")
                    table[vendor_id][1][dev_id.lower()] = dev_name.strip()
                else:
                    if line[0] in " \t":
                        continue
                    vid, _, vname = line.rstrip("\n").partition("  ")
                    vid = vid.strip().lower()
                    if len(vid) != 4 or not _is_hex(vid):
                        vendor_id = None          # we've hit the class/HID sections
                        continue
                    vendor_id = vid
                    table[vendor_id] = (vname.strip(), {})
    except OSError:
        return {}
    return table


def _is_hex(s: str) -> bool:
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def usb_names(vid: str, pid: str) -> tuple[str, str]:
    """('1a2c', '2d23') -> ('China Resource Semico Co., Ltd', 'Keyboard')."""
    table = _load_ids("usb")
    entry = table.get((vid or "").lower())
    if not entry:
        return "", ""
    return entry[0], entry[1].get((pid or "").lower(), "")


def pci_names(vid: str, pid: str) -> tuple[str, str]:
    table = _load_ids("pci")
    entry = table.get((vid or "").lower().removeprefix("0x"))
    if not entry:
        return "", ""
    return entry[0], entry[1].get((pid or "").lower().removeprefix("0x"), "")


@functools.lru_cache(maxsize=512)
def mac_vendor(mac: str) -> str:
    """OUI -> vendor. Uses systemd-hwdb, which ships the IEEE registry."""
    mac = (mac or "").strip().lower().replace("-", ":")
    if len(mac) < 8:
        return ""
    oui = mac[:8].replace(":", "").upper()
    if not have("systemd-hwdb"):
        return ""
    rc, out, _ = run(["systemd-hwdb", "query", f"OUI:{oui}"], timeout=3)
    if rc != 0:
        return ""
    # hwdb answers with ID_OUI_FROM_DATABASE=<vendor>; other providers use
    # ID_VENDOR_FROM_DATABASE, so accept either.
    for line in out.splitlines():
        key, sep, val = line.partition("=")
        if sep and key.strip() in ("ID_OUI_FROM_DATABASE", "ID_VENDOR_FROM_DATABASE"):
            return val.strip()
    return ""


def is_locally_administered(mac: str) -> bool:
    """Randomised / container MACs have bit 1 of the first octet set."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0b10)
    except (ValueError, IndexError):
        return False


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------

def first(iterable: Iterable, default=None):
    for item in iterable:
        return item
    return default


def parse_kv(text: str, sep: str = ":") -> dict[str, str]:
    """Parse `key: value` blocks (lsblk -P style output, /proc files, ...)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if sep in line:
            k, _, v = line.partition(sep)
            out[k.strip()] = v.strip()
    return out


#: A USB device address as sysfs spells it: "3-1", "1-2.3", "1-2.3.4".
#: Interface directories ("1-2.3:1.0") deliberately don't match.
_USB_ADDR_RE = re.compile(r"^\d+-[\d.]+$")


def usb_address_from_path(path: str | Path) -> str:
    """Deepest USB device address in a sysfs path.

    `.../usb1/1-2/1-2.3/1-2.3:1.0/host2/.../block/sdc` → `1-2.3`.

    Taking the *first* match would return `1-2`, the hub — which is how a
    device plugged into a hub ends up attributed to the hub instead of itself.
    """
    found = ""
    for part in str(path).split("/"):
        if _USB_ADDR_RE.match(part):
            found = part
    return found


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")
