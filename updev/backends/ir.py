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
    dev.status = Status.ONLINE
    dev.summary = f"AC set to {temp}°C · {human_duration(age)} ago"
    dev.detail.update({"last_code": last.get("code"), "last_data": last.get("data"),
                       "temp_c": temp})
    if temp is not None:
        dev.metrics["temp_c"] = float(temp)
    dev.metrics["last_age_s"] = round(age, 1)
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
            with open(state_path()) as f:
                state = json.load(f)
        except FileNotFoundError:       # receiver exited between available() and probe()
            return []
        return [device_from_state(state, time.time(), _pid_alive(int(state.get("pid", 0))))]
