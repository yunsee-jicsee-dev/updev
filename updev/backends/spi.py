"""SPI controllers and chip-selects.

SPI has no discovery protocol — you cannot ask a bus what's on it. So the
useful thing a device manager can do here is report the *configuration*
accurately (which controllers exist, which CS lines are exposed, what mode
and clock each one is set to) and tell you when the bus isn't enabled.

`updev spi test <node>` does the one real check available: a MOSI↔MISO
loopback, which proves the controller, the driver and the pin muxing all work.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core.model import Device, Kind, Severity, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import glob, read_text, readable, writable

try:
    import spidev
    HAVE_SPIDEV = True
except ImportError:                                   # pragma: no cover
    spidev = None                                     # type: ignore[assignment]
    HAVE_SPIDEV = False

_MODE_NAMES = {
    0: "mode 0 (CPOL=0, CPHA=0)",
    1: "mode 1 (CPOL=0, CPHA=1)",
    2: "mode 2 (CPOL=1, CPHA=0)",
    3: "mode 3 (CPOL=1, CPHA=1)",
}

# Which header pins each Pi SPI bus lands on — the thing you actually need
# when you're holding a jumper wire.
_PINOUT = {
    0: "MOSI=19, MISO=21, SCLK=23, CE0=24, CE1=26",
    1: "MOSI=38, MISO=35, SCLK=40, CE0=12, CE1=11, CE2=36",
}


class SpiBackend(Backend):
    name = "spi"
    title = "SPI"
    kinds = (Kind.SPI,)

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        devices: list[Device] = []
        nodes = sorted(
            (int(m.group(1)), int(m.group(2)), p)
            for p in glob("/dev/spidev*")
            if (m := re.search(r"spidev(\d+)\.(\d+)$", str(p)))
        )

        buses = sorted({bus for bus, _, _ in nodes})
        for bus in buses:
            devices.append(self._controller(bus, [n for n in nodes if n[0] == bus]))
        for bus, cs, path in nodes:
            devices.append(self._chipselect(bus, cs, path, ctx))

        devices.extend(self._kernel_bound())
        if not nodes:
            devices.insert(0, self._disabled())
        return devices

    # ----------------------------------------------------------------------

    def _controller(self, bus: int, nodes: list[tuple[int, int, Path]]) -> Device:
        master = Path(f"/sys/class/spi_master/spi{bus}")
        dev = Device(
            uid=f"spi:{bus}",
            kind=Kind.SPI,
            name=f"SPI controller {bus}",
            status=Status.ONLINE,
            bus=f"spi{bus}",
            address=str(bus),
            parent="host:board",
        )
        dev.detail["chip_selects"] = [f"spidev{b}.{c}" for b, c, _ in nodes]
        if bus in _PINOUT:
            dev.detail["header_pins"] = _PINOUT[bus]
        if master.is_dir():
            dev.driver = read_text(master / "device/driver/module") or ""
            uevent = read_text(master / "device/uevent")
            m = re.search(r"OF_COMPATIBLE_0=(\S+)", uevent)
            if m:
                dev.detail["compatible"] = m.group(1)
        dev.summary = f"{len(nodes)} chip-select(s): " + ", ".join(
            f"CE{c}" for _, c, _ in nodes
        )
        return dev

    def _chipselect(self, bus: int, cs: int, path: Path, ctx: ProbeContext) -> Device:
        dev = Device(
            uid=f"spi:{bus}.{cs}",
            kind=Kind.SPI,
            name=f"spidev{bus}.{cs}",
            status=Status.IDLE,
            bus=f"spi{bus}",
            address=f"{bus}.{cs}",
            node=str(path),
            parent=f"spi:{bus}",
        )
        dev.detail["chip_select"] = f"CE{cs}"
        dev.detail["writable"] = writable(path)

        if not readable(path):
            dev.issue(
                Severity.INFO,
                "no access to the device node",
                fix="sudo usermod -aG spi $USER   # then log out and back in",
            )
            dev.summary = "present (no access)"
            return dev

        if HAVE_SPIDEV:
            # Opening spidev is read-only in effect: it configures nothing until
            # you transfer, and we restore nothing because we change nothing.
            try:
                spi = spidev.SpiDev()
                spi.open(bus, cs)
                try:
                    mode = spi.mode
                    dev.detail["mode"] = _MODE_NAMES.get(mode, f"mode {mode}")
                    dev.detail["max_speed"] = f"{spi.max_speed_hz / 1e6:.3f} MHz"
                    dev.detail["bits_per_word"] = spi.bits_per_word
                    dev.detail["lsb_first"] = bool(spi.lsbfirst)
                    dev.metrics["max_speed_hz"] = float(spi.max_speed_hz)
                    dev.status = Status.ONLINE
                finally:
                    spi.close()
            except OSError as e:
                dev.status = Status.DEGRADED
                dev.issue(Severity.WARN, f"could not open spidev: {e}")
        else:
            dev.issue(Severity.INFO, "python3-spidev not installed — no mode/clock detail")

        dev.summary = " · ".join(
            b for b in (dev.detail.get("mode", ""), dev.detail.get("max_speed", "")) if b
        ) or "present"
        dev.act("test", "MOSI↔MISO loopback test", f"updev spi test {bus}.{cs}")
        dev.act("xfer", "Send bytes and read the response", f"updev spi xfer {bus}.{cs} 0x9f")
        return dev

    def _kernel_bound(self) -> list[Device]:
        """Devices the device tree already attached to a bus (displays, CAN, ...)."""
        out = []
        for entry in glob("/sys/bus/spi/devices/spi*"):
            m = re.match(r"spi(\d+)\.(\d+)$", entry.name)
            if not m:
                continue
            driver = ""
            link = entry / "driver"
            if link.exists():
                driver = Path(str(link)).resolve().name
            name = read_text(entry / "modalias") or entry.name
            if not driver:
                continue        # bare spidev, already covered above
            bus, cs = m.group(1), m.group(2)
            dev = Device(
                uid=f"spi:{bus}.{cs}:{driver}",
                kind=Kind.SPI,
                name=driver,
                status=Status.ONLINE,
                bus=f"spi{bus}",
                address=f"{bus}.{cs}",
                driver=driver,
                parent=f"spi:{bus}",
                summary=f"kernel driver bound ({name})",
            )
            dev.tags.append("driver-bound")
            out.append(dev)
        return out

    def _disabled(self) -> Device:
        """No /dev/spidev* at all — explain precisely why and how to fix it."""
        dev = Device(
            uid="spi:header",
            kind=Kind.SPI,
            name="40-pin SPI",
            status=Status.DISABLED,
            bus="spi",
            parent="host:board",
            summary="not enabled — no /dev/spidev* nodes",
        )
        dev.detail["header_pins"] = _PINOUT[0]

        config = read_text("/boot/firmware/config.txt") or read_text("/boot/config.txt")
        blockers = []
        if re.search(r"^\s*dtparam=spi=on", config, re.M):
            blockers.append("dtparam=spi=on is set but no nodes appeared — check dmesg")
        for m in re.finditer(r"^\s*dtoverlay=(nospi\S*)", config, re.M):
            blockers.append(f"{m.group(1)} overlay is disabling an SPI controller")
        if blockers:
            dev.detail["config_notes"] = blockers

        dev.issue(
            Severity.WARN,
            "SPI is not enabled",
            fix="sudo raspi-config nonint do_spi 0 && sudo reboot"
                "   # or add dtparam=spi=on to /boot/firmware/config.txt",
            doc="Without it, GPIO 7-11 stay general-purpose and nothing you wire "
                "to the SPI pins can be reached.",
        )
        if any("nospi" in b for b in blockers):
            dev.issue(
                Severity.INFO,
                "a `nospi*` overlay is active in config.txt",
                doc="Some HATs and the Pi 5 default config disable spare SPI "
                    "controllers to free their pins. Remove the overlay line if "
                    "you need that controller.",
            )
        return dev
