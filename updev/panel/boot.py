"""What the panel shows while the board comes up.

Three screens, in order:

  1. **Splash** — drawn before anything is probed, so the panel lights up in
     well under a second rather than looking dead for the length of a scan.
  2. **Progress** — one row per backend, ticked off as each report lands. This
     is the screen that earns its keep: when a bus is wedged, the row that
     never resolves names the backend that hung, which is exactly what you
     cannot see from a blank display or a headless boot.
  3. **Summary** — what was found, and the handful of numbers worth having on
     the glass afterwards: address, temperature, memory, draw, uptime.

The summary is the resting state. `Screen.close()` leaves it there when the
process exits, so the panel keeps reading as a status display with nothing
running.
"""

from __future__ import annotations

import re
import time

from ..core.model import BackendReport, ScanResult, Severity, Status
from ..core.registry import ProbeContext, Scanner
from ..core.util import human_duration, read_text
from .screen import (
    ACCENT, BAD, BAND, BLACK, DIM, FG, GRID, STATUS_RGB, WARN, Screen,
)

__all__ = ["boot", "splash", "progress", "summary"]

VERSION = "1.0.0"

_HEADER_H = 17
_FOOTER_Y = 145
#: A backend that hasn't reported yet — legible, but clearly not a result.
PENDING = (84, 84, 92)

#: Worst first, so the eye lands on trouble before it lands on the count.
_CHIP_ORDER = (Status.ERROR, Status.DEGRADED, Status.ABSENT, Status.DISABLED,
               Status.ONLINE, Status.IDLE, Status.UNKNOWN)


# --------------------------------------------------------------------------
# common furniture
# --------------------------------------------------------------------------

def _header(screen: Screen, right: str = VERSION,
            rule: tuple[int, int, int] = ACCENT) -> None:
    screen.fill((0, 0, screen.width, _HEADER_H), BAND)
    screen.text(5, 3, "updev", ACCENT, size=11, bold=True)
    if right:
        screen.text(screen.width - 5, 6, right, DIM, size=8, right=True)
    screen.rule(_HEADER_H, rule)


def _footer(screen: Screen, text: str, colour: tuple[int, int, int] = DIM) -> None:
    screen.fill((0, _FOOTER_Y, screen.width, screen.height), BAND)
    screen.rule(_FOOTER_Y, GRID)
    screen.text(5, _FOOTER_Y + 4, screen.truncate(text, screen.width - 10, 8), colour, size=8)


# --------------------------------------------------------------------------
# 1. splash
# --------------------------------------------------------------------------

def splash(screen: Screen, board: str = "") -> None:
    """The first thing on the glass. Drawn before a single bus is touched.

    The board name comes straight from the device tree rather than from the
    scan, because the whole point of this screen is to appear before the scan
    does.
    """
    board = board or read_text("/proc/device-tree/model").strip("\x00 ") or "Raspberry Pi"
    screen.clear(BLACK)
    screen.fill((0, 0, screen.width, 60), BAND)
    screen.text(8, 14, "updev", ACCENT, size=26, bold=True)
    screen.text(10, 44, "device manager", DIM, size=8)
    screen.rule(60, ACCENT)

    y = 70
    for line in _wrap(screen, board, screen.width - 12, size=9):
        y = screen.text(6, y, line, FG, size=9)

    screen.text(6, 122, "probing hardware", DIM, size=9)
    screen.bar(6, 136, screen.width - 12, 4, 0.06)
    _footer(screen, f"v{VERSION}")
    screen.flush()


def _wrap(screen: Screen, text: str, width: int, size: int = 9) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if screen.draw.textlength(candidate, font=_face(size)) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:3]


def _face(size: int):
    from .screen import font
    return font(size)


# --------------------------------------------------------------------------
# 2. progress
# --------------------------------------------------------------------------

def progress(screen: Screen, planned: list[str],
             done: dict[str, BackendReport]) -> None:
    """One row per backend: name, what it found, and a dot for how it went.

    Backends run concurrently, so every row without a report is genuinely
    still in flight — no row is singled out as "the current one", because
    there isn't one. A row that stays pending to the end names the bus that
    hung, which is the reason this screen exists.
    """
    screen.clear(BLACK)
    _header(screen, f"{len(done)}/{len(planned)}")

    top = _HEADER_H + 5
    room = _FOOTER_Y - top - 4
    step = max(8, min(11, room // max(1, len(planned))))

    for i, name in enumerate(planned):
        y = top + i * step
        if y + step > _FOOTER_Y:
            break
        report = done.get(name)
        colour, note = _row_state(report)
        screen.text(6, y, screen.truncate(name, 62, 9), colour, size=9)
        if note:
            screen.text(screen.width - 14, y + 1, note, DIM, size=8, right=True)
        screen.dot(screen.width - 7, y + 5, colour, filled=report is not None)

    fraction = len(done) / max(1, len(planned))
    screen.bar(6, _FOOTER_Y - 8, screen.width - 12, 4, fraction)
    pending = [n for n in planned if n not in done]
    _footer(screen, ("waiting: " + " ".join(pending)) if pending else "done")
    screen.flush()


def _row_state(report: BackendReport | None) -> tuple[tuple[int, int, int], str]:
    if report is None:
        return PENDING, "…"
    if not report.available:
        return STATUS_RGB[Status.DISABLED], "off"
    if not report.ok:
        return BAD, "err"
    return STATUS_RGB[Status.ONLINE], str(report.count)


# --------------------------------------------------------------------------
# 3. summary
# --------------------------------------------------------------------------

def summary(screen: Screen, result: ScanResult) -> None:
    """The resting screen: the count, the health, and six numbers."""
    counts: dict[Status, int] = {}
    for device in result.devices:
        counts[device.status] = counts.get(device.status, 0) + 1

    screen.clear(BLACK)
    _header(screen)

    screen.text(6, 22, str(len(result.devices)), FG, size=20, bold=True)
    screen.text(screen.width - 5, 32, "devices", DIM, size=8, right=True)

    # Every status that occurred, worst first, so the chips add up to the
    # total above them — a row that silently drops 13 idle devices reads as
    # the panel hiding something.
    x = 6
    for status in _CHIP_ORDER:
        n = counts.get(status, 0)
        chip = 13 + 6 * len(str(n))
        if not n or x + chip > screen.width - 4:
            continue
        screen.dot(x + 2, 51, STATUS_RGB[status])
        screen.text(x + 8, 46, str(n), STATUS_RGB[status], size=9)
        x += chip
    screen.rule(60, GRID)

    y = 65
    for label, value in _facts(result):
        screen.text(6, y, label, DIM, size=9)
        screen.text(screen.width - 6, y, screen.truncate(value, 74, 9), FG,
                    size=9, right=True)
        y += 12

    errors = sum(1 for _, i in result.issues() if i.severity is Severity.ERROR)
    warns = sum(1 for _, i in result.issues() if i.severity is Severity.WARN)
    if errors or warns:
        note = ", ".join(
            part for part in (f"{errors} error" if errors else "",
                              f"{warns} warning" if warns else "") if part
        )
        screen.text(6, _FOOTER_Y - 13, note, BAD if errors else WARN, size=9)

    ok = sum(1 for r in result.reports if r.available and r.ok)
    _footer(screen, f"{result.duration:.1f}s · {ok}/{len(result.reports)} backends")
    screen.flush()


def _facts(result: ScanResult) -> list[tuple[str, str]]:
    """The six lines worth keeping on the glass, skipping whatever is absent."""
    by_uid = result.by_uid()
    facts: list[tuple[str, str]] = []

    board = by_uid.get("host:board")
    if board:
        name = re.sub(r"\s+Rev\s+\S+$", "", board.name).replace("Raspberry Pi ", "Pi ")
        facts.append(("board", name))

    iface = _primary_iface(result)
    if iface:
        addr = next(iter(iface.detail.get("ipv4", [])), "").split("/")[0]
        facts.append((iface.name[:6], addr or "no address"))

    soc = by_uid.get("host:soc")
    if soc and "temp_c" in soc.metrics:
        ghz = soc.metrics.get("freq_hz", 0) / 1e9
        facts.append(("soc", f"{soc.metrics['temp_c']:.0f}°C"
                             + (f"  {ghz:.1f}GHz" if ghz else "")))

    memory = by_uid.get("host:memory")
    if memory and "used_pct" in memory.metrics:
        facts.append(("mem", f"{memory.metrics['used_pct']:.0f}%"))

    power = by_uid.get("host:power")
    if power and "power_w" in power.metrics:
        facts.append(("power", f"{power.metrics['power_w']:.1f}W"))

    if board and "uptime_s" in board.metrics:
        facts.append(("up", human_duration(board.metrics["uptime_s"])))

    return facts[:6]


def _primary_iface(result: ScanResult):
    """The interface packets actually leave by, not merely the first one up."""
    candidates = [d for d in result.devices if str(d.kind) == "net-iface" and d.detail]
    for device in candidates:
        if device.detail.get("default_route") == "yes":
            return device
    for device in candidates:
        if device.status is Status.ONLINE and device.detail.get("ipv4") \
                and not device.name.startswith(("lo", "docker", "br-", "veth")):
            return device
    return None


# --------------------------------------------------------------------------
# the sequence
# --------------------------------------------------------------------------

def boot(screen: Screen, scanner: Scanner | None = None,
         ctx: ProbeContext | None = None, *, splash_s: float = 0.8,
         hold_s: float = 0.0) -> ScanResult:
    """Splash, then live progress, then the summary. Returns the scan.

    `splash_s` is a floor, not a sleep the scan waits on — the board name it
    shows comes from the host backend, which runs as part of the same scan.
    """
    from ..core.registry import build_scanner

    scanner = scanner or build_scanner()
    ctx = ctx or ProbeContext()

    splash(screen)
    started = time.monotonic()

    planned = [b.name for b in scanner.selected(ctx)]
    done: dict[str, BackendReport] = {}

    def on_report(report: BackendReport) -> None:
        done[report.name] = report
        if time.monotonic() - started >= splash_s:
            progress(screen, planned, done)

    result = scanner.scan(ctx, on_report=on_report)
    progress(screen, planned, done)
    summary(screen, result)
    if hold_s:
        time.sleep(hold_s)
    return result
