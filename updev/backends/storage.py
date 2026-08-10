"""Block devices: SD cards, NVMe over the Pi 5's PCIe lane, USB disks.

`lsblk -J` does the enumeration, and we layer on the things that actually bite
people on a Pi: an SD card silently running in a slow mode, a root filesystem
about to fill up, an NVMe drive that negotiated a single PCIe lane, and the
CID/CSD registers that tell you who really made that "SanDisk" card.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..core.model import Device, Kind, Severity, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import (
    glob,
    have,
    human_bytes,
    read_int,
    read_text,
    run,
    usb_address_from_path,
)

_LSBLK_COLUMNS = (
    "NAME,PATH,TYPE,SIZE,MODEL,SERIAL,VENDOR,REV,TRAN,ROTA,HOTPLUG,STATE,"
    "MOUNTPOINT,FSTYPE,FSSIZE,FSUSED,FSAVAIL,RO,PHY-SEC"
)

# SD card manufacturer IDs (MID) from the CID register. The brand printed on
# the card and the company that made the silicon are frequently different.
_SD_MANUFACTURERS = {
    0x01: "Panasonic", 0x02: "Toshiba/Kioxia", 0x03: "SanDisk", 0x06: "Ritek",
    0x09: "ATP", 0x13: "Kingmax", 0x19: "Dynacard", 0x1B: "Samsung",
    0x1D: "AData", 0x27: "Phison", 0x28: "Lexar", 0x31: "Silicon Power",
    0x41: "Kingston", 0x51: "STEC", 0x5D: "Swissbit", 0x6F: "STMicro",
    0x74: "Transcend", 0x76: "Patriot", 0x82: "Sony/Gobe", 0x9C: "Angelbird/Hoodman",
}


def _usb_parent(name: str) -> str:
    """Resolve /sys/block/<name> and pull the USB device address out of the path."""
    if not name:
        return ""
    try:
        real = str(Path(f"/sys/block/{name}").resolve())
    except OSError:
        return ""
    address = usb_address_from_path(real)
    return f"usb:{address}" if address else ""


def _scsi_product(name: str, node: dict) -> str:
    """Reassemble a product name that SCSI INQUIRY split across two fields.

    A USB-SATA bridge has 8 bytes of vendor and 16 of model, and cheerfully
    writes one long string straight across the boundary: "SHGP31-5" + "00GM"
    is really "SHGP31-500GM". A full 8-character vendor with no trailing space
    is the tell.
    """
    vendor = (node.get("vendor") or "").strip()
    model = (node.get("model") or "").strip()
    if not vendor:
        return model
    if not model:
        return vendor
    raw_vendor = read_text(f"/sys/block/{name}/device/vendor")
    if len(raw_vendor) == 8 and not raw_vendor.endswith(" "):
        return f"{vendor}{model}"
    return f"{vendor} {model}"


class StorageBackend(Backend):
    name = "storage"
    title = "Storage"
    kinds = (Kind.STORAGE,)

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        if not have("lsblk"):
            return False, "lsblk not found"
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        rc, out, err = run(["lsblk", "-J", "-b", "-o", _LSBLK_COLUMNS], timeout=8)
        if rc != 0:
            raise RuntimeError(f"lsblk failed: {err or rc}")
        try:
            tree = json.loads(out).get("blockdevices", [])
        except json.JSONDecodeError as e:
            raise RuntimeError(f"could not parse lsblk output: {e}") from e

        devices: list[Device] = []
        for node in tree:
            self._walk(node, parent="host:board", sink=devices)
        return devices

    # ----------------------------------------------------------------------

    def _walk(self, node: dict, parent: str, sink: list[Device]) -> None:
        if parent == "host:board":
            # Hang USB disks off the USB device they actually arrived on, so the
            # tree reads port → bridge → disk → partitions.
            parent = _usb_parent(node.get("name", "")) or parent
        dev = self._device(node, parent)
        sink.append(dev)
        for child in node.get("children") or []:
            self._walk(child, parent=dev.uid, sink=sink)

    def _device(self, node: dict, parent: str) -> Device:
        name = node.get("name", "?")
        dtype = node.get("type", "")
        size = node.get("size") or 0

        dev = Device(
            uid=f"blk:{name}",
            kind=Kind.STORAGE,
            name=name,
            status=Status.ONLINE,
            bus=node.get("tran") or dtype,
            address=name,
            model=_scsi_product(name, node),
            serial=(node.get("serial") or "").strip(),
            node=node.get("path") or f"/dev/{name}",
            parent=parent,
        )
        # Kept out of `vendor` on purpose: the label should stay "sda", which is
        # what you actually type, with the product name in the summary.
        if node.get("vendor"):
            dev.detail["vendor"] = node["vendor"].strip()
        dev.detail["type"] = dtype
        dev.detail["size"] = human_bytes(size)
        dev.metrics["size_bytes"] = float(size)
        for key, label in (
            ("tran", "transport"), ("rev", "firmware"), ("state", "state"),
            ("fstype", "filesystem"), ("mountpoint", "mounted at"),
            ("phy-sec", "sector size"),
        ):
            val = node.get(key)
            if val:
                dev.detail[label] = val
        if node.get("rota") is not None:
            dev.detail["rotational"] = bool(node["rota"])
        if node.get("hotplug"):
            dev.tags.append("hotplug")

        self._filesystem(dev, node)
        self._transport_detail(dev, name, dtype, node)

        if node.get("ro"):
            dev.tags.append("read-only")
            dev.issue(Severity.WARN, "device is read-only")

        if dtype == "loop":
            dev.status = Status.IDLE
            dev.tags.append("loop")
        if dtype == "disk" and not (node.get("children") or node.get("mountpoint")):
            dev.status = Status.IDLE

        dev.summary = self._summary(dev, node, dtype, size)
        if dtype == "disk":
            dev.act("smart", "SMART health", f"sudo smartctl -a {dev.node}")
        return dev

    def _filesystem(self, dev: Device, node: dict) -> None:
        fssize = node.get("fssize")
        fsused = node.get("fsused")
        if not (fssize and fsused):
            return
        pct = fsused / fssize * 100
        dev.metrics["fs_used_pct"] = pct
        dev.metrics["fs_used_bytes"] = float(fsused)
        dev.detail["filesystem_usage"] = (
            f"{human_bytes(fsused)} / {human_bytes(fssize)} ({pct:.0f}%)"
        )
        if node.get("fsavail"):
            dev.detail["free"] = human_bytes(node["fsavail"])
        if pct >= 95:
            dev.status = Status.DEGRADED
            dev.issue(
                Severity.ERROR,
                f"{node.get('mountpoint') or dev.name} is {pct:.0f}% full",
                fix="sudo apt clean && sudo journalctl --vacuum-size=50M",
            )
        elif pct >= 85:
            dev.issue(Severity.WARN, f"{node.get('mountpoint') or dev.name} is {pct:.0f}% full")

    def _transport_detail(self, dev: Device, name: str, dtype: str, node: dict) -> None:
        if dtype != "disk":
            return
        sysdev = Path(f"/sys/block/{name}/device")

        # --- SD / eMMC ----------------------------------------------------
        if name.startswith("mmcblk"):
            dev.tags.append("sd-card")
            cid = read_text(sysdev / "cid")
            if len(cid) >= 32:
                mid = int(cid[0:2], 16)
                dev.detail["cid_manufacturer"] = _SD_MANUFACTURERS.get(mid, f"MID 0x{mid:02X}")
                dev.detail["cid_oem"] = bytes.fromhex(cid[2:6]).decode("ascii", "replace")
                dev.detail["cid_product"] = bytes.fromhex(cid[6:16]).decode("ascii", "replace")
                dev.detail["cid_serial"] = f"0x{cid[18:26]}"
                month = int(cid[29:30], 16) if cid[29:30] else 0
                year = 2000 + int(cid[27:29], 16) if cid[27:29] else 0
                if 1 <= month <= 12 and year > 2000:
                    dev.detail["manufactured"] = f"{year}-{month:02d}"
            for attr, label in (
                ("speed_class", "speed class"), ("name", "product"),
                ("type", "card type"), ("scr", "SCR"),
            ):
                val = read_text(sysdev / attr)
                if val:
                    dev.detail[label] = val
            clock = read_int(Path(f"/sys/kernel/debug/mmc0/ios/clock"))
            if clock:
                dev.detail["bus_clock"] = f"{clock / 1e6:.0f} MHz"

        # --- NVMe ---------------------------------------------------------
        if name.startswith("nvme"):
            dev.tags.append("nvme")
            ctrl = re.sub(r"n\d+$", "", name)
            base = Path(f"/sys/class/nvme/{ctrl}")
            for attr, label in (
                ("model", "model"), ("firmware_rev", "firmware"),
                ("serial", "serial"), ("numa_node", "numa node"),
            ):
                val = read_text(base / attr)
                if val:
                    dev.detail[label] = val
            link = self._pcie_link(base)
            if link:
                dev.detail.update(link)
                if link.get("_lanes") == 1:
                    dev.issue(
                        Severity.INFO,
                        "NVMe is running on a single PCIe lane",
                        doc="Normal for the Pi 5's x1 connector, but if you expected "
                            "x2 check your HAT and dtparam=pciex1_gen.",
                    )
                if link.get("_gen") and link["_gen"] < 3:
                    dev.issue(
                        Severity.INFO,
                        f"PCIe link negotiated Gen{link['_gen']}",
                        fix="Add dtparam=pciex1_gen=3 to /boot/firmware/config.txt",
                        doc="The Pi 5 defaults to Gen2 for signal-integrity reasons; "
                            "Gen3 works with most drives but is officially unsupported.",
                    )
            for hw in glob("/sys/class/hwmon/hwmon*"):
                if read_text(hw / "name") in ("nvme",):
                    t = read_int(hw / "temp1_input")
                    if t:
                        dev.metrics["temp_c"] = t / 1000
                        dev.detail["temperature"] = f"{t / 1000:.1f}°C"
                        if t / 1000 > 70:
                            dev.issue(Severity.WARN, f"NVMe at {t / 1000:.0f}°C — add a heatsink")
                    break

        if node.get("tran") == "usb":
            dev.tags.append("usb-storage")

    @staticmethod
    def _pcie_link(base: Path) -> dict:
        """Read the negotiated PCIe link width/speed for an NVMe controller."""
        try:
            pci = (base / "device").resolve()
        except OSError:
            return {}
        speed = read_text(pci / "current_link_speed")
        width = read_text(pci / "current_link_width")
        if not (speed or width):
            return {}
        out: dict = {}
        if speed:
            out["pcie_speed"] = speed
            m = re.search(r"([\d.]+)\s*GT/s", speed)
            if m:
                gt = float(m.group(1))
                out["_gen"] = {2.5: 1, 5.0: 2, 8.0: 3, 16.0: 4}.get(gt, 0)
        if width:
            out["pcie_width"] = f"x{width}"
            try:
                out["_lanes"] = int(width)
            except ValueError:
                pass
        return out

    @staticmethod
    def _summary(dev: Device, node: dict, dtype: str, size: int) -> str:
        bits = [human_bytes(size), dtype]
        if dev.model:
            bits.append(dev.model)
        if node.get("tran"):
            bits.append(node["tran"].upper())
        if node.get("mountpoint"):
            bits.append(f"→ {node['mountpoint']}")
        if dev.detail.get("filesystem_usage"):
            bits.append(dev.detail["filesystem_usage"])
        elif node.get("fstype"):
            bits.append(node["fstype"])
        if dev.detail.get("pcie_speed"):
            bits.append(f"PCIe {dev.detail.get('pcie_width', '')} {dev.detail['pcie_speed']}")
        return " · ".join(str(b) for b in bits if b)
