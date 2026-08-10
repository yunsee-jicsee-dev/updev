"""The board itself: SoC, thermals, PMIC rails, memory, firmware, HAT.

On a Raspberry Pi this is the richest backend by far — `vcgencmd` exposes
per-rail voltage and current from the PMIC, which lets us report actual power
draw in watts rather than the usual hand-waving. Everything degrades to plain
/proc and /sys when vcgencmd isn't there, so this still works on any Linux box.
"""

from __future__ import annotations

import os
import platform
import re

from ..core.model import Device, Kind, Severity, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import (
    glob,
    have,
    human_bytes,
    human_duration,
    human_hz,
    read_int,
    read_text,
    run,
    run_ok,
)

# --------------------------------------------------------------------------
# Raspberry Pi revision-code decoding (new-style codes, bit 23 set)
# --------------------------------------------------------------------------

_PROCESSORS = {
    0: "BCM2835", 1: "BCM2836", 2: "BCM2837", 3: "BCM2711", 4: "BCM2712",
}
_MANUFACTURERS = {
    0: "Sony UK", 1: "Egoman", 2: "Embest", 3: "Sony Japan", 4: "Embest", 5: "Stadium",
}
_MEMORY = {0: "256MB", 1: "512MB", 2: "1GB", 3: "2GB", 4: "4GB", 5: "8GB", 6: "16GB"}

# `CPU part` from /proc/cpuinfo -> the marketing name of the core.
_ARM_PARTS = {
    "0xd03": "Cortex-A53", "0xd07": "Cortex-A57", "0xd08": "Cortex-A72",
    "0xd0b": "Cortex-A76", "0xd05": "Cortex-A55", "0xd44": "Cortex-X1",
    "0xc07": "Cortex-A7", "0xb76": "ARM1176JZF-S",
}
_MODELS = {
    0x00: "A", 0x01: "B", 0x02: "A+", 0x03: "B+", 0x04: "2B", 0x06: "CM1",
    0x08: "3B", 0x09: "Zero", 0x0A: "CM3", 0x0C: "Zero W", 0x0D: "3B+",
    0x0E: "3A+", 0x10: "CM3+", 0x11: "4B", 0x12: "Zero 2 W", 0x13: "400",
    0x14: "CM4", 0x15: "CM4S", 0x17: "5", 0x18: "CM5", 0x19: "500",
    0x1A: "CM5 Lite",
}

# `vcgencmd get_throttled` bit meanings. Low bits are live, high bits are sticky.
_THROTTLE_BITS = [
    (0, "under-voltage detected", True),
    (1, "ARM frequency capped", True),
    (2, "currently throttled", True),
    (3, "soft temperature limit active", True),
    (16, "under-voltage has occurred", False),
    (17, "ARM frequency capping has occurred", False),
    (18, "throttling has occurred", False),
    (19, "soft temperature limit has occurred", False),
]

_PMIC_LINE = re.compile(r"^\s*(?P<rail>\S+?)\s+(?P<what>current|volt)\(\d+\)=(?P<val>[\d.]+)")


class HostBackend(Backend):
    name = "host"
    title = "Host / SoC"
    kinds = (Kind.HOST, Kind.SOC, Kind.POWER, Kind.THERMAL)

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        devices: list[Device] = []
        board = self._board()
        devices.append(board)
        devices.append(self._soc(board))
        devices.append(self._memory(board.uid))
        devices.extend(self._thermal(board.uid))
        power = self._power(board.uid)
        if power:
            devices.append(power)
        fan = self._fan(board.uid)
        if fan:
            devices.append(fan)
        hat = self._hat(board.uid)
        if hat:
            devices.append(hat)
        return devices

    # -- board -------------------------------------------------------------

    def _board(self) -> Device:
        model = read_text("/proc/device-tree/model").rstrip("\x00") or platform.machine()
        cpuinfo = read_text("/proc/cpuinfo")
        revision = _cpuinfo_field(cpuinfo, "Revision")
        serial = _cpuinfo_field(cpuinfo, "Serial")

        dev = Device(
            uid="host:board",
            kind=Kind.HOST,
            name=model,
            status=Status.ONLINE,
            vendor="Raspberry Pi" if "Raspberry" in model else "",
            model=model,
            serial=serial,
        )
        dev.detail["kernel"] = platform.release()
        dev.detail["arch"] = platform.machine()
        dev.detail["hostname"] = platform.node()
        dev.detail["os"] = _os_pretty_name()
        dev.detail["python"] = platform.python_version()

        uptime = _uptime()
        if uptime is not None:
            dev.detail["uptime"] = human_duration(uptime)
            dev.metrics["uptime_s"] = uptime

        if revision:
            dev.detail["revision"] = revision
            decoded = _decode_revision(revision)
            if decoded:
                dev.detail.update(decoded)

        # Firmware / bootloader — a stale EEPROM is a real-world Pi 5 footgun.
        if have("vcgencmd"):
            fw = run_ok(["vcgencmd", "version"], timeout=4)
            if fw:
                dev.detail["firmware"] = fw.splitlines()[0]
            bl = run_ok(["vcgencmd", "bootloader_version"], timeout=4)
            if bl:
                lines = [l.strip() for l in bl.splitlines() if l.strip()]
                dev.detail["bootloader"] = lines[0] if lines else ""
            cfg = run_ok(["vcgencmd", "bootloader_config"], timeout=4)
            order = re.search(r"^BOOT_ORDER=(\S+)", cfg, re.M)
            if order:
                dev.detail["boot_order"] = _explain_boot_order(order.group(1))

        if have("rpi-eeprom-update"):
            rc, out, _ = run(["rpi-eeprom-update"], timeout=8)
            if rc == 0 and "UPDATE AVAILABLE" in out.upper():
                dev.issue(
                    Severity.INFO,
                    "bootloader EEPROM update available",
                    fix="sudo rpi-eeprom-update -a && sudo reboot",
                    doc="Newer Pi 5 EEPROMs fix NVMe/USB boot and PCIe quirks.",
                )

        parts = [dev.detail.get("os", ""), f"kernel {platform.release()}"]
        if uptime is not None:
            parts.append(f"up {human_duration(uptime)}")
        dev.summary = " · ".join(p for p in parts if p)
        dev.act("info", "Full board detail", "updev show host:board")
        return dev

    # -- SoC ---------------------------------------------------------------

    def _soc(self, board: Device) -> Device:
        cpuinfo = read_text("/proc/cpuinfo")
        cores = os.cpu_count() or 1
        # arm64 kernels drop the "Hardware" line, so the revision code decoded
        # by _board() is usually the only place the SoC part number appears.
        hw = (
            board.detail.get("processor")
            or _cpuinfo_field(cpuinfo, "Hardware")
            or _cpuinfo_field(cpuinfo, "model name")
            or "CPU"
        )
        impl = _cpuinfo_field(cpuinfo, "CPU part")

        dev = Device(
            uid="host:soc",
            kind=Kind.SOC,
            name=hw,
            status=Status.ONLINE,
            model=hw,
            parent=board.uid,
        )
        dev.detail["cores"] = cores
        core_name = _ARM_PARTS.get(impl.lower())
        if core_name:
            dev.detail["core"] = f"{cores}× {core_name}"

        temp = _cpu_temp()
        if temp is not None:
            dev.metrics["temp_c"] = temp
            dev.detail["temperature"] = f"{temp:.1f}°C"

        freqs = [
            read_int(p) for p in
            glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")
        ]
        freqs = [f for f in freqs if f]
        if freqs:
            cur = max(freqs) * 1000            # sysfs reports kHz
            dev.metrics["freq_hz"] = float(cur)
            dev.detail["frequency"] = human_hz(cur)
        gov = read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
        if gov:
            dev.detail["governor"] = gov
        fmax = read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")
        if fmax:
            dev.detail["max_frequency"] = human_hz(fmax * 1000)

        load = os.getloadavg()
        dev.metrics["load1"] = load[0]
        dev.detail["load"] = f"{load[0]:.2f} {load[1]:.2f} {load[2]:.2f}"
        dev.metrics["load_pct"] = min(100.0, load[0] / cores * 100)

        # Extra clock domains only the VideoCore firmware knows about.
        if have("vcgencmd"):
            clocks = {}
            for domain in ("arm", "core", "v3d", "isp", "hevc", "emmc"):
                out = run_ok(["vcgencmd", "measure_clock", domain], timeout=3)
                m = re.search(r"=(\d+)", out)
                if m and int(m.group(1)):
                    clocks[domain] = human_hz(int(m.group(1)))
            if clocks:
                dev.detail["clocks"] = clocks
            volt = run_ok(["vcgencmd", "measure_volts", "core"], timeout=3)
            m = re.search(r"=([\d.]+)V", volt)
            if m:
                dev.detail["core_voltage"] = f"{float(m.group(1)):.4f}V"

            self._throttle(dev)

        bits = []
        if temp is not None:
            bits.append(f"{temp:.1f}°C")
        if "frequency" in dev.detail:
            bits.append(dev.detail["frequency"])
        bits.append(f"{cores} cores")
        bits.append(f"load {load[0]:.2f}")
        dev.summary = " · ".join(bits)

        if temp is not None:
            if temp >= 85:
                dev.status = Status.DEGRADED
                dev.issue(
                    Severity.ERROR, f"SoC at {temp:.1f}°C — hard throttle territory",
                    fix="Improve cooling; check the fan and case airflow.",
                )
            elif temp >= 75:
                dev.issue(
                    Severity.WARN, f"SoC at {temp:.1f}°C — approaching the soft limit (80°C)",
                    doc="The Pi 5 starts trimming clocks at 80°C and hard-throttles at 85°C.",
                )
        dev.act("watch", "Live SoC telemetry", "updev watch")
        return dev

    def _throttle(self, dev: Device) -> None:
        out = run_ok(["vcgencmd", "get_throttled"], timeout=3)
        m = re.search(r"0x([0-9a-fA-F]+)", out)
        if not m:
            return
        flags = int(m.group(1), 16)
        dev.detail["throttled_raw"] = f"0x{flags:X}"
        live, past = [], []
        for bit, label, is_live in _THROTTLE_BITS:
            if flags & (1 << bit):
                (live if is_live else past).append(label)
        if live:
            dev.status = Status.DEGRADED
            dev.detail["throttle_now"] = live
            for label in live:
                sev = Severity.ERROR if "under-voltage" in label else Severity.WARN
                dev.issue(
                    sev, f"right now: {label}",
                    fix=(
                        "Use the official 27W USB-C PD supply."
                        if "under-voltage" in label
                        else "Improve cooling."
                    ),
                )
        if past:
            dev.detail["throttle_history"] = past
            dev.issue(
                Severity.INFO,
                "since boot: " + ", ".join(past),
                doc="Sticky flags — they record history, not the current state. "
                    "Cleared on reboot.",
            )

    # -- memory ------------------------------------------------------------

    def _memory(self, parent: str) -> Device:
        info = {}
        for line in read_text("/proc/meminfo").splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = v.strip()

        def kb(key: str) -> int:
            return int(info.get(key, "0 kB").split()[0]) * 1024

        total, avail = kb("MemTotal"), kb("MemAvailable")
        used = total - avail
        swap_total, swap_free = kb("SwapTotal"), kb("SwapFree")

        dev = Device(
            uid="host:memory",
            kind=Kind.HOST,
            name="System memory",
            status=Status.ONLINE,
            parent=parent,
        )
        pct = (used / total * 100) if total else 0.0
        dev.metrics["used_pct"] = pct
        dev.metrics["used_bytes"] = float(used)
        dev.metrics["total_bytes"] = float(total)
        dev.detail["total"] = human_bytes(total)
        dev.detail["used"] = human_bytes(used)
        dev.detail["available"] = human_bytes(avail)
        if swap_total:
            dev.detail["swap"] = f"{human_bytes(swap_total - swap_free)} / {human_bytes(swap_total)}"
            dev.metrics["swap_used_bytes"] = float(swap_total - swap_free)
        dev.summary = f"{human_bytes(used)} / {human_bytes(total)} used ({pct:.0f}%)"

        if pct >= 92:
            dev.status = Status.DEGRADED
            dev.issue(Severity.WARN, f"memory {pct:.0f}% used")
        if swap_total and (swap_total - swap_free) > swap_total * 0.5:
            dev.issue(Severity.WARN, "swap more than half full — the board is memory-starved")
        return dev

    # -- thermal zones + fan -----------------------------------------------

    def _thermal(self, parent: str) -> list[Device]:
        out: list[Device] = []
        for zone in glob("/sys/class/thermal/thermal_zone*"):
            raw = read_int(zone / "temp")
            if raw is None:
                continue
            temp = raw / 1000
            ztype = read_text(zone / "type") or zone.name
            dev = Device(
                uid=f"thermal:{zone.name}",
                kind=Kind.THERMAL,
                name=ztype,
                status=Status.ONLINE,
                node=str(zone),
                parent=parent,
            )
            dev.metrics["temp_c"] = temp
            dev.summary = f"{temp:.1f}°C"
            trips = {}
            for tp in sorted(zone.glob("trip_point_*_temp")):
                t = read_int(tp)
                ttype = read_text(str(tp).replace("_temp", "_type"))
                if t and t > 0:
                    trips[ttype or tp.name] = f"{t / 1000:.0f}°C"
            if trips:
                dev.detail["trip_points"] = trips
            out.append(dev)

        # RP1 has its own ADC + temperature sensor on the Pi 5.
        for hw in glob("/sys/class/hwmon/hwmon*"):
            hname = read_text(hw / "name")
            if hname != "rp1_adc":
                continue
            dev = Device(
                uid="host:rp1-adc",
                kind=Kind.SOC,
                name="RP1 ADC",
                status=Status.ONLINE,
                node=str(hw),
                parent=parent,
                summary="southbridge analogue inputs",
            )
            for ch in sorted(hw.glob("in*_input")):
                mv = read_int(ch)
                if mv is not None:
                    dev.detail[ch.name.replace("_input", "")] = f"{mv / 1000:.3f}V"
            t = read_int(hw / "temp1_input")
            if t is not None:
                dev.metrics["temp_c"] = t / 1000
                dev.detail["temperature"] = f"{t / 1000:.1f}°C"
            out.append(dev)
        return out

    def _fan(self, parent: str) -> Device | None:
        """The official Pi 5 cooler shows up as a pwm-fan cooling device."""
        for cool in glob("/sys/class/thermal/cooling_device*"):
            ctype = read_text(cool / "type")
            if "fan" not in ctype.lower():
                continue
            cur = read_int(cool / "cur_state")
            mx = read_int(cool / "max_state") or 1
            dev = Device(
                uid=f"fan:{cool.name}",
                kind=Kind.THERMAL,
                name=ctype,
                status=Status.ONLINE if (cur or 0) > 0 else Status.IDLE,
                node=str(cool),
                parent=parent,
            )
            dev.detail["state"] = f"{cur}/{mx}"
            pct = (cur / mx * 100) if mx else 0
            dev.metrics["fan_pct"] = pct
            dev.summary = f"step {cur}/{mx} ({pct:.0f}%)"
            for hw in glob("/sys/class/hwmon/hwmon*"):
                rpm = read_int(hw / "fan1_input")
                if rpm is not None:
                    dev.metrics["rpm"] = float(rpm)
                    dev.detail["rpm"] = rpm
                    dev.summary += f" · {rpm} rpm"
                    break
            return dev
        return None

    # -- PMIC power --------------------------------------------------------

    def _power(self, parent: str) -> Device | None:
        """Sum every PMIC rail into an actual wattage figure."""
        if not have("vcgencmd"):
            return None
        rc, out, _ = run(["vcgencmd", "pmic_read_adc"], timeout=6)
        if rc != 0 or "volt" not in out:
            return None

        amps: dict[str, float] = {}
        volts: dict[str, float] = {}
        for line in out.splitlines():
            m = _PMIC_LINE.match(line)
            if not m:
                continue
            rail = m.group("rail")
            value = float(m.group("val"))
            base = rail[:-2] if rail.endswith(("_A", "_V")) else rail
            (amps if m.group("what") == "current" else volts)[base] = value

        if not volts:
            return None

        dev = Device(
            uid="host:power",
            kind=Kind.POWER,
            name="PMIC",
            status=Status.ONLINE,
            parent=parent,
            vendor="Raspberry Pi",
        )

        rails: dict[str, str] = {}
        total_w = 0.0
        for base, v in sorted(volts.items()):
            a = amps.get(base)
            if a is None:
                rails[base] = f"{v:.3f}V"
                continue
            w = v * a
            total_w += w
            rails[base] = f"{v:.3f}V @ {a * 1000:7.1f}mA = {w:6.3f}W"
        dev.detail["rails"] = rails
        dev.metrics["power_w"] = total_w
        dev.detail["rail_total"] = f"{total_w:.2f}W"

        ext5v = volts.get("EXT5V")
        if ext5v:
            dev.metrics["input_v"] = ext5v
            dev.detail["input_5v"] = f"{ext5v:.3f}V"
            if ext5v < 4.75:
                dev.status = Status.DEGRADED
                dev.issue(
                    Severity.ERROR,
                    f"5V input sagging to {ext5v:.2f}V",
                    fix="Use the official 27W USB-C PD supply and a short, thick cable.",
                    doc="Below 4.75V the Pi 5 starts throttling and USB ports get flaky.",
                )
            elif ext5v < 4.9:
                dev.issue(Severity.WARN, f"5V input a little low ({ext5v:.2f}V)")

        # rpi_volt exposes a hardware undervoltage latch independent of vcgencmd.
        for hw in glob("/sys/class/hwmon/hwmon*"):
            if read_text(hw / "name") == "rpi_volt":
                if read_int(hw / "in0_lcrit_alarm"):
                    dev.status = Status.DEGRADED
                    dev.issue(
                        Severity.ERROR, "PMIC undervoltage alarm is latched",
                        fix="Replace the power supply.",
                    )

        summary = f"{total_w:.2f}W across {len(rails)} rails"
        if ext5v:
            summary += f" · input {ext5v:.2f}V"
        dev.summary = summary
        dev.act("rails", "Per-rail breakdown", "updev show host:power")
        return dev

    # -- HAT ---------------------------------------------------------------

    def _hat(self, parent: str) -> Device | None:
        base = "/proc/device-tree/hat"
        if not os.path.isdir(base):
            return None
        product = read_text(f"{base}/product").rstrip("\x00")
        vendor = read_text(f"{base}/vendor").rstrip("\x00")
        dev = Device(
            uid="host:hat",
            kind=Kind.HOST,
            name=product or "HAT",
            status=Status.ONLINE,
            vendor=vendor,
            model=product,
            parent=parent,
            node=base,
        )
        for field_name in ("product_id", "product_ver", "uuid"):
            val = read_text(f"{base}/{field_name}").rstrip("\x00")
            if val:
                dev.detail[field_name] = val
        dev.summary = f"{vendor} {product}".strip() or "HAT EEPROM present"
        return dev


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _cpuinfo_field(cpuinfo: str, key: str) -> str:
    for line in cpuinfo.splitlines():
        k, _, v = line.partition(":")
        if k.strip() == key:
            return v.strip()
    return ""


def _os_pretty_name() -> str:
    for line in read_text("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.partition("=")[2].strip().strip('"')
    return platform.system()


def _uptime() -> float | None:
    raw = read_text("/proc/uptime")
    try:
        return float(raw.split()[0])
    except (ValueError, IndexError):
        return None


def _cpu_temp() -> float | None:
    raw = read_int("/sys/class/thermal/thermal_zone0/temp")
    if raw is not None:
        return raw / 1000
    if have("vcgencmd"):
        m = re.search(r"([\d.]+)", run_ok(["vcgencmd", "measure_temp"], timeout=3))
        if m:
            return float(m.group(1))
    return None


def _decode_revision(revision: str) -> dict[str, str]:
    """Unpack a new-style Pi revision code into something a human can read."""
    try:
        code = int(revision, 16)
    except ValueError:
        return {}
    if not (code & (1 << 23)):        # old-style code, not worth decoding
        return {}
    out = {
        "board": "Pi " + _MODELS.get((code >> 4) & 0xFF, f"type 0x{(code >> 4) & 0xFF:02X}"),
        "processor": _PROCESSORS.get((code >> 12) & 0xF, "?"),
        "ram": _MEMORY.get((code >> 20) & 0x7, "?"),
        "manufacturer": _MANUFACTURERS.get((code >> 16) & 0xF, "?"),
        "pcb_revision": f"1.{code & 0xF}",
    }
    if code & (1 << 25):
        out["warranty"] = "void (overvolted)"
    return out


# Boot sources as defined by the Raspberry Pi bootloader's BOOT_ORDER.
_BOOT_SOURCES = {
    "0": "SD card detect",
    "1": "SD card",
    "2": "network",
    "3": "RPIBOOT (USB device mode)",
    "4": "USB mass storage",
    "5": "USB 2.0 mass storage (BCM2711)",
    "6": "NVMe / PCIe",
    "7": "HTTP",
    "e": "stop with error",
    "f": "restart the sequence",
}


def _explain_boot_order(raw: str) -> str:
    """BOOT_ORDER is read right-to-left, one nibble per attempt."""
    digits = raw.lower().removeprefix("0x")
    order = [_BOOT_SOURCES.get(d, d) for d in reversed(digits) if d != "0"]
    return f"{raw} → " + " → ".join(order) if order else raw
