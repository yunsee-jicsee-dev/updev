"""Serial ports: USB-serial adapters, the Pi's own UARTs, and Bluetooth rfcomm.

pyserial's `list_ports` already does the tedious part (matching tty nodes back
to their USB parents), so we lean on it and add the Pi-specific context: which
UART is on the header, whether the console is holding it, and whether the
Bluetooth modem has claimed the good one.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core.model import Device, Kind, Severity, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import glob, read_text, readable

try:
    from serial.tools import list_ports
    HAVE_PYSERIAL = True
except ImportError:                                   # pragma: no cover
    list_ports = None                                 # type: ignore[assignment]
    HAVE_PYSERIAL = False


class SerialBackend(Backend):
    name = "serial"
    title = "Serial / UART"
    kinds = (Kind.SERIAL,)

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        devices: list[Device] = []
        seen: set[str] = set()

        if HAVE_PYSERIAL:
            for port in list_ports.comports():
                seen.add(port.device)
                devices.append(self._from_pyserial(port))

        # pyserial skips /dev/ttyAMA* on some versions; pick them up directly.
        for path in glob("/dev/ttyAMA*") + glob("/dev/serial[0-9]"):
            if str(path) in seen:
                continue
            seen.add(str(path))
            devices.append(self._from_node(str(path)))

        self._annotate_pi_uarts(devices)
        return devices

    # ----------------------------------------------------------------------

    def _from_pyserial(self, port) -> Device:
        dev = Device(
            uid=f"serial:{Path(port.device).name}",
            kind=Kind.SERIAL,
            name=Path(port.device).name,
            status=Status.IDLE,
            bus="serial",
            address=Path(port.device).name,
            node=port.device,
            vendor=(port.manufacturer or "").strip(),
            model=(port.product or "").strip(),
            serial=(port.serial_number or "").strip(),
        )
        if port.vid is not None:
            dev.detail["usb_id"] = f"{port.vid:04x}:{port.pid:04x}"
            dev.parent = self._usb_parent(port)
            dev.tags.append("usb-serial")
        if port.description and port.description != "n/a":
            dev.detail["description"] = port.description
        if port.hwid:
            dev.detail["hwid"] = port.hwid
        dev.driver = self._driver(port.device)
        dev.status = Status.ONLINE if readable(port.device) else Status.IDLE
        # pyserial fills `description` with the port name when it has nothing
        # better, which makes for a summary that just repeats the device column.
        description = dev.detail.get("description", "")
        if description in ("", "n/a", dev.name):
            description = "on-board UART" if port.vid is None else ""
        bits = [b for b in (description, dev.driver) if b]
        dev.summary = " · ".join(bits) or dev.label
        dev.act("open", "Open a terminal on this port", f"updev serial open {dev.name}")
        return dev

    def _from_node(self, node: str) -> Device:
        name = Path(node).name
        dev = Device(
            uid=f"serial:{name}",
            kind=Kind.SERIAL,
            name=name,
            status=Status.ONLINE if readable(node) else Status.IDLE,
            bus="serial",
            address=name,
            node=node,
            parent="host:board",
        )
        dev.driver = self._driver(node)
        bits = ["on-board UART"]
        if dev.driver:
            bits.append(dev.driver)
        dev.summary = " · ".join(bits)
        dev.act("open", "Open a terminal on this port", f"updev serial open {name}")
        return dev

    @staticmethod
    def _driver(node: str) -> str:
        name = Path(node).name
        drv = Path(f"/sys/class/tty/{name}/device/driver")
        if drv.exists():
            try:
                return drv.resolve().name
            except OSError:
                return ""
        return read_text(f"/sys/class/tty/{name}/device/driver_override") or ""

    @staticmethod
    def _usb_parent(port) -> str | None:
        """Map a tty back to the sysfs USB device uid the USB backend produced."""
        location = getattr(port, "location", None)
        if location:
            return f"usb:{location.split(':')[0]}"
        m = re.search(r"(\d+-[\d.]+)", getattr(port, "hwid", "") or "")
        return f"usb:{m.group(1)}" if m else None

    def _annotate_pi_uarts(self, devices: list[Device]) -> None:
        """Explain the serial0/serial1 aliases and the console-vs-header conflict."""
        aliases: dict[str, str] = {}
        for alias in ("serial0", "serial1"):
            link = Path(f"/dev/{alias}")
            if link.is_symlink():
                try:
                    aliases[link.resolve().name] = alias
                except OSError:
                    pass

        cmdline = read_text("/boot/firmware/cmdline.txt") or read_text("/proc/cmdline")
        console_on = set(re.findall(r"console=(tty\w+)", cmdline))

        for dev in devices:
            alias = aliases.get(dev.name)
            if alias:
                dev.detail["alias"] = f"/dev/{alias}"
                if alias == "serial0":
                    dev.detail["role"] = "primary UART — GPIO14 (pin 8) / GPIO15 (pin 10)"
                    dev.tags.append("header")
                else:
                    dev.detail["role"] = "secondary UART"
            if dev.name in console_on:
                dev.tags.append("console")
                dev.detail["kernel_console"] = "yes"
                dev.issue(
                    Severity.INFO,
                    "the kernel serial console is attached to this port",
                    fix="sudo raspi-config nonint do_serial_cons 1   # free it for your own use",
                    doc="You can still open it, but you'll see boot messages and a "
                        "login prompt fighting your data.",
                )
