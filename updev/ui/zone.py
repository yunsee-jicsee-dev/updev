"""Two live, interactive views.

`EventMonitor` — `updev activity`. A scrolling hotplug log. Not full-screen,
so it pipes and scrolls like any other command-line tool.

`UsbZone` — `updev usb zone`, the 체험존. Plug something in and it tells you
which class it is *and why*, laying out every signature it read and what each
one argued for. Reclassifies as facts arrive: a disk shows up on USB before
the kernel has finished SCSI enumeration, so the first verdict is often made
on partial evidence and improves a beat later.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime

from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..core.changes import Change, ChangeKind, diff_devices
from ..core.model import Device, Kind, ScanResult
from ..core.registry import ProbeContext, Scanner
from ..usbclass import (
    CLASS_LABEL,
    CLASS_STYLE,
    Confidence,
    UsbClass,
    Verdict,
    classify,
    gather_facts,
)
from .render import change_text

#: Backends that answer fast enough to poll several times a second.
FAST_BACKENDS = frozenset({"usb", "storage"})
ACTIVITY_BACKENDS = frozenset({"usb", "storage", "serial", "net", "camera", "bluetooth"})

_CONFIDENCE_STYLE = {
    Confidence.CERTAIN: "bold green",
    Confidence.HIGH: "green",
    Confidence.MEDIUM: "yellow",
    Confidence.LOW: "bold red",
}


# ==========================================================================
# updev activity
# ==========================================================================

class EventMonitor:
    """Scrolling hotplug log."""

    def __init__(
        self,
        scanner: Scanner,
        ctx: ProbeContext,
        console: Console,
        interval: float = 0.5,
        as_json: bool = False,
    ) -> None:
        self.scanner = scanner
        self.ctx = ctx
        self.console = console
        self.interval = max(0.1, interval)
        self.as_json = as_json
        self.previous: dict[str, Device] = {}
        self.count = 0

    def run(self, duration: float = 0.0, limit: int = 0) -> int:
        """Watch until Ctrl-C, or until `duration` seconds / `limit` events."""
        if not self.as_json:
            self.console.print(
                Text("watching for device changes — plug something in "
                     "(ctrl-c to stop)", style="dim italic")
            )
        deadline = time.time() + duration if duration else None
        try:
            while True:
                result = self.scanner.scan(self.ctx)
                for change in diff_devices(self.previous, result.by_uid()):
                    self._emit(change)
                    self.count += 1
                    if limit and self.count >= limit:
                        return self.count
                self.previous = result.by_uid()
                if deadline and time.time() >= deadline:
                    return self.count
                self._sleep(deadline)
        except KeyboardInterrupt:
            if not self.as_json:
                self.console.print(Text("\nstopped", style="dim"))
        return self.count

    def _sleep(self, deadline: float | None) -> None:
        end = time.time() + self.interval
        if deadline:
            end = min(end, deadline)
        while time.time() < end:
            time.sleep(min(0.05, max(0.0, end - time.time())))

    def _emit(self, change: Change) -> None:
        if self.as_json:
            import json

            print(json.dumps(change.as_dict(), default=str, ensure_ascii=False), flush=True)
            return
        line = Text(datetime.fromtimestamp(change.when).strftime("%H:%M:%S "), style="dim")
        line.append_text(change_text(change))
        self.console.print(line)


# ==========================================================================
# updev usb zone
# ==========================================================================

class UsbZone:
    """The 체험존: plug a device in, get a classification with its reasoning."""

    def __init__(
        self,
        scanner: Scanner,
        ctx: ProbeContext,
        console: Console,
        interval: float = 0.25,
    ) -> None:
        self.scanner = scanner
        self.ctx = ctx
        self.console = console
        self.interval = max(0.1, interval)
        self.previous: dict[str, Device] = {}
        self.current: tuple[Device, Verdict] | None = None
        self.history: deque[tuple[float, Device, Verdict]] = deque(maxlen=12)
        self.polls = 0
        #: Devices present at startup are the baseline, not "plugged in".
        self.baseline: set[str] = set()

    # -- loop --------------------------------------------------------------

    def run(self, iterations: int | None = None, include_existing: bool = False) -> None:
        first = self.scanner.scan(self.ctx)
        self.previous = first.by_uid()
        self.baseline = set(self.previous)
        if include_existing:
            for dev in self._storage_devices(first):
                self._present(dev)

        with Live(self.render(), console=self.console, screen=True,
                  refresh_per_second=8) as live:
            try:
                while iterations is None or self.polls < iterations:
                    self.step()
                    live.update(self.render())
                    end = time.time() + self.interval
                    while time.time() < end:
                        time.sleep(min(0.05, max(0.0, end - time.time())))
            except KeyboardInterrupt:
                pass

    def step(self) -> None:
        result = self.scanner.scan(self.ctx)
        self.polls += 1
        now = result.by_uid()

        for change in diff_devices(self.previous, now):
            if change.kind == ChangeKind.ADDED and self._is_usb_storage(change.device):
                self._present(change.device)
            elif change.kind == ChangeKind.REMOVED and self.current:
                if change.device.uid == self.current[0].uid:
                    self.current = None

        # A freshly plugged disk enumerates in stages, so keep re-reading until
        # the block device turns up and the verdict stops improving.
        if self.current:
            dev = now.get(self.current[0].uid)
            if dev is not None:
                verdict = self._classify(dev)
                if verdict.usb_class != UsbClass.UNKNOWN or self.current[1].usb_class == UsbClass.UNKNOWN:
                    self.current = (dev, verdict)
                    if self.history and self.history[-1][1].uid == dev.uid:
                        self.history[-1] = (self.history[-1][0], dev, verdict)

        self.previous = now

    def _present(self, dev: Device) -> None:
        verdict = self._classify(dev)
        self.current = (dev, verdict)
        self.history.append((time.time(), dev, verdict))

    @staticmethod
    def _is_usb_storage(dev: Device) -> bool:
        return dev.kind == Kind.USB and "storage" in dev.tags

    @staticmethod
    def _storage_devices(result: ScanResult) -> list[Device]:
        return [d for d in result.devices if d.kind == Kind.USB and "storage" in d.tags]

    @staticmethod
    def _classify(dev: Device) -> Verdict:
        return classify(gather_facts(usb_address=dev.address))

    # -- rendering ---------------------------------------------------------

    def render(self) -> RenderableType:
        blocks: list[RenderableType] = [self._legend()]
        if self.current:
            blocks.append(verdict_panel(*self.current))
        else:
            blocks.append(self._waiting())
        if len(self.history) > 1:
            blocks.append(self._history())
        return Group(*blocks)

    def _legend(self) -> Panel:
        grid = Table.grid(padding=(0, 2))
        for _ in UsbClass:
            grid.add_column(no_wrap=True)
        cells = []
        for cls in (UsbClass.NUSB, UsbClass.FUSB, UsbClass.HUSB, UsbClass.SUSB, UsbClass.ODD):
            cell = Text()
            cell.append(f" {cls} ", style=f"bold black on {CLASS_STYLE[cls]}")
            cell.append(f" {CLASS_LABEL[cls].split(' (')[0]}", style=CLASS_STYLE[cls])
            cells.append(cell)
        grid.add_row(*cells)
        title = Text("USB 체험존", style="bold bright_magenta")
        title.append(f"   poll #{self.polls}", style="dim")
        return Panel(grid, title=title, title_align="left",
                     border_style="bright_magenta", box=box.ROUNDED)

    def _waiting(self) -> Panel:
        body = Text()
        body.append("\n  USB 저장장치를 꽂으세요", style="bold")
        body.append("\n\n  꽂는 즉시 시그니처를 읽어서 어느 분류인지, ", style="dim")
        body.append("왜 그렇게 판단했는지", style="dim bold")
        body.append(" 보여줍니다.\n", style="dim")
        body.append("\n  ctrl-c 로 종료\n", style="dim italic")
        return Panel(Align.center(body), border_style="dim", box=box.ROUNDED,
                     title="대기 중", title_align="left")

    def _history(self) -> Panel:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", no_wrap=True)
        grid.add_column(no_wrap=True)
        grid.add_column(overflow="ellipsis")
        for when, dev, verdict in list(self.history)[:-1][::-1]:
            badge = Text(f" {verdict.usb_class} ",
                         style=f"bold black on {CLASS_STYLE[verdict.usb_class]}")
            grid.add_row(
                datetime.fromtimestamp(when).strftime("%H:%M:%S"),
                badge,
                Text(dev.label, style="bold"),
            )
        return Panel(grid, title="이번 세션에서 본 것", title_align="left",
                     border_style="dim", box=box.ROUNDED)


# ==========================================================================
# shared rendering
# ==========================================================================

def verdict_panel(dev: Device, verdict: Verdict, show_facts: bool = True) -> Panel:
    """The classification card: badge, identity, evidence trail."""
    style = CLASS_STYLE[verdict.usb_class]
    body: list[RenderableType] = []

    headline = Text()
    headline.append(f"  {verdict.usb_class}  ", style=f"bold black on {style}")
    headline.append(f"   {verdict.label}", style=f"bold {style}")
    body.append(headline)

    conf = Text("  confidence: ", style="dim")
    conf.append(str(verdict.confidence),
                style=_CONFIDENCE_STYLE.get(verdict.confidence, "white"))
    if verdict.confidence != Confidence.CERTAIN:
        conf.append(f"   margin {verdict.margin}", style="dim")
        runner = verdict.runner_up
        if runner:
            conf.append(f"   (2nd: {runner})", style="dim")
    else:
        conf.append("   — a signature the spec makes unambiguous", style="dim")
    body.append(conf)

    identity = Text("\n  ")
    identity.append(dev.label, style="bold")
    facts = verdict.facts
    bits = []
    if facts:
        if facts.size_bytes:
            from ..usbclass import _human_capacity
            bits.append(_human_capacity(facts.size_bytes))
        if facts.block_name:
            bits.append(f"/dev/{facts.block_name}")
    bits.append(f"usb {dev.address}")
    identity.append("   " + " · ".join(bits), style="dim")
    body.append(identity)

    if verdict.evidence:
        body.append(Text("\n  근거 (signature → verdict)", style="bold dim"))
        body.append(_evidence_table(verdict))
    else:
        body.append(Text("\n  no signatures could be read", style="dim italic"))

    if show_facts and facts:
        body.append(Text("\n  읽은 값 (raw signatures)", style="bold dim"))
        body.append(_facts_table(facts))

    return Panel(Group(*body), border_style=style, box=box.ROUNDED,
                 title="판정", title_align="left")


def _evidence_table(verdict: Verdict) -> Table:
    """Two lines per signature: the reading on top, the argument underneath.

    A single wide row would squeeze the explanation into a column two words
    across, which is where reasoning goes to die.
    """
    outer = Table.grid(padding=(0, 0))
    outer.add_column(overflow="fold")

    for item in verdict.evidence:
        head = Table.grid(padding=(0, 2))
        head.add_column(width=4, no_wrap=True)
        head.add_column(width=30, no_wrap=True, overflow="ellipsis", style="bold")
        head.add_column(width=32, no_wrap=True, overflow="ellipsis")
        head.add_column(no_wrap=True)

        mark = Text(" !!", style="bold green") if item.decisive else Text("")
        arrow = Text()
        for cls, score in sorted(item.scores.items(), key=lambda kv: -kv[1]):
            if score == 0:
                continue
            arrow.append(f"{cls}", style=CLASS_STYLE.get(cls, "white"))
            arrow.append(f"{score:+d} ", style="green" if score > 0 else "red")

        head.add_row(mark, item.signature, item.observed, arrow)
        outer.add_row(head)
        # Padding rather than a literal prefix, so wrapped lines stay indented.
        outer.add_row(Padding(Text(item.reason, style="dim italic"), (0, 0, 0, 6)))
        outer.add_row("")
    return outer


def _facts_table(facts) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right", no_wrap=True, min_width=24)
    table.add_column(overflow="fold")
    for key, value in facts.describe().items():
        table.add_row(key, Text(str(value)))
    return table


def scores_bar(verdict: Verdict, width: int = 24) -> Table:
    """Final tally, as a small bar chart."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", no_wrap=True, width=6)
    table.add_column(no_wrap=True)
    table.add_column(justify="right", style="dim", width=5)
    top = max((v for v in verdict.scores.values()), default=1) or 1
    for cls, score in sorted(verdict.scores.items(), key=lambda kv: -kv[1]):
        filled = max(0, round(score / top * width)) if score > 0 else 0
        bar = Text("█" * filled, style=CLASS_STYLE.get(cls, "white"))
        if score < 0:
            bar = Text("▏negative", style="red dim")
        table.add_row(Text(str(cls), style=CLASS_STYLE.get(cls, "white")), bar, str(score))
    return table
