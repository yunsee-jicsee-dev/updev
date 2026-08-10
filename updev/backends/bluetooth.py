"""Bluetooth controllers and their paired devices.

sysfs gives us the controller; `bluetoothctl` gives us the pairing list. We
never start a scan here — discovery is an active radio operation with real
side effects, so it lives behind an explicit `updev bt scan`.
"""

from __future__ import annotations

import re

from ..core.model import Device, Kind, Severity, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import glob, have, mac_vendor, read_text, run, run_ok


class BluetoothBackend(Backend):
    name = "bluetooth"
    title = "Bluetooth"
    kinds = (Kind.BLUETOOTH,)

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        if not glob("/sys/class/bluetooth/*"):
            return False, "no Bluetooth controller"
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        devices: list[Device] = []
        blocked = _rfkill_state()

        for path in sorted(glob("/sys/class/bluetooth/hci*")):
            if ":" in path.name:            # hci0:256 style child nodes
                continue
            devices.append(self._controller(path, blocked))

        devices.extend(self._paired())
        return devices

    def _controller(self, path, blocked: dict[str, bool]) -> Device:
        name = path.name
        addr = read_text(path / "address")
        dev = Device(
            uid=f"bt:{name}",
            kind=Kind.BLUETOOTH,
            name=read_text(path / "name") or name,
            status=Status.ONLINE,
            bus="bluetooth",
            address=addr,
            node=str(path),
            parent="host:board",
        )
        dev.detail["address"] = addr
        if addr:
            vendor = mac_vendor(addr)
            if vendor:
                dev.vendor = vendor
        for attr, label in (("hci_revision", "hci revision"), ("manufacturer", "manufacturer")):
            val = read_text(path / attr)
            if val:
                dev.detail[label] = val

        # `bluetoothctl show` takes a controller *address*, not an hci name.
        info = ""
        if have("bluetoothctl"):
            info = run_ok(["bluetoothctl", "show", addr], timeout=5) if addr else ""
            if not info:
                info = run_ok(["bluetoothctl", "show"], timeout=5)
        for key, pattern in (
            ("powered", r"Powered:\s*(\w+)"),
            ("discoverable", r"Discoverable:\s*(\w+)"),
            ("pairable", r"Pairable:\s*(\w+)"),
            ("alias", r"Alias:\s*(.+)"),
        ):
            m = re.search(pattern, info)
            if m:
                dev.detail[key] = m.group(1).strip()

        if blocked.get("bluetooth"):
            dev.status = Status.DISABLED
            dev.issue(
                Severity.WARN, "Bluetooth is soft-blocked by rfkill",
                fix="sudo rfkill unblock bluetooth",
            )
        elif dev.detail.get("powered") == "no":
            dev.status = Status.IDLE
            dev.issue(
                Severity.INFO, "controller is powered off",
                fix="bluetoothctl power on",
            )

        state = dev.detail.get("powered", "?")
        dev.summary = f"{addr} · powered {state}"
        dev.act("scan", "Discover nearby devices", "updev bt scan")
        return dev

    def _paired(self) -> list[Device]:
        if not have("bluetoothctl"):
            return []
        rc, out, _ = run(["bluetoothctl", "devices"], timeout=6)
        if rc != 0:
            return []
        devices = []
        for line in out.splitlines():
            m = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line.strip())
            if not m:
                continue
            addr, label = m.group(1), m.group(2).strip()
            dev = Device(
                uid=f"bt:dev:{addr.replace(':', '')}",
                kind=Kind.BLUETOOTH,
                name=label or addr,
                status=Status.IDLE,
                bus="bluetooth",
                address=addr,
                parent="bt:hci0",
            )
            info = run_ok(["bluetoothctl", "info", addr], timeout=5)
            connected = re.search(r"Connected:\s*yes", info)
            paired = re.search(r"Paired:\s*yes", info)
            trusted = re.search(r"Trusted:\s*yes", info)
            battery = re.search(r"Battery Percentage:.*\((\d+)\)", info)
            icon = re.search(r"Icon:\s*(\S+)", info)
            dev.status = Status.ONLINE if connected else Status.IDLE
            dev.detail["paired"] = bool(paired)
            dev.detail["trusted"] = bool(trusted)
            dev.detail["connected"] = bool(connected)
            if icon:
                dev.detail["type"] = icon.group(1)
            if battery:
                pct = int(battery.group(1))
                dev.metrics["battery_pct"] = float(pct)
                dev.detail["battery"] = f"{pct}%"
                if pct < 20:
                    dev.issue(Severity.INFO, f"battery low ({pct}%)")
            vendor = mac_vendor(addr)
            if vendor:
                dev.vendor = vendor
            bits = [addr]
            if dev.detail.get("type"):
                bits.append(dev.detail["type"])
            bits.append("connected" if connected else "paired" if paired else "known")
            if battery:
                bits.append(f"battery {battery.group(1)}%")
            dev.summary = " · ".join(bits)
            devices.append(dev)
        return devices


def _rfkill_state() -> dict[str, bool]:
    """Which radio types are currently blocked."""
    blocked: dict[str, bool] = {}
    for path in glob("/sys/class/rfkill/rfkill*"):
        rtype = read_text(path / "type")
        soft = read_text(path / "soft") == "1"
        hard = read_text(path / "hard") == "1"
        if rtype:
            blocked[rtype] = blocked.get(rtype, False) or soft or hard
    return blocked
