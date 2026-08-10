"""The live dashboard behind `updev watch`.

Rescans on an interval, keeps a short history for sparklines, and — the part
that earns its keep — diffs consecutive scans so you can watch a device appear
the moment you plug it in.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from rich import box
from rich.align import Align
from rich.console import Console, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..core.changes import diff_devices
from ..core.model import Device, ScanResult, Severity, Status, status_weight
from ..core.registry import ProbeContext, Scanner
from .render import (
    SEVERITY_MARK,
    SEVERITY_STYLE,
    STATUS_DOT,
    STATUS_STYLE,
    change_text,
    device_table,
    status_counts,
    vitals_panel,
)

_SPARK = "▁▂▃▄▅▆▇█"


@dataclass
class Event:
    when: float
    text: Text


@dataclass
class History:
    """Rolling numeric series, keyed by "<uid>.<metric>"."""

    depth: int = 60
    series: dict[str, deque[float]] = field(default_factory=dict)

    def push(self, result: ScanResult) -> None:
        for dev in result.devices:
            for key, value in dev.metrics.items():
                bucket = self.series.setdefault(f"{dev.uid}.{key}", deque(maxlen=self.depth))
                bucket.append(value)

    def spark(self, key: str, width: int = 24) -> Text:
        values = list(self.series.get(key, ()))[-width:]
        if len(values) < 2:
            return Text("—" * 3, style="dim")
        lo, hi = min(values), max(values)
        span = (hi - lo) or 1.0
        out = Text()
        for v in values:
            idx = int((v - lo) / span * (len(_SPARK) - 1))
            out.append(_SPARK[idx], style="cyan")
        return out


class Dashboard:
    """Owns the scan loop, the history and the layout."""

    def __init__(
        self,
        scanner: Scanner,
        ctx: ProbeContext,
        console: Console,
        interval: float = 2.0,
        max_events: int = 40,
    ) -> None:
        self.scanner = scanner
        self.ctx = ctx
        self.console = console
        self.interval = max(0.5, interval)
        self.history = History()
        self.events: deque[Event] = deque(maxlen=max_events)
        self.previous: dict[str, Device] = {}
        self.result: ScanResult | None = None
        self.scans = 0
        self.started = time.time()

    # -- loop --------------------------------------------------------------

    def run(self, iterations: int | None = None) -> None:
        with Live(
            self._render_placeholder(),
            console=self.console,
            screen=True,
            refresh_per_second=8,
            transient=False,
        ) as live:
            try:
                while iterations is None or self.scans < iterations:
                    self.step()
                    live.update(self.render())
                    # Sleep in slices so Ctrl-C lands promptly.
                    deadline = time.time() + self.interval
                    while time.time() < deadline:
                        time.sleep(min(0.1, max(0.0, deadline - time.time())))
            except KeyboardInterrupt:
                pass

    def step(self) -> ScanResult:
        result = self.scanner.scan(self.ctx)
        self.scans += 1
        self.history.push(result)
        self._diff(result)
        self.result = result
        self.previous = result.by_uid()
        return result

    def _diff(self, result: ScanResult) -> None:
        for change in diff_devices(self.previous, result.by_uid()):
            self.events.append(Event(change.when, change_text(change)))

    # -- rendering ---------------------------------------------------------

    def _render_placeholder(self) -> RenderableType:
        return Align.center(Text("probing hardware…", style="bold cyan"), vertical="middle")

    def render(self) -> RenderableType:
        result = self.result
        if result is None:
            return self._render_placeholder()

        layout = Layout()
        layout.split_column(
            Layout(self._header(result), name="header", size=3),
            Layout(name="body"),
            Layout(self._footer(result), name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="left", ratio=3),
            Layout(name="right", ratio=2),
        )
        layout["body"]["left"].split_column(
            Layout(vitals_panel(result), name="vitals", size=10),
            Layout(self._devices(result), name="devices"),
        )
        layout["body"]["right"].split_column(
            Layout(self._trends(result), name="trends", size=10),
            Layout(self._activity(), name="activity"),
        )
        return layout

    def _header(self, result: ScanResult) -> Panel:
        board = result.by_uid().get("host:board")
        left = Text()
        left.append("updev ", style="bold bright_magenta")
        left.append("live", style="bright_magenta")
        left.append("   ")
        if board:
            left.append(board.label, style="bold")
            host = board.detail.get("hostname")
            if host:
                left.append(f" @{host}", style="cyan")
            up = board.detail.get("uptime")
            if up:
                left.append(f"   up {up}", style="dim")

        right = Text()
        right.append(datetime.now().strftime("%H:%M:%S"), style="bold")
        right.append(f"   scan #{self.scans}", style="dim")
        right.append(f"   {result.duration:.2f}s", style="dim")
        right.append(f"   every {self.interval:g}s", style="dim")

        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        grid.add_row(left, right)
        return Panel(grid, box=box.ROUNDED, border_style="bright_magenta")

    def _devices(self, result: ScanResult) -> Panel:
        # Lead with anything unhealthy, then fill the rest with everything else.
        interesting = [
            d for d in result.devices
            if d.status in (Status.ERROR, Status.DEGRADED, Status.DISABLED) or d.issues
        ]
        shown = {d.uid for d in interesting}
        rest = [d for d in result.devices if d.uid not in shown]
        # Individually claimed GPIO lines never change and would otherwise fill
        # the panel, so they sink to the bottom.
        rest.sort(key=lambda d: ("claimed" in d.tags, status_weight(d.status),
                                 str(d.kind), d.uid))
        devices = interesting + rest
        height = max(6, self.console.size.height - 20)
        table = device_table(devices[:height], show_kind=True, compact=True)
        hidden = max(0, len(devices) - height)
        title = Text("devices", style="bold")
        if hidden:
            title.append(f"  (+{hidden} more)", style="dim")
        return Panel(table, title=title, title_align="left", border_style="blue",
                     box=box.ROUNDED)

    def _trends(self, result: ScanResult) -> Panel:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", justify="right", no_wrap=True)
        grid.add_column(no_wrap=True)
        grid.add_column(style="bold cyan", justify="right", no_wrap=True)

        watched = [
            ("host:soc.temp_c", "temp", "{:.1f}°C"),
            ("host:power.power_w", "power", "{:.2f}W"),
            ("host:soc.load_pct", "cpu", "{:.0f}%"),
            ("host:soc.freq_hz", "clock", "{:.0f}"),
            ("host:memory.used_pct", "memory", "{:.0f}%"),
        ]
        for dev in result.devices:
            if "wireless" in dev.tags and "signal_dbm" in dev.metrics:
                watched.append((f"{dev.uid}.signal_dbm", "wi-fi", "{:.0f}dBm"))
                break
        for dev in result.devices:
            if dev.metrics.get("rpm"):
                watched.append((f"{dev.uid}.rpm", "fan", "{:.0f}rpm"))
                break

        for key, label, fmt in watched:
            series = self.history.series.get(key)
            if not series:
                continue
            current = series[-1]
            if label == "clock":
                current_text = f"{current / 1e9:.2f}GHz"
            else:
                current_text = fmt.format(current)
            grid.add_row(label, self.history.spark(key), current_text)

        if not grid.row_count:
            grid.add_row("", Text("collecting…", style="dim italic"), "")
        return Panel(grid, title="trends", title_align="left", border_style="cyan",
                     box=box.ROUNDED)

    def _activity(self) -> Panel:
        if not self.events:
            body: RenderableType = Align.center(
                Text("no changes yet — plug something in", style="dim italic"),
                vertical="middle",
            )
        else:
            grid = Table.grid(padding=(0, 1))
            grid.add_column(style="dim", no_wrap=True)
            grid.add_column(overflow="ellipsis")
            for event in list(self.events)[-20:][::-1]:
                grid.add_row(
                    datetime.fromtimestamp(event.when).strftime("%H:%M:%S"), event.text
                )
            body = grid
        return Panel(body, title="activity", title_align="left", border_style="green",
                     box=box.ROUNDED)

    def _footer(self, result: ScanResult) -> Panel:
        counts = status_counts(result)
        line = Text()
        line.append(f"{len(result.devices)} devices   ", style="bold")
        for status in (Status.ONLINE, Status.IDLE, Status.DEGRADED, Status.DISABLED,
                       Status.ERROR):
            n = counts.get(status, 0)
            if n:
                line.append(f"{STATUS_DOT[status]}{n} ", style=STATUS_STYLE[status])
                line.append(f"{status}  ", style=STATUS_STYLE[status] + " dim")

        pairs = result.issues()
        if pairs:
            line.append("  │  ", style="dim")
            for sev in (Severity.ERROR, Severity.WARN, Severity.INFO):
                n = sum(1 for _, i in pairs if i.severity == sev)
                if n:
                    line.append(f"{SEVERITY_MARK[sev]}{n} ", style=SEVERITY_STYLE[sev])
            worst = pairs[0]
            line.append(f" {worst[0].label}: {worst[1].message}", style="dim italic")

        keys = Text("ctrl-c to quit", style="dim")
        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        grid.add_row(line, keys)
        return Panel(grid, box=box.ROUNDED, border_style="dim")
