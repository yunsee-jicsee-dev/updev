"""I2C buses and whatever is sitting on them.

Two jobs. First: report every /dev/i2c-N, and — importantly on a Pi — notice
when the 40-pin bus isn't enabled at all and say exactly how to turn it on.
Second (with --deep): sweep addresses and guess what the chips are.

Address probing is never completely free of risk, so we copy i2cdetect's
"auto" strategy rather than inventing one: SMBus *receive byte* on the ranges
where a write could corrupt something (EEPROMs at 0x30-0x37 and 0x50-0x5F),
and an SMBus *quick write* everywhere else.

Then we do the thing i2cdetect doesn't: check whether the answer is physically
possible, and re-probe when it isn't. See `_scan`.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ..core.model import Device, Kind, Severity, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import glob, read_text, writable

try:
    from smbus2 import SMBus
    HAVE_SMBUS = True
except ImportError:                                   # pragma: no cover
    SMBus = None                                      # type: ignore[assignment]
    HAVE_SMBUS = False

# 7-bit address -> the parts you're most likely to have wired up. Addresses are
# not unique, so these are hints, not identifications.
KNOWN_ADDRESSES: dict[int, str] = {
    0x0D: "QMC5883L magnetometer",
    0x0E: "MAG3110 magnetometer",
    0x10: "VEML7700 / VL6180 light sensor",
    0x13: "VCNL4010 proximity",
    0x18: "MCP9808 temp / LIS3DH accel",
    0x1C: "MMA8451 / LIS3MDL",
    0x1E: "HMC5883L magnetometer",
    0x20: "PCF8574 / MCP23017 GPIO expander",
    0x21: "PCF8574 / MCP23017 GPIO expander",
    0x23: "BH1750 light sensor",
    0x24: "PN532 NFC reader (updev nfc detect)",
    0x27: "PCF8574 LCD backpack",
    0x28: "BNO055 IMU",
    0x29: "VL53L0X ToF / TSL2591",
    0x36: "MAX17048 fuel gauge / AS5600 encoder",
    0x38: "AHT10/AHT20 humidity / VEML6070",
    0x39: "TSL2561 light sensor",
    0x3C: "SSD1306 / SH1106 OLED",
    0x3D: "SSD1306 OLED (alt)",
    0x3F: "PCF8574A LCD backpack",
    0x40: "INA219 current / HTU21D / Si7021 / PCA9685",
    0x44: "SHT31 temp+humidity",
    0x48: "ADS1115 ADC / LM75 temp",
    0x49: "ADS1115 ADC (alt) / TSL2561",
    0x4A: "ADS1115 ADC (alt)",
    0x4B: "ADS1115 ADC (alt)",
    0x50: "24Cxx EEPROM / HAT EEPROM",
    0x51: "24Cxx EEPROM",
    0x53: "ADXL345 accelerometer",
    0x57: "MAX30102 pulse ox / 24Cxx EEPROM",
    0x5A: "MLX90614 IR thermometer / CCS811",
    0x5C: "AM2320 / BH1750 (alt)",
    0x60: "MCP4725 DAC / SI1145 / MPR121",
    0x61: "SCD30 CO2 sensor",
    0x62: "SCD4x CO2 sensor",
    0x68: "DS3231/DS1307 RTC / MPU6050 / ICM20948",
    0x69: "MPU6050 (alt) / ICM20948 (alt)",
    0x6A: "LSM6DS3 IMU",
    0x6B: "LSM6DSOX IMU",
    0x70: "TCA9548A I2C mux / HT16K33 display",
    0x76: "BME280 / BMP280 / MS5611",
    0x77: "BME280 / BMP280 (alt) / BMP180",
}

# A display's DDC channel is an I2C bus with a fixed, standardised layout.
# On these buses the generic guesses above are wrong, so they get their own map.
DDC_ADDRESSES: dict[int, str] = {
    0x30: "E-DDC segment pointer",
    0x37: "DDC/CI display control",
    0x3A: "HDCP receiver",
    0x50: "monitor EDID",
    0x54: "EDID extension block / SCDC",
}

# Device-tree node name -> what that internal bus is actually wired to.
# The Pi 5 routes both HDMI ports' DDC channels through the SoC's own I2C blocks.
_DT_BUS_ROLES: dict[str, str] = {
    "i2c@7d508200": "HDMI0 DDC",
    "i2c@7d508280": "HDMI1 DDC",
    "i2c@7d504000": "CSI/DSI",
}

# Ranges where i2cdetect uses a read instead of a write, because a stray write
# to an EEPROM is how you brick a HAT.
_READ_RANGES = ((0x30, 0x37), (0x50, 0x5F))
# Reserved 7-bit addresses that nothing should answer on.
_SCAN_RANGE = range(0x03, 0x78)
# More responders than this on one bus means the controller is ACKing blindly.
# Real buses run out of addresses long before here — 8 chips is already a lot.
_IMPLAUSIBLE_COUNT = 24
# Wall-clock budget for one sweep of one bus. A bus with devices on it finishes
# in milliseconds; only a floating bus (nothing pulling the lines up) times out
# per address, and there is nothing to find there anyway.
_SWEEP_BUDGET = 2.0


@dataclass(slots=True)
class _Sweep:
    """Outcome of one pass over the address space."""

    found: list[int]
    probed: int
    stopped_at: int | None          # set when the time budget ran out

    def truncation(self, budget: float) -> str:
        if self.stopped_at is None:
            return ""
        return (
            f"Stopped at 0x{self.stopped_at:02x} after {budget:g}s — every probe is "
            "hitting a bus timeout, which means nothing is pulling SDA/SCL up. "
            "That is what an empty bus with no device attached looks like."
        )


class I2cBackend(Backend):
    name = "i2c"
    title = "I2C"
    kinds = (Kind.I2C,)

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        return True, ""      # we still want to report "no buses, here's the fix"

    def probe(self, ctx: ProbeContext) -> list[Device]:
        devices: list[Device] = []
        buses = sorted(
            (int(m.group(1)), p)
            for p in glob("/dev/i2c-*")
            if (m := re.search(r"i2c-(\d+)$", str(p)))
        )

        for num, path in buses:
            devices.append(self._bus(num, path, ctx, devices))

        devices.insert(0, self._header_status(buses))
        return devices

    # ----------------------------------------------------------------------

    def _bus(self, num: int, path: Path, ctx: ProbeContext, sink: list[Device]) -> Device:
        label = read_text(f"/sys/class/i2c-dev/i2c-{num}/name") or f"i2c-{num}"
        dev = Device(
            uid=f"i2c:{num}",
            kind=Kind.I2C,
            name=f"i2c-{num}",
            status=Status.ONLINE,
            bus=f"i2c-{num}",
            address=str(num),
            model=label,
            node=str(path),
        )
        dev.detail["adapter"] = label
        dev.detail["writable"] = writable(path)
        role = _bus_role(num, label)
        if role:
            dev.detail["role"] = role
            dev.tags.append(role)
        wired_to = _bus_hardware(num)
        if wired_to:
            dev.detail["wired_to"] = wired_to
            dev.model = f"{label} ({wired_to})"

        if not writable(path):
            dev.issue(
                Severity.INFO,
                "no write access to the bus node",
                fix="sudo usermod -aG i2c $USER   # then log out and back in",
            )

        # Drivers that already claimed an address show up in sysfs, and they're
        # free to find — no bus traffic needed.
        claimed = self._claimed(num)
        if claimed:
            dev.detail["claimed_by_drivers"] = claimed
            for addr, drv in sorted(claimed.items()):
                sink.append(self._chip(num, addr, driver=drv))

        if ctx.deep:
            if not HAVE_SMBUS:
                dev.issue(Severity.WARN, "smbus2 not installed — cannot scan addresses")
            elif not writable(path):
                dev.issue(Severity.WARN, "cannot scan: bus node is not writable")
            else:
                try:
                    found, method, note = self._scan(num)
                except OSError as e:
                    dev.status = Status.DEGRADED
                    dev.issue(Severity.ERROR, f"address scan failed: {e}")
                    found, method, note = [], "", ""
                for addr in found:
                    if addr not in claimed:
                        sink.append(self._chip(num, addr, wired_to=wired_to))
                dev.detail["scanned"] = f"0x{_SCAN_RANGE.start:02x}-0x{_SCAN_RANGE.stop - 1:02x}"
                dev.detail["scan_method"] = method
                dev.detail["responding"] = [f"0x{a:02x}" for a in found] or "none"
                if note:
                    dev.detail["scan_note"] = note
                    headline = (
                        "this bus ACKs addresses that hold no device"
                        if "quick-write probe was ACKed" in note
                        else "the address sweep was cut short"
                    )
                    dev.issue(Severity.INFO, headline, doc=note)
                if not found and not claimed:
                    dev.status = Status.IDLE
                    dev.summary = f"{label} · no devices responding"
                else:
                    dev.summary = f"{label} · {len(set(found) | set(claimed))} device(s)"
        else:
            n = len(claimed)
            dev.status = Status.ONLINE if n else Status.IDLE
            dev.summary = (
                f"{label} · {n} claimed by drivers"
                if n
                else f"{label} · run with --deep to scan addresses"
            )
            dev.act("scan", "Probe every address on this bus", f"updev i2c scan {num}")

        dev.act("dump", f"Read a register", f"updev i2c read {num} 0x?? 0x00")
        return dev

    def _chip(self, bus: int, addr: int, driver: str = "", wired_to: str = "") -> Device:
        # On a display's DDC channel the address map is standardised, so use it
        # instead of guessing from the general-purpose sensor table.
        if "DDC" in wired_to:
            guess = DDC_ADDRESSES.get(addr, "")
        else:
            guess = KNOWN_ADDRESSES.get(addr, "")
        dev = Device(
            uid=f"i2c:{bus}:0x{addr:02x}",
            kind=Kind.I2C,
            name=driver or guess or f"unknown chip @ 0x{addr:02x}",
            status=Status.ONLINE,
            bus=f"i2c-{bus}",
            address=f"0x{addr:02x}",
            driver=driver,
            parent=f"i2c:{bus}",
        )
        dev.detail["address_7bit"] = f"0x{addr:02x}"
        dev.detail["address_8bit_write"] = f"0x{addr << 1:02x}"
        dev.detail["address_8bit_read"] = f"0x{(addr << 1) | 1:02x}"
        if guess:
            dev.detail["likely"] = guess
        if driver:
            dev.detail["kernel_driver"] = driver
            dev.summary = f"bound to {driver}"
            dev.tags.append("driver-bound")
        else:
            dev.summary = f"responding · likely {guess}" if guess else "responding, unidentified"
            dev.tags.append("unclaimed")
        dev.act("read", "Read a register", f"updev i2c read {bus} 0x{addr:02x} 0x00")
        return dev

    @staticmethod
    def _claimed(bus: int) -> dict[int, str]:
        """Addresses already bound to a kernel driver, from sysfs."""
        out: dict[int, str] = {}
        base = Path(f"/sys/bus/i2c/devices")
        if not base.is_dir():
            return out
        for entry in sorted(base.glob(f"{bus}-*")):
            m = re.match(rf"{bus}-([0-9a-f]{{4}})$", entry.name)
            if not m:
                continue
            addr = int(m.group(1), 16)
            driver = ""
            link = entry / "driver"
            if link.exists():
                driver = os.path.basename(os.path.realpath(link))
            out[addr] = driver or read_text(entry / "name") or "bound"
        return out

    @staticmethod
    def _scan(bus: int, budget: float = _SWEEP_BUDGET) -> tuple[list[int], str, str]:
        """Probe the address space. Returns (addresses, method, caveat).

        Starts with i2cdetect's auto strategy, then sanity-checks the result.
        Some controllers — notably the Designware blocks driving the Pi 5's
        internal HDMI/DSI buses — ACK a quick-write at *every* address, so
        plain `i2cdetect` reports 117 imaginary chips. When the result looks
        like that, we re-probe in read mode, which those buses answer honestly.

        Read probes are the expensive ones: with nothing pulling the lines up,
        each dead address costs a full bus timeout (~105ms on a Pi 5), so a
        complete sweep of an unconnected bus would take twelve seconds. Hence
        the wall-clock budget — we stop early and say we stopped, rather than
        wedging a scan on a bus that has nothing on it.
        """
        def sweep(read_only: bool) -> _Sweep:
            found: list[int] = []
            probed = 0
            deadline = time.monotonic() + budget
            with SMBus(bus) as smb:
                for addr in _SCAN_RANGE:
                    if time.monotonic() > deadline:
                        return _Sweep(found, probed, addr)      # where we gave up
                    use_read = read_only or any(lo <= addr <= hi for lo, hi in _READ_RANGES)
                    probed += 1
                    try:
                        if use_read:
                            smb.read_byte(addr)
                        else:
                            # SMBus QUICK: address phase only, no data. Same
                            # test i2cdetect uses in its default mode.
                            smb.write_quick(addr)
                    except OSError:
                        continue                     # ENXIO/EREMOTEIO == nobody home
                    found.append(addr)
            return _Sweep(found, probed, None)

        first = sweep(read_only=False)
        if len(first.found) <= _IMPLAUSIBLE_COUNT:
            return first.found, "quick-write (i2cdetect default)", first.truncation(budget)

        verified = sweep(read_only=True)
        note = (
            f"{len(first.found)} of the {first.probed} addresses probed ACKed a "
            f"quick-write, which no real bus does. Re-probed in read mode: "
            f"{len(verified.found)} of {verified.probed} responded. The controller "
            "ACKs writes regardless of whether anything is there."
        )
        extra = verified.truncation(budget)
        return verified.found, "read-byte (quick-write was unreliable)", (
            f"{note} {extra}" if extra else note
        )

    # ----------------------------------------------------------------------

    def _header_status(self, buses: list[tuple[int, Path]]) -> Device:
        """A synthetic device that answers 'is the 40-pin I2C actually on?'"""
        numbers = {n for n, _ in buses}
        header_bus = next((n for n in (1, 0) if n in numbers), None)

        dev = Device(
            uid="i2c:header",
            kind=Kind.I2C,
            name="40-pin I2C (GPIO2/GPIO3)",
            bus="i2c",
            parent="host:board",
        )
        dev.detail["buses_present"] = sorted(numbers) or "none"
        internal = sorted(n for n in numbers if n >= 10)
        if internal:
            dev.detail["internal_buses"] = internal
            dev.detail["note"] = "i2c-10+ are internal (HDMI/DSI/CSI), not the header"

        if header_bus is not None:
            dev.status = Status.ONLINE
            dev.address = str(header_bus)
            dev.node = f"/dev/i2c-{header_bus}"
            dev.summary = f"enabled on i2c-{header_bus} (pins 3/5)"
        else:
            dev.status = Status.DISABLED
            dev.summary = "not enabled — no bus on the GPIO header"
            dev.issue(
                Severity.WARN,
                "the 40-pin I2C bus is not enabled",
                fix="sudo raspi-config nonint do_i2c 0 && sudo reboot"
                    "   # or add dtparam=i2c_arm=on to /boot/firmware/config.txt",
                doc="Only internal buses (i2c-13/14 for HDMI and DSI) are present, "
                    "so nothing you wire to pins 3 and 5 can be reached.",
            )
        return dev


def _bus_hardware(num: int) -> str:
    """Name what an internal bus is physically wired to, via its device-tree node."""
    try:
        node = Path(f"/sys/class/i2c-dev/i2c-{num}/device/of_node").resolve().name
    except OSError:
        return ""
    return _DT_BUS_ROLES.get(node, "")


def _bus_role(num: int, label: str) -> str:
    """Give internal buses a name so people stop wondering what i2c-13 is."""
    low = label.lower()
    if "designware" in low or "brcmstb" in low:
        if num >= 10:
            return "internal"
    if num in (0,):
        return "camera/display"
    if num in (1,):
        return "header"
    if num >= 10:
        return "internal"
    return ""
