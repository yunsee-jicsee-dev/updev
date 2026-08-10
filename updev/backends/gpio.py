"""GPIO chips and the 40-pin header.

Two sources, merged. `lgpio` reads the kernel's chardev interface, which knows
how many lines each chip has and — the genuinely useful part — which lines are
currently *claimed* and by whom. `pinctrl` (Pi-specific) knows the alt-function
mux, pull direction and live level of each header pin, which the chardev
interface deliberately doesn't expose.
"""

from __future__ import annotations

import os
import re

from ..core.model import Device, Kind, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import glob, have, read_text, run_ok

try:
    import lgpio
    HAVE_LGPIO = True
except ImportError:                                   # pragma: no cover
    lgpio = None                                      # type: ignore[assignment]
    HAVE_LGPIO = False

# `pinctrl get` prints one line per pin:  " 2: ip    pu | hi // GPIO2 = input"
_PIN_RE = re.compile(
    r"^\s*(?P<gpio>\d+):\s+(?P<func>\S+)\s+(?P<pull>\S+)\s+\|\s+(?P<level>\S+)\s+//\s+(?P<label>.+)$"
)

# Physical header pin -> BCM GPIO, for the 40-pin connector.
_HEADER: dict[int, int] = {
    3: 2, 5: 3, 7: 4, 8: 14, 10: 15, 11: 17, 12: 18, 13: 27, 15: 22, 16: 23,
    18: 24, 19: 10, 21: 9, 22: 25, 23: 11, 24: 8, 26: 7, 27: 0, 28: 1, 29: 5,
    31: 6, 32: 12, 33: 13, 35: 19, 36: 16, 37: 26, 38: 20, 40: 21,
}
_GPIO_TO_PIN = {g: p for p, g in _HEADER.items()}

# Physical pin -> (label, secondary function). Power and ground pins have no
# GPIO number, which is exactly why a bare GPIO list is such a bad map.
PIN_FUNCTIONS: dict[int, tuple[str, str]] = {
    1: ("3V3", "power"),          2: ("5V", "power"),
    3: ("GPIO2", "SDA1 / I2C"),   4: ("5V", "power"),
    5: ("GPIO3", "SCL1 / I2C"),   6: ("GND", "ground"),
    7: ("GPIO4", "GPCLK0 / 1-Wire"), 8: ("GPIO14", "TXD / UART"),
    9: ("GND", "ground"),         10: ("GPIO15", "RXD / UART"),
    11: ("GPIO17", ""),           12: ("GPIO18", "PCM_CLK / PWM0"),
    13: ("GPIO27", ""),           14: ("GND", "ground"),
    15: ("GPIO22", ""),           16: ("GPIO23", ""),
    17: ("3V3", "power"),         18: ("GPIO24", ""),
    19: ("GPIO10", "MOSI / SPI0"), 20: ("GND", "ground"),
    21: ("GPIO9", "MISO / SPI0"), 22: ("GPIO25", ""),
    23: ("GPIO11", "SCLK / SPI0"), 24: ("GPIO8", "CE0 / SPI0"),
    25: ("GND", "ground"),        26: ("GPIO7", "CE1 / SPI0"),
    27: ("GPIO0", "ID_SD / HAT EEPROM"), 28: ("GPIO1", "ID_SC / HAT EEPROM"),
    29: ("GPIO5", ""),            30: ("GND", "ground"),
    31: ("GPIO6", ""),            32: ("GPIO12", "PWM0"),
    33: ("GPIO13", "PWM1"),       34: ("GND", "ground"),
    35: ("GPIO19", "MISO1 / PCM_FS"), 36: ("GPIO16", ""),
    37: ("GPIO26", ""),           38: ("GPIO20", "MOSI1 / PCM_DIN"),
    39: ("GND", "ground"),        40: ("GPIO21", "SCLK1 / PCM_DOUT"),
}


def read_pin_state() -> dict[int, dict[str, str]]:
    """Public helper for the CLI: current mux/pull/level per BCM GPIO."""
    return GpioBackend()._pinctrl()

_FUNC_NAMES = {
    "ip": "input", "op": "output", "no": "none",
    "a0": "alt0", "a1": "alt1", "a2": "alt2", "a3": "alt3",
    "a4": "alt4", "a5": "alt5", "a6": "alt6", "a7": "alt7", "a8": "alt8",
}
_PULL_NAMES = {"pu": "pull-up", "pd": "pull-down", "pn": "none", "--": "none"}


class GpioBackend(Backend):
    name = "gpio"
    title = "GPIO"
    kinds = (Kind.GPIO,)

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        if not glob("/dev/gpiochip*"):
            return False, "no /dev/gpiochip* nodes"
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        devices: list[Device] = []
        chips = self._chips()
        devices.extend(chips)

        pins = self._pinctrl()
        header = self._header(pins, chips)
        if header:
            devices.append(header)

        # Lines a driver has taken are worth showing individually — that's how
        # you find out what's already using the pin you wanted.
        devices.extend(self._claimed_lines(chips))
        devices.extend(self._onewire())
        devices.extend(self._pwm())
        return devices

    # ----------------------------------------------------------------------

    def _chips(self) -> list[Device]:
        out: list[Device] = []
        seen_rdev: set[int] = set()
        for node in sorted(glob("/dev/gpiochip*"), key=_chip_sort):
            m = re.search(r"gpiochip(\d+)$", str(node))
            if not m:
                continue
            num = int(m.group(1))
            # A Pi 5 ships compatibility aliases (/dev/gpiochip4 is the same
            # chardev as gpiochip0). Same device number == same chip.
            try:
                rdev = os.stat(node).st_rdev
            except OSError:
                rdev = -num
            if rdev in seen_rdev:
                continue
            seen_rdev.add(rdev)
            dev = Device(
                uid=f"gpio:chip{num}",
                kind=Kind.GPIO,
                name=f"gpiochip{num}",
                status=Status.ONLINE,
                bus="gpio",
                address=str(num),
                node=str(node),
                parent="host:board",
            )
            lines = None
            label = ""
            if HAVE_LGPIO:
                try:
                    handle = lgpio.gpiochip_open(num)
                except Exception:
                    handle = None
                if handle is not None:
                    try:
                        info = lgpio.gpio_get_chip_info(handle)
                        # (status, lines, name, label)
                        if isinstance(info, (list, tuple)) and len(info) >= 4:
                            lines, label = info[1], str(info[3])
                        dev.detail["claimed"] = self._chip_claims(handle, lines or 0)
                    except Exception:
                        pass
                    finally:
                        try:
                            lgpio.gpiochip_close(handle)
                        except Exception:
                            pass
            if lines:
                dev.detail["lines"] = lines
                dev.metrics["lines"] = float(lines)
            if label:
                dev.model = label
                dev.detail["label"] = label
            role = _chip_role(label, num)
            if role:
                dev.detail["role"] = role
                dev.tags.append(role)
            claimed = dev.detail.get("claimed") or {}
            dev.summary = " · ".join(
                b for b in (
                    label,
                    f"{lines} lines" if lines else "",
                    f"{len(claimed)} in use" if claimed else "",
                    role,
                ) if b
            )
            out.append(dev)
        return out

    @staticmethod
    def _chip_claims(handle, lines: int) -> dict[int, str]:
        """Ask the kernel which lines are held and what the consumer called them."""
        claims: dict[int, str] = {}
        for line in range(min(lines, 64)):
            try:
                info = lgpio.gpio_get_line_info(handle, line)
            except Exception:
                continue
            # (status, offset, flags, name, user)
            if not isinstance(info, (list, tuple)) or len(info) < 5:
                continue
            flags, name, user = info[2], str(info[3]), str(info[4])
            used = bool(flags & 0x01) or bool(user)
            if used and user:
                claims[line] = user or name
        return claims

    # ----------------------------------------------------------------------

    def _pinctrl(self) -> dict[int, dict[str, str]]:
        if not have("pinctrl"):
            return {}
        out = run_ok(["pinctrl", "get"], timeout=6) or run_ok(["pinctrl"], timeout=6)
        pins: dict[int, dict[str, str]] = {}
        for line in out.splitlines():
            m = _PIN_RE.match(line)
            if not m:
                continue
            gpio = int(m.group("gpio"))
            label = m.group("label")
            func_desc = label.split("=", 1)[1].strip() if "=" in label else ""
            pins[gpio] = {
                "function": _FUNC_NAMES.get(m.group("func"), m.group("func")),
                "pull": _PULL_NAMES.get(m.group("pull"), m.group("pull")),
                "level": {"hi": "high", "lo": "low"}.get(m.group("level"), m.group("level")),
                "alt": func_desc,
                "name": label.split("=", 1)[0].strip(),
            }
        return pins

    def _header(self, pins: dict[int, dict[str, str]], chips: list[Device]) -> Device | None:
        if not pins:
            return None
        dev = Device(
            uid="gpio:header",
            kind=Kind.GPIO,
            name="40-pin header",
            status=Status.ONLINE,
            bus="gpio",
            parent="host:board",
        )
        table = {}
        in_use = 0
        for pin, gpio in sorted(_HEADER.items()):
            info = pins.get(gpio)
            if not info:
                continue
            state = info["function"]
            if state not in ("none",):
                in_use += 1
            desc = f"GPIO{gpio:<2} {state}"
            if info["level"] in ("high", "low"):
                desc += f" {info['level']}"
            if info["pull"] != "none":
                desc += f" ({info['pull']})"
            if info["alt"] and info["alt"] not in ("input", "output", "none"):
                desc += f" [{info['alt']}]"
            table[f"pin {pin:>2}"] = desc
        dev.detail["pins"] = table
        dev.detail["configured"] = f"{in_use}/{len(_HEADER)} pins not in the default state"
        dev.metrics["pins_in_use"] = float(in_use)
        dev.summary = f"{in_use} of {len(_HEADER)} GPIO pins configured"
        dev.act("pins", "Full header map", "updev gpio pins")
        return dev

    def _claimed_lines(self, chips: list[Device]) -> list[Device]:
        out: list[Device] = []
        for chip in chips:
            claims: dict[int, str] = chip.detail.get("claimed") or {}
            for line, consumer in sorted(claims.items()):
                dev = Device(
                    uid=f"{chip.uid}:line{line}",
                    kind=Kind.GPIO,
                    name=f"{chip.name} line {line}",
                    status=Status.ONLINE,
                    bus="gpio",
                    address=f"{chip.address}:{line}",
                    parent=chip.uid,
                    driver=consumer,
                    summary=f"claimed by {consumer}",
                )
                dev.tags.append("claimed")
                pin = _GPIO_TO_PIN.get(line)
                if pin and chip.detail.get("role") == "header":
                    dev.detail["header_pin"] = pin
                out.append(dev)
        return out

    # -- adjacent GPIO-ish subsystems --------------------------------------

    def _onewire(self) -> list[Device]:
        out: list[Device] = []
        for slave in glob("/sys/bus/w1/devices/*"):
            if slave.name.startswith("w1_bus_master"):
                continue
            family = slave.name.split("-")[0]
            dev = Device(
                uid=f"w1:{slave.name}",
                kind=Kind.GPIO,
                name=slave.name,
                status=Status.ONLINE,
                bus="1-wire",
                address=slave.name,
                node=str(slave),
                parent="host:board",
            )
            dev.detail["family"] = family
            if family in ("28", "10", "22"):
                dev.model = "DS18B20-family temperature sensor"
                raw = read_text(slave / "temperature")
                if raw.lstrip("-").isdigit():
                    temp = int(raw) / 1000
                    dev.metrics["temp_c"] = temp
                    dev.detail["temperature"] = f"{temp:.2f}°C"
            dev.summary = dev.model or f"1-Wire device (family {family})"
            out.append(dev)
        return out

    def _pwm(self) -> list[Device]:
        out: list[Device] = []
        for chip in glob("/sys/class/pwm/pwmchip*"):
            npwm = read_text(chip / "npwm")
            dev = Device(
                uid=f"pwm:{chip.name}",
                kind=Kind.GPIO,
                name=chip.name,
                status=Status.ONLINE,
                bus="pwm",
                address=chip.name.removeprefix("pwmchip"),
                node=str(chip),
                parent="host:board",
            )
            channels = {}
            for ch in sorted(chip.glob("pwm*")):
                period = read_text(ch / "period")
                duty = read_text(ch / "duty_cycle")
                enabled = read_text(ch / "enable")
                channels[ch.name] = (
                    f"period {period}ns, duty {duty}ns, "
                    f"{'enabled' if enabled == '1' else 'disabled'}"
                )
            if channels:
                dev.detail["channels"] = channels
            dev.detail["npwm"] = npwm
            dev.summary = f"{npwm} channel(s)" + (f", {len(channels)} exported" if channels else "")
            dev.status = Status.ONLINE if channels else Status.IDLE
            out.append(dev)
        return out


def _chip_sort(path) -> tuple[int, str]:
    m = re.search(r"(\d+)$", str(path))
    return (int(m.group(1)) if m else 0, str(path))


def _chip_role(label: str, num: int) -> str:
    low = label.lower()
    if "rp1" in low:
        return "header"           # the RP1 drives the 40-pin connector on a Pi 5
    if "brcmstb" in low:
        return "internal"
    if "bcm2835" in low or "bcm2711" in low:
        return "header"
    return ""
