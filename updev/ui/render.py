"""Rich rendering: tables, trees, detail panels, issue reports.

All the styling decisions live here so the CLI stays about behaviour. Nothing
in this module talks to hardware — it only ever consumes ScanResult.
"""

from __future__ import annotations

from typing import Any, Iterable

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from ..core.model import (
    Device,
    Kind,
    ScanResult,
    Severity,
    Status,
    severity_weight,
    status_weight,
)

# --------------------------------------------------------------------------
# palette
# --------------------------------------------------------------------------

STATUS_STYLE: dict[Status, str] = {
    Status.ONLINE: "bold green",
    Status.IDLE: "dim white",
    Status.DEGRADED: "bold yellow",
    Status.DISABLED: "bold blue",
    Status.ABSENT: "magenta",
    Status.ERROR: "bold red",
    Status.UNKNOWN: "dim",
}

STATUS_DOT: dict[Status, str] = {
    Status.ONLINE: "●",
    Status.IDLE: "○",
    Status.DEGRADED: "◐",
    Status.DISABLED: "◌",
    Status.ABSENT: "◍",
    Status.ERROR: "✖",
    Status.UNKNOWN: "?",
}

SEVERITY_STYLE: dict[Severity, str] = {
    Severity.ERROR: "bold red",
    Severity.WARN: "bold yellow",
    Severity.INFO: "cyan",
}

SEVERITY_MARK: dict[Severity, str] = {
    Severity.ERROR: "✖",
    Severity.WARN: "▲",
    Severity.INFO: "i",
}

KIND_STYLE: dict[Kind, str] = {
    Kind.HOST: "bold magenta",
    Kind.SOC: "bold magenta",
    Kind.POWER: "bold yellow",
    Kind.THERMAL: "yellow",
    Kind.STORAGE: "bright_blue",
    Kind.USB: "bright_cyan",
    Kind.I2C: "bright_green",
    Kind.SPI: "green",
    Kind.SERIAL: "bright_yellow",
    Kind.CAMERA: "bright_magenta",
    Kind.GPIO: "cyan",
    Kind.NET_IFACE: "blue",
    Kind.NET_HOST: "bright_blue",
    Kind.BLUETOOTH: "blue",
    Kind.DISPLAY: "magenta",
    Kind.UNKNOWN: "white",
}

KIND_TITLE: dict[Kind, str] = {
    Kind.HOST: "Host",
    Kind.SOC: "SoC",
    Kind.POWER: "Power",
    Kind.THERMAL: "Thermal",
    Kind.STORAGE: "Storage",
    Kind.USB: "USB",
    Kind.I2C: "I2C",
    Kind.SPI: "SPI",
    Kind.SERIAL: "Serial / UART",
    Kind.CAMERA: "Camera",
    Kind.GPIO: "GPIO",
    Kind.NET_IFACE: "Network interfaces",
    Kind.NET_HOST: "LAN neighbours",
    Kind.BLUETOOTH: "Bluetooth",
    Kind.DISPLAY: "Display",
    Kind.UNKNOWN: "Other",
}

# The order sections appear in `updev scan`.
KIND_ORDER: tuple[Kind, ...] = (
    Kind.HOST, Kind.SOC, Kind.POWER, Kind.THERMAL, Kind.STORAGE, Kind.USB,
    Kind.I2C, Kind.SPI, Kind.SERIAL, Kind.CAMERA, Kind.GPIO,
    Kind.NET_IFACE, Kind.NET_HOST, Kind.BLUETOOTH, Kind.DISPLAY, Kind.UNKNOWN,
)


def status_text(status: Status, label: bool = False) -> Text:
    style = STATUS_STYLE.get(status, "white")
    dot = STATUS_DOT.get(status, "?")
    return Text(f"{dot} {status}" if label else dot, style=style)


def kind_text(kind: Kind) -> Text:
    return Text(str(kind), style=KIND_STYLE.get(kind, "white"))


# --------------------------------------------------------------------------
# main table
# --------------------------------------------------------------------------

def device_table(
    devices: Iterable[Device],
    title: str | None = None,
    show_kind: bool = True,
    show_node: bool = False,
    compact: bool = False,
) -> Table:
    """`compact` drops the address column and narrows the name, for side panels
    where the summary is what you're actually reading."""
    table = Table(
        box=box.SIMPLE_HEAD,
        title=title,
        title_style="bold",
        title_justify="left",
        header_style="dim bold",
        expand=True,
        pad_edge=False,
        show_edge=False,
    )
    # Fixed widths, not max_widths: every section must line up with the next,
    # and rich sizes each table independently otherwise.
    table.add_column("", width=1, no_wrap=True)
    if show_kind:
        table.add_column("kind", width=9, no_wrap=True, overflow="ellipsis")
    table.add_column("device", style="bold", width=24 if compact else 32,
                     no_wrap=True, overflow="ellipsis")
    if not compact:
        table.add_column("address", style="dim", width=14, no_wrap=True, overflow="ellipsis")
    if show_node:
        table.add_column("node", style="dim", width=18, no_wrap=True, overflow="ellipsis")
    table.add_column("summary", overflow="ellipsis", no_wrap=True, ratio=1)

    for dev in devices:
        marks = _issue_marks(dev)
        name = Text(dev.label, style=STATUS_STYLE.get(dev.status, "white"))
        if marks:
            name.append(" ")
            name.append(marks)
        row: list[RenderableType] = [status_text(dev.status)]
        if show_kind:
            row.append(kind_text(dev.kind))
        row.append(name)
        if not compact:
            row.append(dev.address or dev.uid.split(":", 1)[-1])
        if show_node:
            row.append(dev.node)
        row.append(Text(dev.summary, style="" if dev.status != Status.IDLE else "dim"))
        table.add_row(*row)
    return table


def _issue_marks(dev: Device) -> Text:
    out = Text()
    for sev in (Severity.ERROR, Severity.WARN, Severity.INFO):
        n = sum(1 for i in dev.issues if i.severity == sev)
        if n:
            out.append(SEVERITY_MARK[sev] * min(n, 3), style=SEVERITY_STYLE[sev])
    return out


def grouped_report(result: ScanResult, show_node: bool = False) -> RenderableType:
    """The default `updev scan` body: one section per device kind."""
    by_kind = result.by_kind()
    blocks: list[RenderableType] = []
    for kind in KIND_ORDER:
        devices = by_kind.get(kind)
        if not devices:
            continue
        devices = sorted(devices, key=lambda d: (status_weight(d.status), d.uid))
        heading = Text(f"{KIND_TITLE.get(kind, str(kind))}  ", style=KIND_STYLE.get(kind, "white"))
        heading.append(f"({len(devices)})", style="dim")
        blocks.append(heading)
        blocks.append(device_table(devices, show_kind=False, show_node=show_node))
    if not blocks:
        return Text("no devices found", style="dim italic")
    return Group(*blocks)


# --------------------------------------------------------------------------
# tree
# --------------------------------------------------------------------------

def device_tree(result: ScanResult) -> Tree:
    """Physical topology, following the parent links backends set."""
    by_uid = result.by_uid()
    children: dict[str | None, list[Device]] = {}
    for dev in result.devices:
        parent = dev.parent if dev.parent in by_uid else None
        children.setdefault(parent, []).append(dev)

    root_dev = by_uid.get("host:board")
    root_label = Text(root_dev.label if root_dev else "system", style="bold magenta")
    tree = Tree(root_label, guide_style="dim")

    def node_label(dev: Device) -> Text:
        label = Text()
        label.append(STATUS_DOT.get(dev.status, "?") + " ", style=STATUS_STYLE.get(dev.status))
        label.append(dev.label, style="bold")
        if dev.address:
            label.append(f"  {dev.address}", style="dim")
        if dev.summary:
            label.append(f"  {dev.summary}", style="dim italic")
        marks = _issue_marks(dev)
        if marks:
            label.append("  ")
            label.append(marks)
        return label

    def attach(parent_uid: str | None, branch: Tree) -> None:
        kids = sorted(
            children.get(parent_uid, []),
            key=lambda d: (KIND_ORDER.index(d.kind) if d.kind in KIND_ORDER else 99, d.uid),
        )
        for kid in kids:
            if kid.uid == "host:board":
                continue
            sub = branch.add(node_label(kid))
            attach(kid.uid, sub)

    attach("host:board", tree)
    # Anything whose parent didn't resolve gets parked at the root.
    orphans = [d for d in children.get(None, []) if d.uid != "host:board"]
    if orphans:
        misc = tree.add(Text("unparented", style="dim italic"))
        for dev in sorted(orphans, key=lambda d: d.uid):
            misc.add(node_label(dev))
    return tree


# --------------------------------------------------------------------------
# detail
# --------------------------------------------------------------------------

def device_detail(dev: Device) -> Panel:
    body: list[RenderableType] = []

    head = Table.grid(padding=(0, 2))
    head.add_column(style="dim", justify="right", no_wrap=True)
    head.add_column()
    fields = [
        ("uid", dev.uid),
        ("status", None),
        ("kind", str(dev.kind)),
        ("bus", dev.bus),
        ("address", dev.address),
        ("node", dev.node),
        ("vendor", dev.vendor),
        ("model", dev.model),
        ("serial", dev.serial),
        ("driver", dev.driver),
        ("parent", dev.parent or ""),
        ("tags", ", ".join(dev.tags)),
    ]
    for key, value in fields:
        if key == "status":
            head.add_row("status", status_text(dev.status, label=True))
            continue
        if value:
            head.add_row(key, Text(str(value)))
    body.append(head)

    if dev.detail:
        body.append(Text("\ndetail", style="bold dim"))
        body.append(_kv_table(dev.detail))

    if dev.metrics:
        body.append(Text("\nmetrics", style="bold dim"))
        metrics = Table.grid(padding=(0, 2))
        metrics.add_column(style="dim", justify="right", no_wrap=True)
        metrics.add_column(style="bold cyan")
        for key, value in sorted(dev.metrics.items()):
            metrics.add_row(key, f"{value:,.3f}".rstrip("0").rstrip("."))
        body.append(metrics)

    if dev.issues:
        body.append(Text("\nissues", style="bold dim"))
        body.append(issue_list(dev.issues))

    if dev.actions:
        body.append(Text("\nactions", style="bold dim"))
        actions = Table.grid(padding=(0, 2))
        actions.add_column(style="bold", no_wrap=True)
        actions.add_column(style="dim")
        actions.add_column(style="cyan")
        for action in dev.actions:
            actions.add_row(action.name, action.description, action.command)
        body.append(actions)

    title = Text(dev.label, style="bold")
    title.append(f"  {dev.uid}", style="dim")
    return Panel(
        Group(*body),
        title=title,
        title_align="left",
        border_style=STATUS_STYLE.get(dev.status, "white"),
        box=box.ROUNDED,
    )


def _kv_table(data: dict[str, Any], indent: int = 0) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right", no_wrap=True, min_width=14)
    table.add_column(overflow="fold")
    for key, value in data.items():
        table.add_row(key.replace("_", " "), _format_value(value))
    return table


def _format_value(value: Any) -> RenderableType:
    if isinstance(value, dict):
        inner = Table.grid(padding=(0, 1))
        inner.add_column(style="cyan", no_wrap=True)
        inner.add_column()
        for k, v in value.items():
            inner.add_row(str(k), Text(str(v)))
        return inner
    if isinstance(value, (list, tuple)):
        if not value:
            return Text("-", style="dim")
        if all(not isinstance(v, (dict, list)) for v in value):
            return Text(", ".join(str(v) for v in value))
        inner = Table.grid(padding=(0, 1))
        inner.add_column()
        for item in value:
            inner.add_row(_format_value(item))
        return inner
    if isinstance(value, bool):
        return Text("yes" if value else "no", style="green" if value else "dim")
    return Text(str(value))


# --------------------------------------------------------------------------
# issues / doctor
# --------------------------------------------------------------------------

def issue_list(issues) -> Table:
    table = Table.grid(padding=(0, 1))
    table.add_column(width=1, no_wrap=True)
    table.add_column(overflow="fold")
    for issue in sorted(issues, key=lambda i: severity_weight(i.severity)):
        block = Text(issue.message, style=SEVERITY_STYLE.get(issue.severity, "white"))
        if issue.doc:
            block.append(f"\n{issue.doc}", style="dim italic")
        if issue.fix:
            block.append("\n$ ", style="dim")
            block.append(issue.fix, style="bold cyan")
        table.add_row(
            Text(SEVERITY_MARK.get(issue.severity, "-"), style=SEVERITY_STYLE.get(issue.severity)),
            block,
        )
    return table


def doctor_report(result: ScanResult) -> RenderableType:
    pairs = result.issues()
    if not pairs:
        return Panel(
            Text("Nothing to report — every device probed clean.", style="bold green"),
            border_style="green",
            box=box.ROUNDED,
            title="doctor",
            title_align="left",
        )

    blocks: list[RenderableType] = []
    for severity in (Severity.ERROR, Severity.WARN, Severity.INFO):
        group = [(d, i) for d, i in pairs if i.severity == severity]
        if not group:
            continue
        heading = Text(
            f"{SEVERITY_MARK[severity]} {severity.upper()}  ({len(group)})",
            style=SEVERITY_STYLE[severity],
        )
        blocks.append(heading)
        table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, expand=True,
                      header_style="dim")
        table.add_column("device", style="bold", no_wrap=True, max_width=26)
        table.add_column("problem", overflow="fold")
        for dev, issue in group:
            cell = Text(issue.message)
            if issue.doc:
                cell.append(f"\n{issue.doc}", style="dim italic")
            if issue.fix:
                cell.append("\n$ ", style="dim")
                cell.append(issue.fix, style="bold cyan")
            table.add_row(Text(dev.label, style=KIND_STYLE.get(dev.kind, "white")), cell)
        blocks.append(table)

    counts = {
        sev: sum(1 for _, i in pairs if i.severity == sev)
        for sev in (Severity.ERROR, Severity.WARN, Severity.INFO)
    }
    subtitle = Text()
    for sev, n in counts.items():
        if n:
            subtitle.append(f" {n} {sev} ", style=SEVERITY_STYLE[sev])
    border = (
        "red" if counts[Severity.ERROR]
        else "yellow" if counts[Severity.WARN]
        else "cyan"
    )
    return Panel(
        Group(*blocks),
        title="doctor",
        title_align="left",
        subtitle=subtitle,
        subtitle_align="right",
        border_style=border,
        box=box.ROUNDED,
    )


# --------------------------------------------------------------------------
# summary / vitals
# --------------------------------------------------------------------------

def change_text(change) -> Text:
    """One hotplug event as a single styled line."""
    from ..core.changes import ChangeKind

    dev = change.device
    out = Text()
    if change.kind == ChangeKind.ADDED:
        out.append("+ ", style="bold green")
        out.append(f"{KIND_TITLE.get(dev.kind, str(dev.kind))} ",
                   style=KIND_STYLE.get(dev.kind, "white"))
        out.append(dev.label, style="bold")
        if dev.summary:
            out.append(f" — {dev.summary}", style="dim")
    elif change.kind == ChangeKind.REMOVED:
        out.append("- ", style="bold red")
        out.append(f"{KIND_TITLE.get(dev.kind, str(dev.kind))} ",
                   style=KIND_STYLE.get(dev.kind, "white"))
        out.append(dev.label, style="bold")
        out.append(" disappeared", style="dim")
    else:
        out.append("~ ", style="bold yellow")
        out.append(f"{dev.label} ", style="bold")
        out.append(str(change.previous_status),
                   style=STATUS_STYLE.get(change.previous_status, "dim"))
        out.append(" → ", style="dim")
        out.append(str(dev.status), style=STATUS_STYLE.get(dev.status, "dim"))
    return out


def status_counts(result: ScanResult) -> dict[Status, int]:
    counts: dict[Status, int] = {}
    for dev in result.devices:
        counts[dev.status] = counts.get(dev.status, 0) + 1
    return counts


def summary_bar(result: ScanResult) -> RenderableType:
    counts = status_counts(result)
    line = Text()
    line.append(f"{len(result.devices)} devices", style="bold")
    line.append("   ")
    for status in (
        Status.ONLINE, Status.IDLE, Status.DEGRADED,
        Status.DISABLED, Status.ERROR, Status.ABSENT, Status.UNKNOWN,
    ):
        n = counts.get(status, 0)
        if not n:
            continue
        line.append(f"{STATUS_DOT[status]} ", style=STATUS_STYLE[status])
        line.append(f"{n} {status}  ", style=STATUS_STYLE[status])

    pairs = result.issues()
    if pairs:
        line.append("  │  ", style="dim")
        for sev in (Severity.ERROR, Severity.WARN, Severity.INFO):
            n = sum(1 for _, i in pairs if i.severity == sev)
            if n:
                line.append(f"{SEVERITY_MARK[sev]}{n} ", style=SEVERITY_STYLE[sev])
    line.append(f"  │  scanned in {result.duration:.2f}s", style="dim")
    return line


def vitals_panel(result: ScanResult) -> Panel:
    """The numbers you want at a glance: temperature, power, load, memory."""
    by_uid = result.by_uid()
    grid = Table.grid(padding=(0, 3))
    grid.add_column(style="dim", justify="right", no_wrap=True)
    grid.add_column(no_wrap=True)

    soc = by_uid.get("host:soc")
    if soc:
        temp = soc.metrics.get("temp_c")
        if temp is not None:
            grid.add_row("SoC temp", _gauge(temp, 30, 85, f"{temp:.1f}°C", invert=True))
        load = soc.metrics.get("load_pct")
        if load is not None:
            grid.add_row("CPU load", _gauge(load, 0, 100, soc.detail.get("load", ""), invert=True))
        if soc.detail.get("frequency"):
            grid.add_row("clock", Text(soc.detail["frequency"], style="cyan"))

    power = by_uid.get("host:power")
    if power:
        watts = power.metrics.get("power_w")
        if watts is not None:
            grid.add_row("power draw", _gauge(watts, 2, 12, f"{watts:.2f} W", invert=True))
        volts = power.metrics.get("input_v")
        if volts is not None:
            style = "green" if volts >= 4.9 else "yellow" if volts >= 4.75 else "bold red"
            grid.add_row("5V input", Text(f"{volts:.3f} V", style=style))

    mem = by_uid.get("host:memory")
    if mem:
        pct = mem.metrics.get("used_pct")
        if pct is not None:
            grid.add_row("memory", _gauge(pct, 0, 100, mem.summary, invert=True))

    root = next(
        (d for d in result.devices if d.detail.get("mounted at") == "/"), None
    )
    if root:
        pct = root.metrics.get("fs_used_pct")
        if pct is not None:
            grid.add_row("rootfs", _gauge(pct, 0, 100, root.detail.get("filesystem_usage", ""),
                                          invert=True))

    wifi = next(
        (d for d in result.devices if "wireless" in d.tags and d.metrics.get("signal_dbm")), None
    )
    if wifi:
        dbm = wifi.metrics["signal_dbm"]
        grid.add_row("wi-fi", _gauge(dbm, -90, -30, wifi.detail.get("signal_dbm", "")))

    return Panel(grid, title="vitals", title_align="left", border_style="cyan", box=box.ROUNDED)


def _gauge(value: float, lo: float, hi: float, label: str, width: int = 18,
           invert: bool = False) -> Text:
    """A tiny inline bar. `invert` means high values are bad."""
    span = hi - lo or 1
    frac = max(0.0, min(1.0, (value - lo) / span))
    filled = round(frac * width)
    if invert:
        style = "green" if frac < 0.6 else "yellow" if frac < 0.85 else "bold red"
    else:
        style = "bold red" if frac < 0.2 else "yellow" if frac < 0.45 else "green"
    bar = Text("█" * filled, style=style)
    bar.append("░" * (width - filled), style="dim")
    bar.append(f"  {label}", style=style)
    return bar


def backend_table(result: ScanResult) -> Table:
    table = Table(box=box.SIMPLE, header_style="dim bold", expand=True,
                  show_edge=False, pad_edge=False)
    table.add_column("", width=1)
    table.add_column("backend", style="bold", width=11, no_wrap=True)
    table.add_column("devices", justify="right", width=7, no_wrap=True)
    table.add_column("time", justify="right", style="dim", width=7, no_wrap=True)
    table.add_column("note", style="dim", overflow="fold", ratio=1)
    for rep in result.reports:
        if not rep.available:
            mark, note = Text("○", style="dim"), rep.reason
        elif not rep.ok:
            mark, note = Text("✖", style="bold red"), rep.error
        else:
            mark, note = Text("●", style="green"), ""
        table.add_row(
            mark, rep.name,
            str(rep.count) if rep.available and rep.ok else "-",
            f"{rep.duration * 1000:.0f}ms" if rep.duration else "-",
            note,
        )
    return table


def header_panel(result: ScanResult, subtitle: str = "") -> Panel:
    by_uid = result.by_uid()
    board = by_uid.get("host:board")
    title = Text()
    title.append("updev", style="bold bright_magenta")
    title.append("  ", style="")
    if board:
        title.append(board.label, style="bold")
        host = board.detail.get("hostname")
        if host:
            title.append(f"  @{host}", style="cyan")
    body = Text(board.summary if board else "", style="dim")
    if subtitle:
        body.append(f"\n{subtitle}", style="dim italic")
    return Panel(body, title=title, title_align="left", border_style="bright_magenta",
                 box=box.ROUNDED)


def make_console(no_color: bool = False, width: int | None = None) -> Console:
    return Console(no_color=no_color, width=width, soft_wrap=False, highlight=False)
