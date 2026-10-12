"""IR remote receiver: what the air conditioner was last told to do.

updev never touches the IR pin itself. Decoding IR means timing edges to the
microsecond from a GPIO callback, which only one process can own, and that
process is `ir_lcd.py` from the jicsee1 repo (pixel_chinese_quiz/ir_lcd.py).
It drops a small JSON state file every time it decodes a code; this backend
just reads that file. No file → the receiver program isn't running, so the
backend reports itself unavailable instead of inventing a device.

State file (default /tmp/ir_ac.json, override with $IR_AC_STATE), written
atomically by ir_lcd.py:

    {"version": 1, "pid": 1234, "pin": 18,
     "started": 1760000000.0, "updated": 1760000123.4, "count": 3,
     "last": {"code": "0x8808A46", "data": "0x08A4", "temp_c": 25, "time": 1760000123.4}}

`last` is null until the first button press. Only LG AC codes
0x880834F..0x8808F4B (set-point 18..30 °C) are ever recorded; the temperature
is bits 8-11 of the code + 15. If you change the format, bump `version` on
both sides.

The receiver is a VS1838B (38 kHz demodulating, active-low output) on GPIO18
by default. `decode_lg()` unpacks the 28-bit LG AC frame so updev can show
mode, fan and checksum too, not just the temperature ir_lcd.py stored.
"""

from __future__ import annotations

import json
import os
import time

from ..core.model import Device, Kind, Severity, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import human_duration

STATE_VERSION = 1
DEFAULT_STATE_PATH = "/tmp/ir_ac.json"

#: A receiver that has heard nothing for this long is probably pointed the
#: wrong way, unplugged, or its output pin is floating.
QUIET_AFTER_S = 6 * 3600

# LG AC frame, 28 bits, MSB first (field layout as in IRremoteESP8266's ir_LG):
#   [27:20] signature 0x88  [19:18] power (3 = off)  [14:12] mode
#   [11:8] temp-15  [7:4] fan  [3:0] checksum = sum of nibbles [19:4] & 0xF
LG_SIGNATURE = 0x88
LG_MODES = {0: "cool", 1: "dry", 2: "fan", 3: "auto", 4: "heat"}
LG_FANS = {0: "lowest", 1: "low", 9: "low", 2: "medium", 10: "high", 4: "max", 5: "auto"}


def parse_code(text: str | int) -> int:
    """'0x8808A46', '8808A46' or an int → int. Raises ValueError on junk."""
    if isinstance(text, int):
        return text
    t = text.strip().lower()
    return int(t[2:] if t.startswith("0x") else t, 16)


def lg_checksum(code: int) -> int:
    return sum((code >> shift) & 0xF for shift in (4, 8, 12, 16)) & 0xF


def decode_lg(code: int) -> dict | None:
    """Unpack an LG AC code, or None if it isn't one (wrong signature/size)."""
    if code < 0 or code >> 28 or (code >> 20) & 0xFF != LG_SIGNATURE:
        return None
    power_off = (code >> 18) & 0x3 == 0x3
    mode_n = (code >> 12) & 0x7
    fan_n = (code >> 4) & 0xF
    out = {
        "code": f"0x{code:07X}",
        "power": "off" if power_off else "on",
        "checksum_ok": lg_checksum(code) == code & 0xF,
    }
    if not power_off:
        out.update({
            "mode": LG_MODES.get(mode_n, f"mode{mode_n}"),
            "temp_c": ((code >> 8) & 0xF) + 15,
            "fan": LG_FANS.get(fan_n, f"fan{fan_n}"),
        })
    return out


def read_state(path: str | None = None) -> dict | None:
    """Parsed state file, None if absent. Raises ValueError if it's garbage."""
    try:
        with open(path or state_path()) as f:
            state = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(str(e)) from e
    if not isinstance(state, dict):
        raise ValueError("top level is not an object")
    return state


def broken_state_device(why: str) -> Device:
    dev = Device(uid="ir:state", kind=Kind.IR, name="IR receiver",
                 status=Status.DEGRADED, bus="gpio", driver="ir_lcd.py",
                 summary="unreadable state file", detail={"state_file": state_path()})
    dev.issue(Severity.WARN, f"can't read {state_path()}: {why}",
              fix=f"rm {state_path()}  # ir_lcd.py rewrites it on the next code",
              doc="ir_lcd.py writes the file atomically, so a broken one usually "
                  "means something else wrote to that path.")
    return dev


def state_path() -> str:
    return os.environ.get("IR_AC_STATE", DEFAULT_STATE_PATH)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:          # alive, just owned by someone else (e.g. root)
        return True
    return True


def device_from_state(state: dict, now: float, alive: bool) -> Device:
    """Turn a parsed state file into a Device. Pure, so tests can feed it dicts."""
    pin = int(state.get("pin", 18))
    dev = Device(
        uid=f"ir:gpio{pin}",
        kind=Kind.IR,
        name=f"IR receiver (GPIO{pin})",
        status=Status.IDLE,
        bus="gpio",
        address=f"GPIO{pin}",
        vendor="LG",
        model="AC remote",
        driver="ir_lcd.py",
        detail={"state_file": state_path(), "pid": state.get("pid"),
                "decoded": state.get("count", 0)},
    )
    dev.tags.append("lg-ac")

    if state.get("version") != STATE_VERSION:
        dev.status = Status.DEGRADED
        dev.summary = f"unknown state file version {state.get('version')!r}"
        dev.issue(Severity.WARN, "IR state file format doesn't match this updev",
                  doc=f"updev understands version {STATE_VERSION}; update updev or ir_lcd.py "
                      "so both sides agree.")
        return dev

    if not alive:
        dev.status = Status.DEGRADED
        dev.summary = "receiver program stopped (stale state file)"
        dev.issue(Severity.WARN, "ir_lcd.py isn't running — no IR codes are being decoded",
                  fix="cd ~/jicsee1/pixel_chinese_quiz && python3 ir_lcd.py",
                  doc=f"{state_path()} was left behind by pid {state.get('pid')}, "
                      "which no longer exists.")
        return dev

    last = state.get("last")
    if not last:
        dev.summary = "listening · no button pressed yet"
        return dev

    age = max(0.0, now - float(last.get("time", now)))
    temp = last.get("temp_c")
    decoded = None
    try:
        decoded = decode_lg(parse_code(last.get("code", "")))
    except ValueError:
        pass
    if decoded and temp is None:
        temp = decoded.get("temp_c")

    dev.status = Status.ONLINE
    bits = [f"AC set to {temp}°C"]
    if decoded and decoded.get("mode"):
        bits.append(f"{decoded['mode']}, fan {decoded['fan']}")
    bits.append(f"{human_duration(age)} ago")
    dev.summary = " · ".join(bits)
    dev.detail.update({"last_code": last.get("code"), "last_data": last.get("data"),
                       "temp_c": temp})
    if decoded:
        dev.detail.update({k: decoded[k] for k in ("power", "mode", "fan", "checksum_ok")
                           if k in decoded})
        if not decoded["checksum_ok"]:
            dev.issue(Severity.INFO, f"last code {decoded['code']} has a bad LG checksum",
                      doc="Usually a partial frame — the remote was too far, at an angle, "
                          "or the VS1838 picked up sunlight/LED noise. Press again.")
    if temp is not None:
        dev.metrics["temp_c"] = float(temp)
    dev.metrics["last_age_s"] = round(age, 1)
    started = float(state.get("started", 0) or 0)
    if started:
        dev.metrics["uptime_s"] = round(max(0.0, now - started), 1)
    if age > QUIET_AFTER_S:
        dev.status = Status.IDLE
        dev.issue(Severity.INFO, f"no IR code in {human_duration(age)}",
                  doc="If you've been pressing buttons, check the VS1838 wiring: "
                      "OUT→GPIO, VCC→3V3 (not 5V), GND→GND, dome facing the remote.")
    return dev


class IrBackend(Backend):
    name = "ir"
    title = "IR remote"
    kinds = (Kind.IR,)

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        if not os.path.exists(state_path()):
            return False, f"no {state_path()} (ir_lcd.py not running)"
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        try:
            state = read_state()
        except ValueError as e:
            return [broken_state_device(str(e))]
        if state is None:               # receiver exited between available() and probe()
            return []
        return [device_from_state(state, time.time(), _pid_alive(int(state.get("pid", 0) or 0)))]
