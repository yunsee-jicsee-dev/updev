"""The `updev` command line.

Read-only by default. Anything that drives a bus, transmits, or writes to a
chip is behind its own subcommand and — where a mistake could brick hardware —
behind an explicit `--yes`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import click
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .backends import backend_names
from .core.model import Kind, ScanResult, Severity
from .core.registry import ProbeContext, Scanner, build_scanner
from .ui import render
from .ui.dash import Dashboard

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}


class DeviceFirstGroup(click.Group):
    """Also accept `updev <device> <verb>`, not just `updev <verb> <device>`.

    Typing the thing you are holding before the thing you want to do to it is
    how people describe it out loud ("this floppy drive — open its editor"),
    and the rewrite is unambiguous: it only fires when the first word is not a
    command and the second one is a verb that takes a device.
    """

    #: Verbs whose first argument is a device.
    DEVICE_VERBS = frozenset({"gui", "tools", "show"})

    def resolve_command(self, ctx, args):
        if (
            len(args) >= 2
            and args[0] not in self.commands
            and not args[0].startswith("-")
            and args[1] in self.DEVICE_VERBS
        ):
            command = self.commands[args[1]]
            return args[1], command, [args[0], *args[2:]]
        return super().resolve_command(ctx, args)


class State:
    """Shared plumbing hung off the click context."""

    def __init__(self) -> None:
        self.console: Console = Console()
        self.scanner: Scanner = build_scanner()
        self.ctx = ProbeContext()
        self.as_json = False

    def scan(self, **overrides) -> ScanResult:
        ctx = self.ctx
        for key, value in overrides.items():
            setattr(ctx, key, value)
        if self.as_json:
            return self.scanner.scan(ctx)
        with self.console.status("[cyan]probing hardware…", spinner="dots"):
            return self.scanner.scan(ctx)

    def emit(self, payload: dict) -> None:
        click.echo(json.dumps(payload, indent=2, default=str, ensure_ascii=False))


pass_state = click.make_pass_decorator(State, ensure=True)


# ==========================================================================
# root
# ==========================================================================

@click.group(cls=DeviceFirstGroup, context_settings=CONTEXT_SETTINGS,
             invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--no-color", is_flag=True, help="Disable ANSI colour.")
@click.option("--deep", is_flag=True, help="Slower, more thorough probing (bus scans, LAN sweep).")
@click.option("--timeout", type=float, default=20.0, show_default=True,
              help="Per-backend time budget in seconds.")
@click.option("-b", "--backend", "backends", multiple=True,
              help="Only run these backends (repeatable).")
@click.option("-x", "--exclude", "excluded", multiple=True,
              help="Skip these backends (repeatable).")
@click.option("--width", type=int, default=None, help="Force output width.")
@click.version_option("1.0.0", "-V", "--version", prog_name="updev")
@click.pass_context
def cli(ctx, as_json, no_color, deep, timeout, backends, excluded, width):
    """updev — one device manager for the whole board.

    LAN, USB, I2C, SPI, serial, cameras, GPIO, storage, Bluetooth, NFC and the
    Raspberry Pi itself, in one place.

    A device can come first: `updev 3-2 gui`, `updev sda tools`, `updev eth0
    show` all work, as does the usual `updev gui 3-2`.
    """
    state = State()
    state.console = render.make_console(no_color=no_color, width=width)
    state.as_json = as_json
    state.ctx = ProbeContext(
        deep=deep,
        timeout=timeout,
        include=frozenset(backends),
        exclude=frozenset(excluded),
    )
    ctx.obj = state
    if ctx.invoked_subcommand is None:
        ctx.invoke(scan)


# ==========================================================================
# scan / tree / show / doctor
# ==========================================================================

@cli.command("help")
@click.argument("topic", required=False)
@pass_state
def help_command(state: State, topic):
    """Explain a topic properly — what the classifications mean, and why.

    `updev help` lists the topics; `updev help usb` opens one. For the flags
    of an individual command, use `--help` on that command instead.
    """
    from .helptopics import QUICKSTART, TOPICS, find_topic

    if topic:
        found = find_topic(topic)
        if found is None:
            if state.as_json:
                state.emit({"error": f"no topic named {topic!r}",
                            "topics": [t.name for t in TOPICS]})
            else:
                state.console.print(f"[red]no topic named[/] [bold]{topic}[/]")
                state.console.print("[dim]available: " +
                                    ", ".join(t.name for t in TOPICS) + "[/]")
            raise SystemExit(1)
        if state.as_json:
            state.emit(found.as_dict())
            return
        state.console.print(_topic_panel(found))
        return

    if state.as_json:
        state.emit({
            "quickstart": [{"command": c, "description": d} for c, d in QUICKSTART],
            "topics": [t.as_dict() for t in TOPICS],
        })
        return

    quick = Table.grid(padding=(0, 3))
    quick.add_column(style="bold cyan", no_wrap=True, width=18)
    quick.add_column(style="dim")
    for command, description in QUICKSTART:
        quick.add_row(command, description)

    index = Table.grid(padding=(0, 3))
    index.add_column(style="bold", no_wrap=True, width=12)
    index.add_column(no_wrap=True, width=26)
    index.add_column(style="dim")
    for entry in TOPICS:
        index.add_row(f"help {entry.name}", entry.title, entry.blurb)

    banner = Text()
    banner.append("updev", style="bold bright_magenta")
    banner.append("  —  보드에 붙은 모든 것을 한 곳에서", style="dim")

    state.console.print(Panel(
        Group(
            banner,
            Text("\n바로 써보기", style="bold dim"),
            quick,
            Text("\n주제별 설명", style="bold dim"),
            index,
            Text("\n개별 명령의 플래그는 ", style="dim") +
            Text("updev <명령> --help", style="bold cyan"),
        ),
        border_style="bright_magenta", box=box.ROUNDED,
    ))


def _topic_panel(topic):
    body = [Text(topic.blurb, style="dim italic")]
    if topic.body:
        body.append(Text())
        body.append(Text(topic.body))
    if topic.table:
        headers, rows = topic.table
        table = Table(box=box.SIMPLE, show_edge=False, header_style="dim bold",
                      expand=True)
        table.add_column(headers[0], style="bold", no_wrap=True, width=22)
        for header in headers[1:-1]:
            table.add_column(header, no_wrap=True, width=24)
        table.add_column(headers[-1], overflow="fold", ratio=1, style="dim")
        for row in rows:
            table.add_row(*row)
        body.append(Text())
        body.append(table)
    if topic.commands:
        commands = Table.grid(padding=(0, 3))
        commands.add_column(style="bold cyan", no_wrap=True)
        commands.add_column(style="dim", overflow="fold")
        for command, description in topic.commands:
            commands.add_row(command, description)
        body.append(Text("\n명령", style="bold dim"))
        body.append(commands)
    return Panel(Group(*body), title=topic.title, title_align="left",
                 border_style="cyan", box=box.ROUNDED)


@cli.command()
@click.option("-k", "--kind", "kinds", multiple=True,
              help="Only show these kinds (usb, i2c, net-iface, …).")
@click.option("-s", "--status", "statuses", multiple=True,
              help="Only show these statuses (online, degraded, …).")
@click.option("-t", "--tag", "tags", multiple=True, help="Only show devices with this tag.")
@click.option("--nodes", is_flag=True, help="Include the /dev node column.")
@click.option("--quiet", is_flag=True, help="Table only — no header or backend report.")
@pass_state
def scan(state: State, kinds, statuses, tags, nodes, quiet):
    """Enumerate everything attached to this board."""
    result = state.scan()
    devices = _filter(result.devices, kinds, statuses, tags)

    if state.as_json:
        payload = result.as_dict()
        payload["devices"] = [d.as_dict() for d in devices]
        state.emit(payload)
        return

    filtered = ScanResult(devices=devices, reports=result.reports,
                          started=result.started, finished=result.finished)
    console = state.console
    if not quiet:
        console.print(render.header_panel(result))
    console.print(render.grouped_report(filtered, show_node=nodes))
    console.print()
    console.print(render.summary_bar(filtered))
    if not quiet:
        failed = [r for r in result.reports if r.available and not r.ok]
        if failed:
            console.print()
            console.print(Panel(render.backend_table(result), title="backends",
                                title_align="left", border_style="red", box=box.ROUNDED))
        pairs = result.issues()
        if pairs:
            worst = pairs[0]
            hint = Text("\nrun ", style="dim")
            hint.append("updev doctor", style="bold cyan")
            hint.append(f" — {len(pairs)} finding(s), worst: ", style="dim")
            hint.append(worst[1].message, style=render.SEVERITY_STYLE[worst[1].severity])
            console.print(hint)


@cli.command()
@pass_state
def tree(state: State):
    """Show the device topology as a tree."""
    result = state.scan()
    if state.as_json:
        state.emit(result.as_dict())
        return
    state.console.print(render.header_panel(result))
    state.console.print(render.device_tree(result))
    state.console.print()
    state.console.print(render.summary_bar(result))


@cli.command()
@click.argument("query")
@pass_state
def show(state: State, query):
    """Everything known about a device.

    QUERY matches a uid, name, address or /dev node — `updev show usb:4-1`,
    `updev show eth0`, `updev show nvme` all work.
    """
    result = state.scan()
    matches = result.find(query)
    if not matches:
        if state.as_json:
            state.emit({"query": query, "matches": []})
        else:
            state.console.print(f"[red]no device matches[/] [bold]{query}[/]")
            state.console.print("[dim]try `updev scan` to see what's available[/]")
        raise SystemExit(1)

    if state.as_json:
        state.emit({"query": query, "matches": [d.as_dict() for d in matches]})
        return

    if len(matches) > 8:
        state.console.print(
            f"[yellow]{len(matches)} devices match[/] [bold]{query}[/] — showing the list:"
        )
        state.console.print(render.device_table(matches))
        return
    for dev in matches:
        state.console.print(render.device_detail(dev))
        _print_tools(state, dev)


def _print_tools(state: State, dev) -> None:
    """The tools that fit this device, under its detail panel.

    Only for the kinds where `toolkit` knows more than the backend already
    said — a USB device's tools come from its role and storage class, which
    the backend never computed.
    """
    if dev.kind != Kind.USB or "root-hub" in dev.tags:
        return
    from .toolkit import annotate, recognize
    from .ui.zone import tool_lines

    recognition = recognize(dev)
    annotate(recognition.tools)
    if not recognition.tools:
        return
    state.console.print(Text("  할 수 있는 것", style="bold dim"))
    state.console.print(tool_lines(recognition.tools))
    state.console.print()


@cli.command()
@click.option("--fail-on", type=click.Choice(["error", "warn", "info", "never"]),
              default="error", show_default=True,
              help="Exit non-zero when a finding at this severity or worse exists.")
@pass_state
def doctor(state: State, fail_on):
    """Diagnose what's wrong, and print the command that fixes it."""
    result = state.scan()
    pairs = result.issues()

    if state.as_json:
        state.emit({
            "findings": [
                {"device": d.uid, "label": d.label, **i.as_dict()} for d, i in pairs
            ],
            "counts": {
                str(sev): sum(1 for _, i in pairs if i.severity == sev)
                for sev in (Severity.ERROR, Severity.WARN, Severity.INFO)
            },
        })
    else:
        state.console.print(render.header_panel(result))
        state.console.print(render.doctor_report(result))

    thresholds = {"error": (Severity.ERROR,),
                  "warn": (Severity.ERROR, Severity.WARN),
                  "info": (Severity.ERROR, Severity.WARN, Severity.INFO),
                  "never": ()}
    trip = thresholds[fail_on]
    if any(i.severity in trip for _, i in pairs):
        raise SystemExit(1)


@cli.command()
@click.option("-i", "--interval", type=float, default=2.0, show_default=True,
              help="Seconds between rescans.")
@click.option("-n", "--iterations", type=int, default=None,
              help="Stop after this many scans (useful for scripting).")
@pass_state
def watch(state: State, interval, iterations):
    """Live dashboard: vitals, trends, and hotplug events as they happen."""
    if state.as_json:
        raise click.UsageError("--json cannot be combined with watch")
    dash = Dashboard(state.scanner, state.ctx, state.console, interval=interval)
    dash.run(iterations=iterations)
    if dash.result:
        state.console.print(render.summary_bar(dash.result))


@cli.command()
@click.argument("path", type=click.Path(dir_okay=False, writable=True), required=False)
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json",
              show_default=True)
@pass_state
def export(state: State, path, fmt):
    """Write a full inventory to a file (or stdout)."""
    result = state.scan()
    if fmt == "json":
        text = json.dumps(result.as_dict(), indent=2, default=str, ensure_ascii=False)
    else:
        import csv
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["uid", "kind", "status", "name", "vendor", "model",
                         "bus", "address", "node", "driver", "summary", "issues"])
        for d in result.devices:
            writer.writerow([
                d.uid, d.kind, d.status, d.label, d.vendor, d.model, d.bus,
                d.address, d.node, d.driver, d.summary,
                " | ".join(f"{i.severity}: {i.message}" for i in d.issues),
            ])
        text = buf.getvalue()

    if path:
        Path(path).write_text(text, encoding="utf-8")
        state.console.print(
            f"[green]wrote[/] {len(result.devices)} devices → [bold]{path}[/]"
        )
    else:
        click.echo(text)


@cli.command()
@pass_state
def backends(state: State):
    """List the probe backends and whether they can run here."""
    result = state.scan()
    if state.as_json:
        state.emit({"backends": [r.as_dict() for r in result.reports]})
        return
    state.console.print(Panel(render.backend_table(result), title="backends",
                              title_align="left", border_style="cyan", box=box.ROUNDED))
    known = set(backend_names())
    ran = {r.name for r in result.reports}
    skipped = known - ran
    if skipped:
        state.console.print(
            f"[dim]not run (slow — needs --deep or -b): {', '.join(sorted(skipped))}[/]"
        )


@cli.command()
@click.option("-i", "--interval", type=float, default=0.5, show_default=True,
              help="Seconds between polls.")
@click.option("-d", "--duration", type=float, default=0.0,
              help="Stop after this many seconds (0 = until Ctrl-C).")
@click.option("-n", "--limit", type=int, default=0,
              help="Stop after this many events (0 = unlimited).")
@click.option("--all", "watch_all", is_flag=True,
              help="Poll every backend, not just the fast ones.")
@pass_state
def activity(state: State, interval, duration, limit, watch_all):
    """Live log of devices appearing, disappearing and changing state.

    Polls the fast backends several times a second, so plugging something in
    shows up almost immediately. With --json, emits one JSON object per event,
    which pipes into anything.
    """
    from .ui.zone import ACTIVITY_BACKENDS, EventMonitor

    ctx = state.ctx
    if not watch_all and not ctx.include:
        ctx.include = ACTIVITY_BACKENDS
    monitor = EventMonitor(state.scanner, ctx, state.console, interval=interval,
                           as_json=state.as_json)
    monitor.run(duration=duration, limit=limit)


# ==========================================================================
# USB
# ==========================================================================

@cli.group()
def usb():
    """Classify USB storage by signature: NUSB, FUSB, HUSB, SUSB, ODD."""


@usb.command("zone")
@click.option("-i", "--interval", type=float, default=0.25, show_default=True,
              help="Seconds between polls.")
@click.option("--existing", is_flag=True,
              help="Also classify whatever is already plugged in.")
@click.option("--storage-only", is_flag=True,
              help="Only react to mass storage, as the zone used to.")
@click.option("-n", "--iterations", type=int, default=None, hidden=True)
@pass_state
def usb_zone(state: State, interval, existing, storage_only, iterations):
    """USB 체험존 — plug anything in and watch it get identified.

    Shows what it is, every signature that went into that decision so a
    surprising answer can be argued with, and the tools that fit it: a floppy
    drive gets `updev floppy`, a camera gets camtoy, a keyboard gets an evdev
    tap. Storage is classified by signature; everything else by role.
    """
    if state.as_json:
        raise click.UsageError("--json cannot be combined with zone; use `updev usb classify`")
    from .ui.zone import FAST_BACKENDS, UsbZone

    ctx = state.ctx
    if not ctx.include:
        ctx.include = FAST_BACKENDS
    UsbZone(state.scanner, ctx, state.console, interval=interval,
            storage_only=storage_only).run(
        iterations=iterations, include_existing=existing
    )


@usb.command("classify")
@click.argument("target", required=False)
@click.option("--facts/--no-facts", default=True, show_default=True,
              help="Include the raw signature readings.")
@pass_state
def usb_classify(state: State, target, facts):
    """Classify attached USB storage, showing the evidence.

    TARGET is a USB address (4-1), a block device (sda) or part of a name.
    Omit it to classify everything currently attached.
    """
    from .ui.zone import scores_bar, verdict_panel
    from .usbclass import classify, gather_facts

    result = state.scan(include=frozenset({"usb"}))
    candidates = [d for d in result.devices if d.kind == Kind.USB and "storage" in d.tags]
    if target:
        want = target.strip().lower().removeprefix("/dev/")
        candidates = [
            d for d in candidates
            if want in (d.address.lower(), d.uid.lower())
            or want in d.label.lower()
            or want == (gather_facts(usb_address=d.address).block_name or "").lower()
        ]

    if not candidates:
        message = (
            f"no USB storage matches {target!r}" if target
            else "no USB mass-storage devices are attached"
        )
        if state.as_json:
            state.emit({"devices": []})
        else:
            state.console.print(f"[yellow]{message}[/]")
            state.console.print(
                "[dim]plug one in and try again, or run[/] [bold cyan]updev usb zone[/]"
            )
        raise SystemExit(1)

    verdicts = [(d, classify(gather_facts(usb_address=d.address))) for d in candidates]

    if state.as_json:
        state.emit({
            "devices": [
                {"uid": d.uid, "address": d.address, "label": d.label, **v.as_dict()}
                for d, v in verdicts
            ]
        })
        return

    for dev, verdict in verdicts:
        state.console.print(verdict_panel(dev, verdict, show_facts=facts))
        state.console.print(Panel(scores_bar(verdict), title="최종 점수",
                                  title_align="left", border_style="dim",
                                  box=box.ROUNDED))


@usb.command("path")
@click.argument("target", required=False)
@pass_state
def usb_path(state: State, target):
    """Trace a device's physical path: root hub → hubs → device.

    Shows the port taken at each hop, the speed negotiated there, every /dev
    node the device owns, and where the chain is bottlenecked.

    Omit TARGET to show the path of every attached device.
    """
    from .usbrole import (
        build_path,
        device_nodes,
        gather_device_facts,
        identify,
        path_bottleneck,
    )

    result = state.scan(include=frozenset({"usb"}))
    devices = [d for d in result.devices if d.kind == Kind.USB and "root-hub" not in d.tags]
    if target:
        want = target.strip().lower()
        devices = [
            d for d in devices
            if want in (d.address.lower(), d.uid.lower()) or want in d.label.lower()
        ]
        if not devices:
            state.console.print(f"[red]no USB device matches[/] [bold]{target}[/]")
            state.console.print("[dim]run[/] [bold cyan]updev scan -k usb[/] [dim]to list them[/]")
            raise SystemExit(1)

    payload = []
    for dev in devices:
        hops = build_path(dev.address)
        verdict = identify(gather_device_facts(dev.address))
        nodes = device_nodes(dev.address)
        bottleneck = path_bottleneck(hops)
        if state.as_json:
            payload.append({
                "address": dev.address,
                "label": dev.label,
                "roles": [str(r) for r in verdict.roles],
                "bottleneck": bottleneck,
                "nodes": nodes,
                "path": [
                    {"address": h.address, "label": h.label, "port": h.port,
                     "speed_mbps": h.speed, "speed": h.speed_label,
                     "root_hub": h.is_root_hub}
                    for h in hops
                ],
            })
            continue
        state.console.print(_path_panel(dev, hops, verdict, nodes, bottleneck))

    if state.as_json:
        state.emit({"devices": payload})


def _path_panel(dev, hops, verdict, nodes, bottleneck):
    from .usbrole import ROLE_STYLE

    tree = Table.grid(padding=(0, 1))
    tree.add_column(no_wrap=True)                 # indent + connector
    tree.add_column(style="bold", no_wrap=True, width=30, overflow="ellipsis")
    tree.add_column(style="dim", no_wrap=True, width=9)
    tree.add_column(no_wrap=True, width=22)
    tree.add_column(no_wrap=True)

    tree.add_row("", Text("Raspberry Pi (host)", style="bold magenta"), "", "", "")
    culprit = bottleneck[0] if bottleneck else None
    for depth, hop in enumerate(hops):
        indent = "  " * depth + ("└─ " if depth else " └─ ")
        name = Text(hop.label or hop.address, style="bold")
        if hop.is_target:
            name.append("  ←", style="bold cyan")
        where = Text("root hub" if hop.is_root_hub else f"port {hop.port}", style="dim")
        tree.add_row(
            Text(indent, style="dim"),
            name,
            hop.address,
            Text(hop.speed_label or "-",
                 style="bold yellow" if depth == culprit else "green"),
            Text("◀ 병목" if depth == culprit else "", style="bold yellow") if
            depth == culprit else where,
        )

    body = [tree]

    if bottleneck:
        warn = Text("\n  ▲ ", style="bold yellow")
        warn.append(bottleneck[1], style="yellow")
        body.append(warn)

    info = Table.grid(padding=(0, 2))
    info.add_column(style="dim", justify="right", no_wrap=True, min_width=8)
    info.add_column(overflow="fold")
    if verdict.roles:
        roles = Text()
        for role in verdict.roles:
            roles.append(f" {role} ", style=f"bold black on {ROLE_STYLE.get(role, 'white')}")
            roles.append(" ")
        info.add_row("역할", roles)
    storage_class = dev.detail.get("storage_class")
    if storage_class:
        info.add_row("분류", Text(storage_class, style="bold"))
    for subsystem, entries in sorted(nodes.items()):
        info.add_row(subsystem, Text(", ".join(entries), style="cyan"))
    info.add_row("sysfs", Text(f"/sys/bus/usb/devices/{dev.address}", style="dim"))
    body.append(Text())
    body.append(info)

    return Panel(Group(*body), title=f"USB 경로 · {dev.address}", title_align="left",
                 border_style="bright_cyan", box=box.ROUNDED)


@usb.command("descriptors")
@click.argument("target")
@click.option("--hints/--no-hints", default=True, show_default=True,
              help="Include what the descriptors imply.")
@pass_state
def usb_descriptors(state: State, target, hints):
    """Raw USB descriptors, read through the system's USB permissions.

    Shows what sysfs never surfaces: every configuration and alternate setting,
    the full endpoint map with transfer types and packet sizes, interface
    associations, and class-specific descriptors.

    Reading needs no root — /dev/bus/usb is world-readable. Write access (which
    control transfers and QEMU passthrough need) is reported but not required.
    """
    from .usbdesc import (
        descriptor_hints,
        descriptor_source,
        parse_descriptors,
        read_raw_descriptors,
    )

    result = state.scan(include=frozenset({"usb"}))
    want = target.strip().lower()
    devices = [
        d for d in result.devices
        if d.kind == Kind.USB and (
            want in (d.address.lower(), d.uid.lower()) or want in d.label.lower()
        )
    ]
    if not devices:
        state.console.print(f"[red]no USB device matches[/] [bold]{target}[/]")
        raise SystemExit(1)

    dev = devices[0]
    path, kind, writable = descriptor_source(dev.address)
    blob, source, error = read_raw_descriptors(dev.address)
    if error:
        state.console.print(f"[red]{error}[/]")
        raise SystemExit(1)
    tree = parse_descriptors(blob, source)

    if state.as_json:
        state.emit({
            "address": dev.address, "label": dev.label,
            "source": path, "source_kind": kind, "writable": writable,
            "descriptors": tree.as_dict(),
            "hints": descriptor_hints(tree) if hints else [],
        })
        return

    state.console.print(_descriptor_panel(dev, tree, path, kind, writable,
                                          descriptor_hints(tree) if hints else []))


def _descriptor_panel(dev, tree, path, kind, writable, hints):
    body = []

    head = Table.grid(padding=(0, 2))
    head.add_column(style="dim", justify="right", no_wrap=True, min_width=14)
    head.add_column()
    head.add_row("source", Text(f"{path}  ({kind}, {tree.raw_length} bytes)", style="cyan"))
    access = Text("read", style="green")
    access.append("  ·  write ", style="dim")
    access.append("yes" if writable else "no", style="green" if writable else "yellow")
    if not writable:
        access.append("  (control transfers and QEMU passthrough need it)", style="dim")
    head.add_row("access", access)
    if tree.device:
        d = tree.device
        head.add_row("USB version", Text(d.usb_version, style="bold"))
        head.add_row("id", Text(f"{d.vendor_id}:{d.product_id}  rev {d.device_version}"))
        head.add_row("device class", Text(f"0x{d.cls:02x}/0x{d.subclass:02x}/0x{d.protocol:02x}"))
        head.add_row("EP0 packet", Text(f"{d.max_packet_size0} bytes"))
    body.append(head)

    for config in tree.configurations:
        power = f"{config.max_power_ma} mA"
        if config.self_powered:
            power += " (self-powered)"
        title = Text(f"\nconfiguration {config.value}", style="bold")
        title.append(f"   {config.num_interfaces} interfaces · {power}", style="dim")
        body.append(title)

        for assoc in config.associations:
            line = Text("  ⇥ association: ", style="dim")
            line.append(
                f"interfaces {assoc.first_interface}.."
                f"{assoc.first_interface + assoc.interface_count - 1} "
                f"= one function (class 0x{assoc.cls:02x})",
                style="bright_magenta",
            )
            body.append(line)

        table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False,
                      header_style="dim", expand=True)
        table.add_column("iface", style="bold", width=7, no_wrap=True)
        table.add_column("class/sub/proto", width=18, no_wrap=True)
        table.add_column("endpoints", width=34, overflow="fold")
        table.add_column("class-specific", overflow="fold", style="dim", ratio=1)
        for iface in config.interfaces:
            endpoints = Text()
            for ep in iface.endpoints:
                style = {"isochronous": "bright_magenta", "interrupt": "cyan",
                         "bulk": "green"}.get(ep.transfer_type, "white")
                endpoints.append(ep.describe() + "\n", style=style)
            table.add_row(
                f"{iface.number}.{iface.alternate}",
                iface.triple,
                endpoints or Text("none", style="dim"),
                " · ".join(iface.class_specific) or "",
            )
        body.append(table)

    if hints:
        body.append(Text("\n무엇을 뜻하나", style="bold dim"))
        implications = Table.grid(padding=(0, 1))
        implications.add_column(width=2, no_wrap=True)
        implications.add_column(overflow="fold")
        for hint in hints:
            implications.add_row(Text("·", style="cyan"), Text(hint))
        body.append(implications)

    if tree.unknown:
        kinds = ", ".join(f"0x{t:02x}({n}B)" for t, n in tree.unknown[:8])
        body.append(Text(f"\nunparsed descriptors: {kinds}", style="dim italic"))

    return Panel(Group(*body), title=f"USB descriptors · {dev.address} · {dev.label}",
                 title_align="left", border_style="bright_cyan", box=box.ROUNDED)


@usb.command("signatures")
@pass_state
def usb_signatures(state: State):
    """The rule table: which signature implies which class, and why."""
    from .usbclass import CLASS_LABEL, CLASS_STYLE, UsbClass, signature_reference

    rules = signature_reference()
    if state.as_json:
        state.emit({
            "classes": {str(c): CLASS_LABEL[c] for c in UsbClass},
            "rules": rules,
        })
        return

    classes = Table(box=box.SIMPLE, show_edge=False, header_style="dim", expand=True)
    classes.add_column("class", width=7)
    classes.add_column("meaning")
    for cls in (UsbClass.NUSB, UsbClass.FUSB, UsbClass.HUSB, UsbClass.SUSB, UsbClass.ODD):
        classes.add_row(
            Text(f" {cls} ", style=f"bold black on {CLASS_STYLE[cls]}"),
            Text(CLASS_LABEL[cls], style=CLASS_STYLE[cls]),
        )

    table = Table(box=box.SIMPLE, show_edge=False, header_style="dim bold", expand=True)
    table.add_column("signature", style="bold", width=36, overflow="fold")
    table.add_column("implies", width=11, no_wrap=True)
    table.add_column("weight", width=9, no_wrap=True)
    table.add_column("why", overflow="fold", ratio=1, style="dim")
    for rule in rules:
        weight = rule["weight"]
        table.add_row(
            rule["signature"],
            Text(rule["verdict"], style="bold"),
            Text(weight, style="bold green" if weight == "decisive" else
                 "yellow" if weight == "strong" else "dim"),
            rule["why"],
        )

    state.console.print(Panel(classes, title="분류", title_align="left",
                              border_style="bright_magenta", box=box.ROUNDED))
    state.console.print(Panel(
        Group(table, Text(
            "\ndecisive 규칙이 하나라도 걸리면 즉시 확정하고 나머지는 보지 않는다. "
            "나머지는 가중치를 더해 최고점을 고르고, 2위와의 격차로 신뢰도를 매긴다.",
            style="dim italic")),
        title="시그니처 규칙", title_align="left",
        border_style="cyan", box=box.ROUNDED,
    ))


# ==========================================================================
# floppy
# ==========================================================================

@cli.group()
def floppy():
    """Build a 1.44 MB floppy image that shows ASCII art instead of booting."""


@floppy.command("make")
@click.option("-o", "--output", default="updev-art.img", show_default=True,
              type=click.Path(dir_okay=False), help="Where to write the image.")
@click.option("--label", default="UPDEV ART", show_default=True,
              help="Volume label (11 characters).")
@click.option("--force", is_flag=True, help="Overwrite an existing file.")
@pass_state
def floppy_make(state: State, output, label, force):
    """Write the image. Boot it and you get art; mount it and you get art."""
    from .floppy import FLOPPY_SIZE, build_image

    path = Path(output)
    if path.exists() and not force:
        state.console.print(
            f"[yellow]{path} already exists[/] — pass [bold cyan]--force[/] to overwrite"
        )
        raise SystemExit(2)

    image = build_image(label=label)
    path.write_bytes(image.data)

    if state.as_json:
        state.emit({"output": str(path), "bytes": image.size, "label": image.label,
                    "files": image.files, "free_bytes": image.free_bytes})
        return

    table = Table(box=box.SIMPLE, show_edge=False, header_style="dim")
    table.add_column("file", style="bold")
    table.add_column("bytes", justify="right", style="dim")
    for name, size in image.files.items():
        table.add_row(name, f"{size:,}")

    tips = Text()
    tips.append("\n부팅해보기  ", style="dim")
    tips.append(f"qemu-system-i386 -fda {path} -boot a", style="bold cyan")
    tips.append("\n내용 보기    ", style="dim")
    tips.append(f"udisksctl loop-setup -r -f {path}", style="bold cyan")
    tips.append("\n실물 플로피  ", style="dim")
    tips.append(f"sudo dd if={path} of=/dev/sdX bs=512", style="bold cyan")
    tips.append("   ← 대상 장치를 반드시 확인할 것", style="dim italic")

    state.console.print(Panel(
        Group(
            Text(f"{image.size:,} bytes · FAT12 · label {image.label!r} · "
                 f"{image.free_bytes:,} bytes free", style="dim"),
            Text(),
            table,
            tips,
        ),
        title=f"wrote {path}", title_align="left",
        border_style="bright_magenta", box=box.ROUNDED,
    ))


@floppy.command("show")
@click.argument("piece", required=False)
@pass_state
def floppy_show(state: State, piece):
    """Print the art to the terminal without writing anything.

    PIECE picks one file (readme, updev, disk, pi5, buses, usbclass); omit it
    for the boot-sector message.
    """
    from .floppy import boot_message, gallery

    art = gallery()
    if not piece:
        state.console.print(Panel(
            Text(boot_message().replace("\r\n", "\n").strip("\n"), style="bright_green"),
            title="boot sector", title_align="left",
            border_style="bright_green", box=box.ROUNDED,
        ))
        state.console.print(Text(
            "  pieces: " + ", ".join(n.split(".")[0].lower() for n in art),
            style="dim",
        ))
        return

    want = piece.strip().lower()
    match = next((n for n in art if n.split(".")[0].lower() == want), None)
    if match is None:
        state.console.print(f"[red]no such piece:[/] {piece}")
        state.console.print("[dim]available: " +
                            ", ".join(n.split('.')[0].lower() for n in art) + "[/]")
        raise SystemExit(1)
    state.console.print(Panel(
        Text(art[match].rstrip("\n"), style="bright_cyan"),
        title=match, title_align="left", border_style="bright_cyan", box=box.ROUNDED,
    ))


@floppy.command("boot")
@click.option("-i", "--image", type=click.Path(dir_okay=False),
              help="Image to boot. Omit to build the ASCII-art floppy on the fly.")
@click.option("--disk", type=click.Path(exists=True, dir_okay=False),
              help="Also attach a hard-disk image.")
@click.option("--usb", "usb_targets", multiple=True,
              help="Pass a host USB device through to the guest (repeatable).")
@click.option("--display", "display_mode",
              type=click.Choice(["none", "gtk", "sdl"]), default="none",
              show_default=True, help="none runs headless and takes a screenshot.")
@click.option("-t", "--timeout", type=float, default=12.0, show_default=True,
              help="Seconds to let the guest run.")
@click.option("--screenshot", type=click.Path(dir_okay=False), default="",
              help="Where to save the framebuffer (.png needs Pillow, else .ppm).")
@click.option("--memory", type=int, default=128, show_default=True, help="Guest RAM in MB.")
@click.option("--force", is_flag=True,
              help="Proceed past passthrough warnings (input devices, unmounted disks).")
@click.option("--dry-run", is_flag=True, help="Print the QEMU command and stop.")
@pass_state
def floppy_boot(state: State, image, disk, usb_targets, display_mode, timeout,
                screenshot, memory, force, dry_run):
    """Boot the image in QEMU — see the boot sector actually run.

    Headless by default: it runs the guest, grabs the framebuffer through the
    QEMU monitor, and tells you what was drawn.

    --usb hands a host USB device to the guest. That needs write access to
    /dev/bus/usb, which reading descriptors does not; run `updev usb permissions`
    to see where you stand. Note the ASCII-art boot sector is 512 bytes and
    cannot drive USB — pass --image with a real OS if you want the guest to
    enumerate the device.
    """
    import tempfile

    from .floppy import build_image
    from .vm import build_plan, check_passthrough, run_qemu, screen_to_text

    checks = []
    for target in usb_targets:
        check = check_passthrough(target.strip())
        checks.append(check)

    blocked = [c for c in checks if c.blockers]
    warned = [c for c in checks if c.warnings and not c.blockers]
    if blocked or (warned and not force):
        if state.as_json:
            state.emit({"passthrough": [c.as_dict() for c in checks],
                        "launched": False})
        else:
            state.console.print(_passthrough_panel(checks, force))
        raise SystemExit(2)

    tmp = None
    if not image:
        tmp = tempfile.NamedTemporaryFile(suffix=".img", delete=False)
        tmp.write(build_image().data)
        tmp.close()
        image = tmp.name
    elif not Path(image).exists():
        state.console.print(f"[red]no such image:[/] {image}")
        raise SystemExit(1)

    monitor = ""
    if display_mode == "none":
        monitor = str(Path(tempfile.gettempdir()) / f"updev-qemu-{os.getpid()}.sock")
        if not screenshot:
            screenshot = str(Path(tempfile.gettempdir()) /
                             f"updev-boot-{os.getpid()}.png")

    try:
        plan = build_plan(
            floppy=image, disk=disk, passthrough=checks, memory=memory,
            display=display_mode, monitor_path=monitor, screenshot=screenshot,
        )
    except RuntimeError as e:
        state.console.print(f"[red]{e}[/]")
        raise SystemExit(1)

    if dry_run:
        if state.as_json:
            state.emit({"command": plan.argv, "passthrough":
                        [c.as_dict() for c in checks]})
        else:
            state.console.print(Panel(Text(plan.command, style="bold cyan"),
                                      title="dry run", title_align="left",
                                      border_style="cyan", box=box.ROUNDED))
        return

    if not state.as_json:
        state.console.print(
            Text(f"booting {Path(image).name} for {timeout:g}s…", style="dim")
        )
    rc, output, shot = run_qemu(plan, timeout=timeout,
                                screenshot_after=min(6.0, timeout * 0.5))

    drawn = screen_to_text(shot) if shot else []
    lit_rows = [line for line in drawn if line.strip()]

    if state.as_json:
        state.emit({
            "image": image, "returncode": rc, "screenshot": shot,
            "rows_drawn": len(lit_rows),
            "passthrough": [c.as_dict() for c in checks],
            "output": output.strip().splitlines()[-20:],
        })
        return

    body = []
    if shot:
        body.append(Text(f"screenshot  {shot}", style="cyan"))
    if lit_rows:
        body.append(Text(
            f"\n{len(lit_rows)} row(s) drawn — the guest put something on screen:",
            style="dim",
        ))
        grid = Text()
        for line in lit_rows[:14]:
            grid.append("  " + line[:76] + "\n", style="green")
        body.append(grid)
    elif shot:
        body.append(Text(
            "\nthe screen came back blank — the boot sector may not have run",
            style="yellow",
        ))
    if checks:
        body.append(Text(
            f"\npassed through: " +
            ", ".join(f"{c.address} ({c.label})" for c in checks),
            style="bright_magenta",
        ))
        body.append(Text(
            "the 512-byte art boot sector cannot enumerate USB; QEMU attached the "
            "device but nothing in the guest will use it",
            style="dim italic",
        ))
    if output.strip():
        tail = "\n".join(output.strip().splitlines()[-6:])
        body.append(Text(f"\nqemu:\n{tail}", style="dim"))

    state.console.print(Panel(Group(*body) if body else Text("no output", style="dim"),
                              title=f"booted · {Path(image).name}", title_align="left",
                              border_style="bright_magenta", box=box.ROUNDED))
    if tmp:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _passthrough_panel(checks, force):
    from .vm import udev_rule

    body = []
    for check in checks:
        head = Text()
        head.append(f"{check.address}  ", style="bold")
        head.append(check.label, style="dim")
        if check.roles:
            head.append(f"   [{', '.join(check.roles)}]", style="cyan")
        body.append(head)
        body.append(Text(f"  node {check.node}   read "
                         f"{'yes' if check.readable else 'no'} · write "
                         f"{'yes' if check.writable else 'no'}", style="dim"))
        for blocker in check.blockers:
            line = Text("  ✖ ", style="bold red")
            line.append(blocker, style="red")
            body.append(line)
        for warning in check.warnings:
            line = Text("  ▲ ", style="bold yellow")
            line.append(warning, style="yellow")
            body.append(line)
        body.append(Text())

    # Only offer to escalate when permission is the *only* thing in the way.
    # A device backing a mounted filesystem is not a permissions problem, and
    # telling someone to sudo past it would hand a guest the running rootfs.
    def only_permission_blocks(check):
        return all("no write access" in b for b in check.blockers)

    hard_blocked = [c for c in checks if c.blockers and not only_permission_blocks(c)]
    needs_write = any(not c.writable for c in checks)

    if hard_blocked:
        body.append(Text(
            "sudo 로는 해결되지 않는다 — 권한 문제가 아니라 그 장치를 넘기면 "
            "호스트가 쓰던 것을 뺏기기 때문이다.",
            style="bold yellow",
        ))
        if any("mounted filesystem" in b for c in hard_blocked for b in c.blockers):
            body.append(Text(
                "디스크를 정말 넘기려면 먼저 언마운트할 것. 루트 파일시스템이 올라간 "
                "디스크는 애초에 넘길 수 없다.",
                style="dim",
            ))
        if any("hub" in b for c in hard_blocked for b in c.blockers):
            body.append(Text(
                "허브 대신 그 뒤에 달린 개별 장치 주소를 지정할 것 "
                "(updev usb path 로 확인).",
                style="dim",
            ))
    elif needs_write:
        body.append(Text("write access to /dev/bus/usb is what passthrough needs, "
                         "and only passthrough — reading descriptors already works.",
                         style="dim"))
        body.append(Text("\n한 번만 쓸 거면:", style="dim"))
        body.append(Text("  sudo " + " ".join(sys.argv), style="bold cyan"))
        body.append(Text("\n영구적으로 (plugdev 그룹에 권한 부여):", style="dim"))
        body.append(Text(
            f'  echo \'{udev_rule()}\' | sudo tee /etc/udev/rules.d/70-updev-usb.rules\n'
            "  sudo udevadm control --reload && sudo udevadm trigger",
            style="bold cyan",
        ))
        body.append(Text(
            "  ↑ 이 규칙은 모든 USB 장치에 plugdev 쓰기 권한을 준다. "
            "특정 장치만 원하면 idVendor/idProduct를 넣어 좁힐 것.",
            style="dim italic",
        ))
    if any(c.warnings for c in checks) and not force and not hard_blocked:
        body.append(Text("경고를 확인했으면 --force 를 붙여 다시 실행.", style="dim"))

    blocked = any(c.blockers for c in checks)
    return Panel(Group(*body), title="passthrough 거부됨" if blocked else "확인 필요",
                 title_align="left", border_style="red" if blocked else "yellow",
                 box=box.ROUNDED)


@usb.command("permissions")
@pass_state
def usb_permissions(state: State):
    """Where you stand on USB access: what reads, what needs more."""
    from .usbdesc import descriptor_source
    from .vm import udev_rule

    result = state.scan(include=frozenset({"usb"}))
    devices = [d for d in result.devices if d.kind == Kind.USB and "root-hub" not in d.tags]

    rows = []
    for dev in devices:
        path, kind, writable = descriptor_source(dev.address)
        rows.append((dev, path, kind, writable))

    if state.as_json:
        state.emit({
            "groups": _own_groups(),
            "devices": [
                {"address": d.address, "label": d.label, "node": p,
                 "source": k, "readable": bool(p), "writable": w}
                for d, p, k, w in rows
            ],
        })
        return

    table = Table(box=box.SIMPLE, show_edge=False, header_style="dim bold", expand=True)
    table.add_column("device", style="bold", width=28, no_wrap=True, overflow="ellipsis")
    table.add_column("addr", style="dim", width=8, no_wrap=True)
    table.add_column("node", width=22, no_wrap=True)
    table.add_column("read", width=5, no_wrap=True)
    table.add_column("write", width=6, no_wrap=True)
    table.add_column("가능한 것", no_wrap=True, overflow="ellipsis", ratio=1)
    for dev, path, kind, writable in rows:
        table.add_row(
            dev.label, dev.address, path or "-",
            Text("yes" if path else "no", style="green" if path else "red"),
            Text("yes" if writable else "no", style="green" if writable else "yellow"),
            Text("디스크립터 + 패스스루", style="green") if writable
            else Text("디스크립터 읽기", style="dim"),
        )

    groups = _own_groups()
    notes = Text()
    notes.append("\n소속 그룹: ", style="dim")
    notes.append(", ".join(groups), style="cyan")
    notes.append("\n\n", style="dim")
    notes.append("읽기", style="bold green")
    notes.append("는 이미 된다 — /dev/bus/usb 가 crw-rw-r-- 라서 권한 없이 "
                 "디스크립터를 전부 읽는다.\n", style="dim")
    notes.append("쓰기", style="bold yellow")
    notes.append("는 컨트롤 전송과 QEMU 패스스루에만 필요하다. 영구적으로 열려면:\n",
                 style="dim")
    notes.append(
        f"  echo '{udev_rule()}' | sudo tee /etc/udev/rules.d/70-updev-usb.rules\n"
        "  sudo udevadm control --reload && sudo udevadm trigger",
        style="bold cyan",
    )

    state.console.print(Panel(Group(table, notes), title="USB 권한",
                              title_align="left", border_style="cyan", box=box.ROUNDED))


def _own_groups() -> list[str]:
    import grp

    try:
        names = [grp.getgrgid(g).gr_name for g in os.getgroups()]
    except Exception:
        return []
    return sorted(set(names))


@floppy.command("info")
@click.argument("image", type=click.Path(exists=True, dir_okay=False))
@pass_state
def floppy_info(state: State, image):
    """Read an image back: BPB, boot signature, root directory."""
    from .floppy import inspect_image

    data = Path(image).read_bytes()
    info = inspect_image(data)
    if state.as_json:
        state.emit(info)
        return

    head = Table.grid(padding=(0, 2))
    head.add_column(style="dim", justify="right", no_wrap=True)
    head.add_column()
    for key in ("size", "oem", "bytes_per_sector", "sectors_per_cluster",
                "total_sectors", "media_descriptor", "sectors_per_fat",
                "volume_label", "fs_type", "boot_signature"):
        if key in info:
            head.add_row(key.replace("_", " "), Text(str(info[key])))
    bootable = info.get("bootable_signature_present")
    head.add_row("0x55AA present",
                 Text("yes" if bootable else "no", style="green" if bootable else "red"))

    listing = Table(box=box.SIMPLE, show_edge=False, header_style="dim")
    listing.add_column("name", style="bold")
    listing.add_column("cluster", justify="right", style="dim")
    listing.add_column("bytes", justify="right")
    for entry in info.get("entries", []):
        listing.add_row(
            entry["name"] + (" (volume label)" if entry["volume_label"] else ""),
            str(entry["cluster"]),
            f"{entry['size']:,}",
        )

    state.console.print(Panel(Group(head, Text(), listing), title=str(image),
                              title_align="left", border_style="cyan", box=box.ROUNDED))


# ==========================================================================
# tools — what to run on whatever is attached
# ==========================================================================

@cli.command("tools")
@click.argument("target", required=False)
@click.option("--brief", is_flag=True, help="One line per tool.")
@pass_state
def tools_command(state: State, target, brief):
    """뭘 꽂았든, 그걸로 할 수 있는 것.

    `updev usb zone` without the live screen. Identifies what is attached —
    storage by signature, everything else by role — and lists the commands
    that fit it. A USB floppy gets `updev floppy`; a thumb drive gets a read
    benchmark; a keyboard gets an evdev tap; a wired NFC module gets the
    reader.

    TARGET is anything `updev show` accepts. Omit it for everything attached.
    """
    from .toolkit import annotate, recognize
    from .ui.zone import tool_lines, tools_panel

    result = state.scan()
    if target:
        devices = result.find(target)
        if not devices:
            state.console.print(f"[red]nothing matches[/] [bold]{target}[/]")
            state.console.print("[dim]run[/] [bold cyan]updev scan[/] [dim]to list devices[/]")
            raise SystemExit(1)
    else:
        devices = [
            d for d in result.devices
            if (d.kind == Kind.USB and "root-hub" not in d.tags)
            or d.kind == Kind.NFC
            or (d.kind == Kind.CAMERA and d.node)
        ]

    recognitions = []
    for dev in devices:
        recognition = recognize(dev)
        annotate(recognition.tools)
        recognitions.append(recognition)

    if state.as_json:
        state.emit({"devices": [r.as_dict() for r in recognitions]})
        return

    if not recognitions:
        state.console.print("[yellow]nothing attached that updev has a tool for[/]")
        state.console.print("[dim]plug something in, or run[/] "
                            "[bold cyan]updev nfc wiring[/] [dim]to wire a reader[/]")
        return

    for recognition in recognitions:
        dev = recognition.device
        title = Text()
        title.append(f" {recognition.badge} ", style="bold black on bright_yellow")
        title.append(f"  {dev.label}", style="bold")
        title.append(f"   {recognition.headline}", style="dim")
        if brief:
            state.console.print(title)
            state.console.print(tool_lines(recognition.tools))
            state.console.print()
        else:
            state.console.print(tools_panel(recognition, title=str(title.plain).strip()))



# ==========================================================================
# gui
# ==========================================================================

@cli.command("gui")
@click.argument("target", required=False)
@pass_state
def gui_command(state: State, target):
    """장치별 에디터 GUI — 꽂힌 것마다 그에 맞는 편집기.

    TARGET is anything `updev show` accepts; omit it to browse everything.
    `updev 3-2 gui` works too.

    Plain tkinter, so there is nothing to install on Raspberry Pi OS. A USB
    floppy gets the FAT12 editor, an NFC reader gets the tag editor, an I2C
    chip gets its register space, a keyboard gets its event stream — and
    anything else at least gets its full detail and the commands that fit it.
    """
    from .gui import available, launch

    if state.as_json:
        raise click.UsageError("--json cannot be combined with gui")

    ok, why = available()
    if not ok:
        state.console.print(f"[red]GUI를 열 수 없습니다[/] — {why}")
        state.console.print("[dim]터미널에서 같은 걸 보려면:[/] "
                            "[bold cyan]updev tools[/]")
        raise SystemExit(1)

    result = state.scan()
    launch(result, target=target or "", deep=state.ctx.deep)


# ==========================================================================
# disk — read-only measurement
# ==========================================================================

@cli.group()
def disk():
    """Measure a block device. Reads only — nothing here opens for writing."""


@disk.command("bench")
@click.argument("target")
@click.option("--chunk", type=int, default=4, show_default=True,
              help="MiB per sequential read.")
@click.option("-n", "--reads", type=int, default=8, show_default=True,
              help="How many sequential chunks.")
@click.option("--seeks", type=int, default=48, show_default=True,
              help="Random reads for the latency probe (0 skips it).")
@click.option("--buffered", is_flag=True,
              help="Skip O_DIRECT; drop the cache before each read instead.")
@pass_state
def disk_bench(state: State, target, chunk, reads, seeks, buffered):
    """실제 읽기 속도 · 랜덤 접근 지연 — 링크 속도가 아니라 매체의 속도.

    TARGET is a device node (/dev/sda) or a name updev knows (sda, the model).

    The seek probe is the interesting half: `updev usb classify` decides HUSB
    against SUSB from the drive's own VPD 0xB1 claim, and a median random read
    latency either backs that up or catches the bridge lying.
    """
    from .bench import link_comparison, rotation_hint, run
    from .core.util import human_bytes, usb_address_from_path

    node = _resolve_block(state, target)
    try:
        result = run(node, chunk_mb=chunk, reads=reads, seeks=max(0, seeks),
                     direct=not buffered)
    except PermissionError:
        state.console.print(f"[red]no read access to[/] [bold]{node}[/]")
        state.console.print(f"[dim]raw sectors are root:disk. Either:[/] "
                            f"[bold cyan]sudo updev disk bench {node}[/]")
        state.console.print("[dim]or a udev rule scoped to this one device. Not the "
                            "disk group — it hands over raw read/write on every "
                            "block device, which is root by another name.[/]")
        raise SystemExit(1)
    except OSError as e:
        state.console.print(f"[red]{node}: {e.strerror or e}[/]")
        raise SystemExit(1)

    payload = result.as_dict()

    # Cross-checks: the negotiated link, and what the classifier decided.
    name = Path(node).name
    link_mbps = 0.0
    address = ""
    try:
        real = str(Path(f"/sys/block/{name}").resolve())
        address = usb_address_from_path(real)
    except OSError:
        pass
    if address:
        from .usbrole import build_path
        for hop in build_path(address):
            if hop.is_target:
                link_mbps = float(hop.speed or 0)
    payload["link_mbps"] = link_mbps
    payload["usb_address"] = address

    verdict = None
    if address:
        from .usbclass import UsbClass, classify, gather_facts
        verdict = classify(gather_facts(usb_address=address))
        payload["classifier"] = str(verdict.usb_class)

    if state.as_json:
        state.emit(payload)
        return

    head = Table.grid(padding=(0, 2))
    head.add_column(style="dim", justify="right", no_wrap=True, min_width=18)
    head.add_column(overflow="fold")
    head.add_row("device", Text(f"{node}   {human_bytes(result.size_bytes)}", style="bold"))
    head.add_row("sequential read",
                 Text(f"{result.throughput_mbs:.1f} MB/s", style="bold bright_green")
                 .append(f"   (median chunk {result.steady_mbs:.1f} MB/s, "
                         f"{result.reads} × {result.chunk_bytes // (1024 * 1024)} MiB)",
                         style="dim"))
    if link_mbps:
        head.add_row("versus the link", Text(link_comparison(result.throughput_mbs, link_mbps)))
    if result.seek_ms:
        spin, reason = rotation_hint(result.seek_median)
        line = Text(f"{result.seek_median:.2f} ms", style="bold")
        line.append(f"   → {spin}", style="bold cyan")
        head.add_row("random read (median)", line)
        head.add_row("", Text(reason, style="dim italic"))
    head.add_row("io", Text("O_DIRECT" if result.direct else "buffered, cache dropped",
                            style="dim"))
    if result.note:
        head.add_row("", Text(result.note, style="dim"))

    body = [head]
    if verdict is not None:
        from .usbclass import UsbClass
        spin, _ = rotation_hint(result.seek_median)
        expected = {UsbClass.HUSB: "rotating", UsbClass.SUSB: "solid-state",
                    UsbClass.NUSB: "solid-state"}.get(verdict.usb_class)
        if expected and spin in ("rotating", "solid-state"):
            agree = expected == spin
            note = Text("\n  ")
            note.append("분류기와 실측: ", style="bold dim")
            note.append(f"{verdict.usb_class} ", style="bold")
            note.append("says " + expected, style="dim")
            note.append("  ·  measured " + spin, style="dim")
            note.append("   일치" if agree else "   불일치", 
                        style="bold green" if agree else "bold red")
            body.append(note)
            if not agree:
                body.append(Text(
                    "  the two disagree, and the measurement is the one that "
                    "touched the medium — a bridge that misreports VPD 0xB1 is "
                    "exactly the failure this probe exists to catch.",
                    style="dim italic"))

    state.console.print(Panel(Group(*body), title="disk bench", title_align="left",
                              border_style="bright_green", box=box.ROUNDED))


def _resolve_block(state: State, target: str) -> str:
    """A node path, a bare name, an image file, or anything `updev show` matches."""
    want = target.strip()
    if Path(want).is_file() or (want.startswith("/dev/") and Path(want).exists()):
        return want
    if Path(f"/dev/{want}").exists():
        return f"/dev/{want}"
    result = state.scan(include=frozenset({"storage"}))
    for dev in result.find(want):
        if dev.node:
            return dev.node
    state.console.print(f"[red]no block device matches[/] [bold]{target}[/]")
    state.console.print("[dim]run[/] [bold cyan]updev scan -k storage[/]")
    raise SystemExit(1)


# ==========================================================================
# hid — what the keyboard is actually sending
# ==========================================================================

@cli.group()
def hid():
    """Keyboards, mice, gamepads — the events, as the kernel decoded them."""


@hid.command("list")
@pass_state
def hid_list(state: State):
    """Every input device with an event node."""
    from .hid import event_nodes

    root = Path("/sys/class/input")
    rows = []
    for entry in sorted(root.glob("input*")) if root.is_dir() else []:
        for node in event_nodes(entry):
            rows.append(node)

    if state.as_json:
        state.emit({"devices": [n.as_dict() for n in rows]})
        return
    if not rows:
        state.console.print("[yellow]no input devices[/]")
        return

    table = Table(box=box.SIMPLE, show_edge=False, header_style="dim")
    table.add_column("node", style="bold")
    table.add_column("name")
    table.add_column("emits", style="dim")
    table.add_column("access", style="dim")
    for node in rows:
        table.add_row(node.path, node.name, ", ".join(node.capabilities) or "—",
                      "readable" if node.readable else "no access")
    state.console.print(table)


@hid.command("watch")
@click.argument("target")
@click.option("-d", "--duration", type=float, default=0.0,
              help="Stop after this many seconds.")
@click.option("-n", "--limit", type=int, default=0, help="Stop after this many events.")
@click.option("--all-events", is_flag=True,
              help="Include the SYN/MSC framing events too.")
@pass_state
def hid_watch(state: State, target, duration, limit, all_events):
    """키보드·마우스가 실제로 보내는 이벤트를 그대로.

    TARGET is a USB address (1-2.2), an input directory (input29), an event
    node (event5) or part of the device's name. A composite receiver is
    several event nodes and all of them are watched.

    Reading a keyboard's event node means reading everything typed on it,
    passwords included — which is why the target is required and why this
    prints what it opened before it starts.
    """
    from .hid import describe, resolve, watch

    nodes = resolve(target)
    if not nodes:
        state.console.print(f"[red]no input device matches[/] [bold]{target}[/]")
        state.console.print("[dim]run[/] [bold cyan]updev hid list[/]")
        raise SystemExit(1)

    blocked = [n for n in nodes if not n.readable]
    if blocked and len(blocked) == len(nodes):
        state.console.print(f"[red]no read access to[/] {', '.join(n.path for n in blocked)}")
        state.console.print("[dim]add yourself to the input group:[/] "
                            "[bold cyan]sudo usermod -aG input $USER[/]"
                            "[dim]   (then log out and back in)[/]")
        raise SystemExit(1)

    readable_nodes = [n for n in nodes if n.readable]
    if not state.as_json:
        for node in readable_nodes:
            state.console.print(Text("watching ", style="dim")
                                .append(node.path, style="bold")
                                .append(f"   {node.name}", style="dim"))
        if any("keys" in n.capabilities for n in readable_nodes):
            state.console.print(Text(
                "이 장치는 키 입력을 보냅니다 — 여기 찍히는 건 실제로 입력되는 "
                "내용입니다.", style="yellow"))
        state.console.print(Text("ctrl-c 로 종료", style="dim italic"))

    count = 0
    try:
        for node, event in watch(readable_nodes, duration=duration, quiet=not all_events):
            count += 1
            if state.as_json:
                click.echo(json.dumps({"node": node.path, **event.as_dict()},
                                      ensure_ascii=False), nl=True)
            else:
                line = Text(time.strftime("%H:%M:%S "), style="dim")
                line.append(f"{Path(node.path).name:<8}", style="dim")
                line.append(describe(event),
                            style="bold" if event.type == 0x01 else "")
                state.console.print(line)
            if limit and count >= limit:
                break
    except KeyboardInterrupt:
        pass
    if not state.as_json:
        state.console.print(Text(f"\n{count} event(s)", style="dim"))


# ==========================================================================
# nfc — readers on jumper wires
# ==========================================================================

@cli.group()
def nfc():
    """NFC readers wired to the 40-pin header: MFRC522 and PN532.

    These modules cannot announce themselves — SPI has no enumeration and I2C
    has one fixed address — so start with `updev nfc wiring`, wire it, then
    `updev nfc detect`.
    """


@nfc.command("wiring")
@click.argument("module", required=False)
@pass_state
def nfc_wiring(state: State, module):
    """점퍼선 배선표 — 어느 핀에 뭘 꽂는지.

    MODULE picks one entry (rc522, spi, i2c, uart); omit it for all of them.
    """
    from .backends.nfc import missing_buses
    from .nfc import WIRINGS, wiring_for

    wanted = [wiring_for(module)] if module else list(WIRINGS)
    if module and wanted[0] is None:
        state.console.print(f"[red]no wiring for[/] [bold]{module}[/]")
        state.console.print("[dim]try: rc522, spi, i2c, uart[/]")
        raise SystemExit(1)

    if state.as_json:
        state.emit({
            "wirings": [w.as_dict() for w in wanted],
            "missing_buses": [{"bus": b, "enable": fix} for b, fix in missing_buses()],
        })
        return

    for wiring in wanted:
        table = Table(box=box.SIMPLE, show_edge=False, header_style="dim")
        table.add_column("모듈 핀", style="bold")
        table.add_column("", style="dim", justify="center")
        table.add_column("40핀 헤더", justify="right", style="bold bright_yellow")
        table.add_column("파이 쪽 이름", style="dim")
        for name, pin, pi_name in wiring.wires:
            table.add_row(name, "→", f"pin {pin}", pi_name)
        body = Group(
            table,
            Text(),
            Text(wiring.note, style="dim italic"),
            Text("\n확인 방법  ", style="dim").append(wiring.probe, style="cyan"),
        )
        state.console.print(Panel(
            body, title=f"{wiring.module}   [{wiring.bus}]", title_align="left",
            border_style="bright_red", box=box.ROUNDED,
        ))

    for bus, fix in missing_buses():
        state.console.print(Text(f"\n{bus.upper()} 가 꺼져 있습니다 — ", style="yellow")
                            .append("배선해도 아무것도 안 보입니다.", style="yellow dim"))
        state.console.print(Text("  $ ", style="dim").append(fix, style="bold cyan"))

    state.console.print(Text("\n배선 끝났으면  ", style="dim")
                        .append("updev nfc detect", style="bold cyan"))


@nfc.command("detect")
@click.option("-v", "--verbose", is_flag=True, help="Show the buses that stayed silent.")
@click.option("--uart", is_flag=True,
              help="Also probe /dev/serial0 (off by default — the console usually owns it).")
@click.option("--no-spi", is_flag=True, help="Skip SPI; I2C only.")
@pass_state
def nfc_detect(state: State, verbose, uart, no_spi):
    """리더가 붙어있는지 물어본다 — SPI·I2C(·UART)를 차례로.

    SPI has no addressing, so this clocks a register read out to whatever is
    on each chip-select. That is safe for an RC522 or a PN532 and harmless for
    most things, but it is why the scan backend won't do it without --deep.
    """
    from .backends.nfc import probe_all

    readers = probe_all(spi=not no_spi, i2c=True, uart=uart)
    found = [r for r in readers if r.found]

    if state.as_json:
        state.emit({"readers": [r.as_dict() for r in readers],
                    "found": len(found)})
        return

    for reader in found:
        body = Table.grid(padding=(0, 2))
        body.add_column(style="dim", justify="right", no_wrap=True, min_width=10)
        body.add_column(overflow="fold")
        body.add_row("module", Text(reader.module, style="bold"))
        body.add_row("transport", reader.transport)
        body.add_row("where", reader.where)
        body.add_row("firmware", Text(reader.detail, style="bright_green"))
        state.console.print(Panel(body, title="찾음", title_align="left",
                                  border_style="bright_green", box=box.ROUNDED))

    if not found:
        state.console.print("[yellow]아무 리더도 대답하지 않았습니다[/]")
        state.console.print("[dim]배선표:[/] [bold cyan]updev nfc wiring[/]")

    if verbose or not found:
        table = Table(box=box.SIMPLE, show_edge=False, header_style="dim")
        table.add_column("probed", style="bold")
        table.add_column("result")
        for reader in readers:
            table.add_row(
                f"{reader.module} · {reader.where}",
                Text("found", style="green") if reader.found
                else Text(reader.error or "no answer", style="dim"),
            )
        state.console.print(table)

    if found:
        state.console.print(Text("\n태그 올려보기  ", style="dim")
                            .append("updev nfc poll", style="bold cyan"))


@nfc.command("read")
@click.option("-t", "--timeout", type=float, default=8.0, show_default=True,
              help="Seconds to wait for a tag.")
@click.option("--uart", is_flag=True, help="Include the UART reader in the search.")
@pass_state
def nfc_read(state: State, timeout, uart):
    """태그 하나 읽기 — UID·ATQA·SAK 와 그게 무슨 카드인지."""
    from .nfc import atqa_describe

    chip, close, reader = _open_nfc(state, uart=uart)
    try:
        tag = _wait_for_tag(state, chip, timeout)
    finally:
        close()

    if tag is None:
        if state.as_json:
            state.emit({"tag": None, "reader": reader.where})
        else:
            state.console.print(f"[yellow]{timeout:.0f}초 동안 태그가 없었습니다[/]")
        raise SystemExit(1)

    if state.as_json:
        state.emit({"tag": tag.as_dict(), "reader": reader.where})
        return

    body = Table.grid(padding=(0, 2))
    body.add_column(style="dim", justify="right", no_wrap=True, min_width=8)
    body.add_column(overflow="fold")
    body.add_row("UID", Text(tag.uid_hex, style="bold bright_green"))
    body.add_row("kind", Text(tag.kind, style="bold"))
    if tag.atqa:
        body.add_row("ATQA", atqa_describe(tag.atqa))
    if tag.sak >= 0:
        body.add_row("SAK", f"0x{tag.sak:02x}")
    body.add_row("reader", tag.reader)
    state.console.print(Panel(body, title="태그", title_align="left",
                              border_style="bright_green", box=box.ROUNDED))
    if tag.sak in (0x08, 0x09, 0x18, 0x19):
        state.console.print(Text("MIFARE Classic — 섹터 덤프:  ", style="dim")
                            .append("updev nfc dump --sector 1", style="bold cyan"))


@nfc.command("poll")
@click.option("-d", "--duration", type=float, default=0.0,
              help="Stop after this many seconds.")
@click.option("-n", "--limit", type=int, default=0, help="Stop after this many tags.")
@click.option("--repeat", is_flag=True, help="Report a tag every pass, not just on change.")
@click.option("--uart", is_flag=True, help="Include the UART reader in the search.")
@pass_state
def nfc_poll(state: State, duration, limit, repeat, uart):
    """태그를 계속 기다린다 — 올릴 때마다 한 줄.

    The NFC counterpart of `updev usb zone`: hold a card on the reader and it
    says what it is.
    """
    from .nfc import NfcError

    chip, close, reader = _open_nfc(state, uart=uart)
    if not state.as_json:
        state.console.print(Text("태그를 리더에 올리세요 — ", style="bold")
                            .append(f"{reader.module} · {reader.where}", style="dim"))
        state.console.print(Text("ctrl-c 로 종료", style="dim italic"))

    seen = ""
    count = 0
    deadline = time.time() + duration if duration else None
    try:
        while deadline is None or time.time() < deadline:
            try:
                tag = chip.poll()
            except NfcError:
                seen = ""
                time.sleep(0.15)
                continue
            if tag.uid_hex != seen or repeat:
                seen = tag.uid_hex
                count += 1
                if state.as_json:
                    click.echo(json.dumps(tag.as_dict(), ensure_ascii=False))
                else:
                    line = Text(time.strftime("%H:%M:%S "), style="dim")
                    line.append(tag.uid_hex, style="bold bright_green")
                    line.append(f"   {tag.kind}", style="dim")
                    state.console.print(line)
                if limit and count >= limit:
                    break
            time.sleep(0.15)
    except KeyboardInterrupt:
        pass
    finally:
        close()
    if not state.as_json:
        state.console.print(Text(f"\n{count} tag(s)", style="dim"))


@nfc.command("dump")
@click.option("-s", "--sector", type=int, default=1, show_default=True,
              help="MIFARE Classic sector to read.")
@click.option("--key", default="FFFFFFFFFFFF", show_default=True,
              help="6-byte key A, as hex.")
@click.option("-t", "--timeout", type=float, default=8.0, show_default=True)
@click.option("--uart", is_flag=True, help="Include the UART reader in the search.")
@pass_state
def nfc_dump(state: State, sector, key, timeout, uart):
    """MIFARE Classic 한 섹터를 읽는다 (읽기 전용).

    Blocks are read, never written. The default key is the factory
    FFFFFFFFFFFF, which is what an unwritten card ships with; a keyed card
    fails authentication and says so rather than guessing.
    """
    from .nfc import (
        Mfrc522,
        NfcError,
        ascii_dump,
        describe_block,
        hexdump,
        sector_blocks,
    )

    try:
        key_bytes = bytes.fromhex(key.replace(":", "").replace(" ", ""))
    except ValueError:
        raise click.UsageError(f"--key must be hex, got {key!r}")
    if len(key_bytes) != 6:
        raise click.UsageError(f"--key must be 6 bytes, got {len(key_bytes)}")
    if sector < 0 or sector > 39:
        raise click.UsageError("MIFARE Classic has sectors 0-39")

    chip, close, reader = _open_nfc(state, uart=uart)
    rows = []
    try:
        tag = _wait_for_tag(state, chip, timeout)
        if tag is None:
            if state.as_json:
                state.emit({"tag": None})
            else:
                state.console.print(f"[yellow]{timeout:.0f}초 동안 태그가 없었습니다[/]")
            raise SystemExit(1)

        for block in sector_blocks(sector):
            try:
                if isinstance(chip, Mfrc522):
                    chip.authenticate(block, tag.uid, key_bytes)
                    data = chip.read_block(block)
                else:
                    data = chip.read_block(block, tag.uid, key_bytes)
                rows.append((block, data, ""))
            except NfcError as e:
                rows.append((block, b"", str(e)))
        if isinstance(chip, Mfrc522):
            chip.stop_crypto()
            chip.halt()
    finally:
        close()

    if state.as_json:
        state.emit({
            "tag": tag.as_dict(),
            "sector": sector,
            "blocks": [
                {"block": b, "hex": hexdump(d), "ascii": ascii_dump(d),
                 "role": describe_block(b, d), "error": err}
                for b, d, err in rows
            ],
        })
        return

    table = Table(box=box.SIMPLE, show_edge=False, header_style="dim")
    table.add_column("blk", justify="right", style="dim")
    table.add_column("16 bytes", style="bold")
    table.add_column("ascii")
    table.add_column("", style="dim")
    for block, data, error in rows:
        if error:
            table.add_row(str(block), Text(error, style="red"), "", "")
            continue
        table.add_row(str(block), hexdump(data), ascii_dump(data),
                      describe_block(block, data))
    state.console.print(Panel(
        table, title=f"sector {sector}   ·   UID {tag.uid_hex}   ·   {tag.kind}",
        title_align="left", border_style="bright_red", box=box.ROUNDED,
    ))


def _open_nfc(state: State, uart: bool = False):
    """Find a reader and open it, or explain what to check. (chip, close, reader)."""
    from .backends.nfc import open_reader, probe_all

    readers = [r for r in probe_all(spi=True, i2c=True, uart=uart) if r.found]
    if not readers:
        state.console.print("[red]리더를 찾지 못했습니다[/]")
        state.console.print("[dim]배선 확인:[/] [bold cyan]updev nfc wiring[/]"
                            "[dim]   ·   자세히:[/] [bold cyan]updev nfc detect -v[/]")
        raise SystemExit(1)
    reader = readers[0]
    try:
        chip, close = open_reader(reader)
    except OSError as e:
        state.console.print(f"[red]{reader.where}: {e.strerror or e}[/]")
        raise SystemExit(1)
    return chip, close, reader


def _wait_for_tag(state: State, chip, timeout: float):
    """Poll until a tag answers or the clock runs out."""
    from .nfc import NfcError

    deadline = time.time() + max(0.1, timeout)
    while time.time() < deadline:
        try:
            return chip.poll()
        except NfcError:
            time.sleep(0.15)
    return None


# ==========================================================================
# I2C
# ==========================================================================

@cli.group()
def i2c():
    """Scan buses, read registers, identify chips."""


@i2c.command("scan")
@click.argument("bus", type=int, required=False)
@pass_state
def i2c_scan(state: State, bus):
    """Probe every address on an I2C bus (all buses if BUS is omitted)."""
    result = state.scan(deep=True, include=frozenset({"i2c"}))
    devices = [d for d in result.devices if d.kind == Kind.I2C]
    if bus is not None:
        devices = [d for d in devices if d.bus == f"i2c-{bus}" or d.address == str(bus)]
        if not devices:
            state.console.print(f"[red]no such bus:[/] i2c-{bus}")
            raise SystemExit(1)

    if state.as_json:
        state.emit({"devices": [d.as_dict() for d in devices]})
        return

    buses = [d for d in devices if ":" not in d.uid.removeprefix("i2c:")]
    for bus_dev in buses:
        chips = [d for d in devices if d.parent == bus_dev.uid]
        state.console.print(_i2c_grid(bus_dev, chips))
    failed = [r for r in result.reports if not r.ok]
    for rep in failed:
        state.console.print(f"[red]backend {rep.name} failed:[/] {rep.error}")


def _i2c_grid(bus_dev, chips) -> Panel:
    """An i2cdetect-style matrix, but with the chip guesses filled in."""
    present = {int(c.address, 16): c for c in chips if c.address.startswith("0x")}
    grid = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, header_style="dim")
    grid.add_column("", style="dim")
    for col in range(16):
        grid.add_column(f"{col:x}", justify="center", width=2)
    for row in range(0, 8):
        cells = [f"{row:x}0"]
        for col in range(16):
            addr = row * 16 + col
            if addr in present:
                chip = present[addr]
                style = "bold green" if chip.driver else "bold yellow"
                cells.append(Text(f"{addr:02x}", style=style))
            elif 0x03 <= addr <= 0x77:
                cells.append(Text("--", style="dim"))
            else:
                cells.append(Text("  ", style="dim"))
        grid.add_row(*cells)

    body = [grid]
    if chips:
        listing = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, header_style="dim")
        listing.add_column("addr", style="bold")
        listing.add_column("status")
        listing.add_column("identification")
        for chip in sorted(chips, key=lambda c: c.address):
            listing.add_row(
                chip.address,
                Text("driver", style="green") if chip.driver else Text("unclaimed", style="yellow"),
                chip.detail.get("likely") or chip.driver or "unidentified",
            )
        body.append(Text("\nfound", style="bold dim"))
        body.append(listing)
        body.append(Text(
            "\ngreen = a kernel driver owns it · yellow = free for userspace",
            style="dim italic",
        ))
    else:
        body.append(Text("\nnothing responded on this bus", style="dim italic"))
    if bus_dev.issues:
        body.append(Text())
        body.append(render.issue_list(bus_dev.issues))

    title = Text(bus_dev.name, style="bold")
    title.append(f"  {bus_dev.detail.get('adapter', '')}", style="dim")
    return Panel(Group(*body), title=title, title_align="left",
                 border_style="bright_green", box=box.ROUNDED)


@i2c.command("read")
@click.argument("bus", type=int)
@click.argument("address")
@click.argument("register", default="0x00")
@click.option("-n", "--length", type=int, default=1, show_default=True,
              help="Bytes to read.")
@pass_state
def i2c_read(state: State, bus, address, register, length):
    """Read LENGTH bytes from REGISTER of a chip. Read-only, always safe."""
    from smbus2 import SMBus

    addr, reg = _parse_int(address), _parse_int(register)
    try:
        with SMBus(bus) as smb:
            data = smb.read_i2c_block_data(addr, reg, min(32, max(1, length)))
    except OSError as e:
        state.console.print(
            f"[red]read failed[/] on i2c-{bus} 0x{addr:02x} reg 0x{reg:02x}: {e}"
        )
        raise SystemExit(1)

    if state.as_json:
        state.emit({"bus": bus, "address": f"0x{addr:02x}", "register": f"0x{reg:02x}",
                    "data": data})
        return
    state.console.print(_hex_panel(
        data, f"i2c-{bus} 0x{addr:02x} @ reg 0x{reg:02x}", base=reg
    ))


@i2c.command("dump")
@click.argument("bus", type=int)
@click.argument("address")
@click.option("-n", "--length", type=int, default=256, show_default=True)
@pass_state
def i2c_dump(state: State, bus, address, length):
    """Dump a chip's register space as a hex table."""
    from smbus2 import SMBus

    addr = _parse_int(address)
    data: list[int] = []
    try:
        with SMBus(bus) as smb:
            for reg in range(min(256, length)):
                try:
                    data.append(smb.read_byte_data(addr, reg))
                except OSError:
                    data.append(-1)
    except OSError as e:
        state.console.print(f"[red]cannot open i2c-{bus}:[/] {e}")
        raise SystemExit(1)

    if state.as_json:
        state.emit({"bus": bus, "address": f"0x{addr:02x}", "registers": data})
        return
    state.console.print(_hex_panel(data, f"i2c-{bus} 0x{addr:02x} register dump"))


@i2c.command("write")
@click.argument("bus", type=int)
@click.argument("address")
@click.argument("register")
@click.argument("value")
@click.option("--yes", is_flag=True, help="Required — writes can misconfigure hardware.")
@pass_state
def i2c_write(state: State, bus, address, register, value, yes):
    """Write a byte to a register. Requires --yes."""
    from smbus2 import SMBus

    addr, reg, val = _parse_int(address), _parse_int(register), _parse_int(value)
    if not yes:
        state.console.print(
            f"[yellow]refusing to write[/] 0x{val:02x} → i2c-{bus} 0x{addr:02x} "
            f"reg 0x{reg:02x}\n[dim]a bad register write can misconfigure or brick a "
            f"chip. Re-run with[/] [bold cyan]--yes[/] [dim]if that's what you want.[/]"
        )
        raise SystemExit(2)
    try:
        with SMBus(bus) as smb:
            smb.write_byte_data(addr, reg, val)
    except OSError as e:
        state.console.print(f"[red]write failed:[/] {e}")
        raise SystemExit(1)
    state.console.print(
        f"[green]wrote[/] 0x{val:02x} → i2c-{bus} 0x{addr:02x} reg 0x{reg:02x}"
    )


# ==========================================================================
# SPI
# ==========================================================================

@cli.group()
def spi():
    """Inspect SPI buses and exercise them."""


@spi.command("test")
@click.argument("target", default="0.0")
@click.option("--speed", type=int, default=1_000_000, show_default=True, help="Clock in Hz.")
@click.option("--mode", type=click.IntRange(0, 3), default=0, show_default=True)
@pass_state
def spi_test(state: State, target, speed, mode):
    """Loopback test: jumper MOSI (pin 19) to MISO (pin 21), then run this.

    Proves the controller, the driver and the pin muxing all work end to end.
    """
    try:
        import spidev
    except ImportError:
        state.console.print("[red]python3-spidev is not installed[/]")
        raise SystemExit(1)

    bus, cs = _parse_spi_target(target)
    pattern = bytes([0x00, 0xFF, 0x55, 0xAA, 0x0F, 0xF0, 0x5A, 0xA5, 0x01, 0x80])

    dev = spidev.SpiDev()
    try:
        dev.open(bus, cs)
    except (OSError, FileNotFoundError) as e:
        state.console.print(f"[red]cannot open /dev/spidev{bus}.{cs}:[/] {e}")
        state.console.print("[dim]run[/] [bold cyan]updev doctor[/] [dim]— SPI may be disabled[/]")
        raise SystemExit(1)

    try:
        dev.mode = mode
        dev.max_speed_hz = speed
        received = bytes(dev.xfer2(list(pattern)))
    except OSError as e:
        state.console.print(f"[red]transfer failed:[/] {e}")
        raise SystemExit(1)
    finally:
        dev.close()

    match = received == pattern
    if state.as_json:
        state.emit({"bus": bus, "cs": cs, "sent": list(pattern),
                    "received": list(received), "loopback": match})
        return

    table = Table(box=box.SIMPLE, show_edge=False, header_style="dim")
    table.add_column("sent", style="cyan")
    table.add_column("received")
    table.add_column("", width=1)
    for tx, rx in zip(pattern, received):
        ok = tx == rx
        table.add_row(
            f"0x{tx:02X}",
            Text(f"0x{rx:02X}", style="green" if ok else "red"),
            Text("✓" if ok else "✗", style="green" if ok else "red"),
        )
    verdict = (
        Text("loopback OK — the bus works", style="bold green") if match
        else Text(
            "no loopback. Either MOSI and MISO aren't jumpered together, "
            "or the bus isn't wired through.",
            style="bold yellow",
        )
    )
    state.console.print(Panel(
        Group(table, Text(), verdict),
        title=f"spidev{bus}.{cs} · mode {mode} · {speed / 1e6:g} MHz",
        title_align="left",
        border_style="green" if match else "yellow",
        box=box.ROUNDED,
    ))
    if not match:
        raise SystemExit(1)


@spi.command("xfer")
@click.argument("target")
@click.argument("data", nargs=-1, required=True)
@click.option("--speed", type=int, default=1_000_000, show_default=True)
@click.option("--mode", type=click.IntRange(0, 3), default=0, show_default=True)
@click.option("--read", "read_extra", type=int, default=0,
              help="Clock out this many extra bytes to collect the reply.")
@click.option("--yes", is_flag=True, help="Required — this drives the bus and asserts CS.")
@pass_state
def spi_xfer(state: State, target, data, speed, mode, read_extra, yes):
    """Send bytes and show what came back. Requires --yes.

    Example — read a flash chip's JEDEC ID:  updev spi xfer 0.0 0x9f --read 3 --yes
    """
    try:
        import spidev
    except ImportError:
        state.console.print("[red]python3-spidev is not installed[/]")
        raise SystemExit(1)

    bus, cs = _parse_spi_target(target)
    payload = [_parse_int(d) & 0xFF for d in data] + [0x00] * max(0, read_extra)
    if not yes:
        hexed = " ".join(f"0x{b:02X}" for b in payload)
        state.console.print(
            f"[yellow]refusing to transmit[/] {hexed} on spidev{bus}.{cs}\n"
            "[dim]this asserts chip-select and clocks real data at whatever is wired "
            "up. Re-run with[/] [bold cyan]--yes[/] [dim]once you're sure.[/]"
        )
        raise SystemExit(2)

    dev = spidev.SpiDev()
    try:
        dev.open(bus, cs)
        dev.mode = mode
        dev.max_speed_hz = speed
        received = bytes(dev.xfer2(list(payload)))
    except OSError as e:
        state.console.print(f"[red]transfer failed:[/] {e}")
        raise SystemExit(1)
    finally:
        try:
            dev.close()
        except Exception:
            pass

    if state.as_json:
        state.emit({"sent": payload, "received": list(received)})
        return
    state.console.print(_hex_panel(list(received), f"spidev{bus}.{cs} response"))


# ==========================================================================
# camera
# ==========================================================================

@cli.group()
def cam():
    """List, inspect and capture from cameras."""


@cam.command("list")
@pass_state
def cam_list(state: State):
    """Show every camera libcamera and V4L2 know about."""
    result = state.scan(deep=True, include=frozenset({"camera"}))
    cameras = [d for d in result.devices if d.kind == Kind.CAMERA]
    if state.as_json:
        state.emit({"cameras": [d.as_dict() for d in cameras]})
        return
    real = [d for d in cameras if "helper" not in d.tags]
    helpers = [d for d in cameras if "helper" in d.tags]
    state.console.print(render.device_table(real, title="cameras", show_kind=False,
                                            show_node=True))
    if helpers:
        state.console.print()
        state.console.print(Text(
            f"plus {len(helpers)} ISP/codec pipeline nodes "
            "(not cameras — `updev scan --deep -b camera` to see them)",
            style="dim italic",
        ))
    for dev in real:
        for issue in dev.issues:
            state.console.print(
                Text(f"  {issue.message}", style=render.SEVERITY_STYLE[issue.severity])
            )
            if issue.fix:
                state.console.print(Text(f"  $ {issue.fix}", style="bold cyan"))


@cam.command("modes")
@click.argument("camera", default="0")
@pass_state
def cam_modes(state: State, camera):
    """List the sensor modes a camera supports."""
    result = state.scan(deep=True, include=frozenset({"camera"}))
    matches = [d for d in result.devices if d.kind == Kind.CAMERA and (
        d.address == camera or camera in d.uid or camera in d.name
    )]
    if not matches:
        state.console.print(f"[red]no camera matches[/] {camera}")
        raise SystemExit(1)
    dev = matches[0]
    modes = dev.detail.get("modes") or dev.detail.get("resolutions") or []
    if state.as_json:
        state.emit({"camera": dev.uid, "modes": modes})
        return
    if not modes:
        state.console.print(f"[yellow]no mode information for[/] {dev.label}")
        return
    table = Table(box=box.SIMPLE, header_style="dim", show_edge=False)
    table.add_column("#", style="dim", justify="right")
    table.add_column("mode", style="bold")
    for i, mode in enumerate(modes):
        table.add_row(str(i), str(mode))
    state.console.print(Panel(table, title=dev.label, title_align="left",
                              border_style="bright_magenta", box=box.ROUNDED))


@cam.command("capture")
@click.argument("camera", default="0")
@click.option("-o", "--output", default="capture.jpg", show_default=True,
              type=click.Path(dir_okay=False))
@click.option("--width", type=int, default=None, help="Capture width in pixels.")
@click.option("--height", type=int, default=None, help="Capture height in pixels.")
@click.option("--warmup", type=float, default=2.0, show_default=True,
              help="Seconds to let auto-exposure settle.")
@pass_state
def cam_capture(state: State, camera, output, width, height, warmup):
    """Grab a still image."""
    import os
    os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:ERROR")
    try:
        from picamera2 import Picamera2
    except ImportError:
        state.console.print("[red]picamera2 is not installed[/]")
        raise SystemExit(1)

    try:
        index = int(camera)
    except ValueError:
        index = 0

    infos = Picamera2.global_camera_info()
    if not infos:
        state.console.print("[red]no cameras detected[/]")
        state.console.print("[dim]run[/] [bold cyan]updev doctor[/] [dim]for the likely reason[/]")
        raise SystemExit(1)
    if index >= len(infos):
        state.console.print(f"[red]camera {index} does not exist[/] (found {len(infos)})")
        raise SystemExit(1)

    picam = Picamera2(index)
    try:
        config_kwargs = {}
        if width and height:
            config_kwargs["main"] = {"size": (width, height)}
        picam.configure(picam.create_still_configuration(**config_kwargs))
        picam.start()
        with state.console.status(f"[cyan]exposing for {warmup:g}s…"):
            time.sleep(warmup)
        picam.capture_file(output)
    finally:
        try:
            picam.stop()
            picam.close()
        except Exception:
            pass

    size = Path(output).stat().st_size if Path(output).exists() else 0
    if state.as_json:
        state.emit({"camera": index, "output": output, "bytes": size})
        return
    state.console.print(
        f"[green]captured[/] {infos[index].get('Model', 'camera')} → "
        f"[bold]{output}[/] [dim]({size / 1024:.0f} KB)[/]"
    )


# ==========================================================================
# network
# ==========================================================================

@cli.group()
def net():
    """Interfaces and the neighbours you can reach."""


@net.command("scan")
@click.option("--cidr", default="", help="Subnet to sweep, e.g. 192.168.1.0/24.")
@click.option("--iface", default="", help="Sweep the subnet on this interface.")
@click.option("--ports/--no-ports", default=True, show_default=True,
              help="TCP-connect to common ports on each host found.")
@click.option("--names/--no-names", default=True, show_default=True,
              help="Reverse-DNS each host.")
@click.option("--concurrency", type=int, default=256, show_default=True)
@pass_state
def net_scan(state: State, cidr, iface, ports, names, concurrency):
    """Sweep the local subnet for live hosts.

    Unprivileged: ICMP via the `ping` binary, then the kernel's own ARP table
    for MAC addresses. Only scan networks you're responsible for.
    """
    if iface and not cidr:
        cidr = _cidr_for_iface(iface)
        if not cidr:
            state.console.print(f"[red]no IPv4 subnet on[/] {iface}")
            raise SystemExit(1)

    result = state.scan(
        deep=ports, lan_cidr=cidr, resolve_names=names,
        lan_concurrency=concurrency, include=frozenset({"lan"}),
    )
    hosts = [d for d in result.devices if d.kind == Kind.NET_HOST and d.uid != "lan:subnet"]
    subnet = next((d for d in result.devices if d.uid == "lan:subnet"), None)

    if state.as_json:
        state.emit({"subnet": subnet.as_dict() if subnet else None,
                    "hosts": [d.as_dict() for d in hosts]})
        return

    failed = [r for r in result.reports if not r.ok]
    for rep in failed:
        state.console.print(f"[red]{rep.name}:[/] {rep.error}")
    if not hosts:
        state.console.print("[yellow]no hosts found[/]")
        return

    table = Table(box=box.SIMPLE_HEAD, header_style="dim bold", expand=True,
                  show_edge=False, pad_edge=False)
    table.add_column("", width=1)
    table.add_column("address", style="bold", no_wrap=True)
    table.add_column("hostname", no_wrap=True, max_width=28)
    table.add_column("mac", style="dim", no_wrap=True)
    table.add_column("vendor", no_wrap=True, max_width=22)
    table.add_column("open ports", overflow="ellipsis")
    for dev in sorted(hosts, key=lambda d: tuple(int(p) for p in d.address.split("."))):
        open_ports = dev.detail.get("open_ports") or []
        if "this-host" in dev.tags:
            who = Text("this Pi", style="bold magenta")
        elif dev.vendor:
            who = Text(dev.vendor)
        else:
            who = Text(dev.detail.get("mac_note", ""), style="dim")
        table.add_row(
            render.status_text(dev.status),
            Text(dev.address, style="bold magenta" if "this-host" in dev.tags else "bold"),
            Text(dev.detail.get("hostname", ""), style="cyan"),
            dev.detail.get("mac", ""),
            who,
            Text(", ".join(open_ports), style="green"),
        )
    title = Text(subnet.name if subnet else "LAN", style="bold")
    if subnet:
        title.append(f"  {subnet.summary}", style="dim")
    state.console.print(Panel(table, title=title, title_align="left",
                              border_style="bright_blue", box=box.ROUNDED))


# ==========================================================================
# bluetooth / serial / gpio / power
# ==========================================================================

@cli.group()
def bt():
    """Bluetooth controllers and paired devices."""


@bt.command("scan")
@click.option("-d", "--duration", type=float, default=8.0, show_default=True,
              help="Seconds to leave discovery running.")
@pass_state
def bt_scan(state: State, duration):
    """Discover nearby Bluetooth devices."""
    import subprocess

    from .core.util import have, mac_vendor

    if not have("bluetoothctl"):
        state.console.print("[red]bluetoothctl not found[/]")
        raise SystemExit(1)

    proc = subprocess.Popen(
        ["bluetoothctl", "--timeout", str(int(duration)), "scan", "on"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    with state.console.status(f"[cyan]discovering for {duration:g}s…"):
        try:
            out, _ = proc.communicate(timeout=duration + 10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""

    import re
    found: dict[str, str] = {}
    for m in re.finditer(r"Device\s+([0-9A-Fa-f:]{17})\s+(.+)", out or ""):
        addr, label = m.group(1), m.group(2).strip()
        if not re.fullmatch(r"[0-9A-F-]{17}", label.replace(":", "-")):
            found[addr] = label
        else:
            found.setdefault(addr, "")

    if state.as_json:
        state.emit({"devices": [{"address": a, "name": n} for a, n in sorted(found.items())]})
        return
    if not found:
        state.console.print("[yellow]nothing discovered[/] [dim]— is the controller powered on?[/]")
        return
    table = Table(box=box.SIMPLE, header_style="dim", show_edge=False)
    table.add_column("address", style="bold")
    table.add_column("name")
    table.add_column("vendor", style="dim")
    for addr, label in sorted(found.items()):
        table.add_row(addr, label or "[dim]unnamed[/]", mac_vendor(addr))
    state.console.print(Panel(table, title=f"discovered {len(found)}", title_align="left",
                              border_style="blue", box=box.ROUNDED))


@cli.group()
def serial():
    """Serial ports and UARTs."""


@serial.command("ports")
@pass_state
def serial_ports(state: State):
    """List serial ports with their USB provenance."""
    result = state.scan(include=frozenset({"serial"}))
    ports = [d for d in result.devices if d.kind == Kind.SERIAL]
    if state.as_json:
        state.emit({"ports": [d.as_dict() for d in ports]})
        return
    if not ports:
        state.console.print("[yellow]no serial ports found[/]")
        return
    state.console.print(render.device_table(ports, title="serial ports", show_kind=False,
                                            show_node=True))


@serial.command("monitor")
@click.argument("port")
@click.option("-b", "--baud", type=int, default=115200, show_default=True)
@click.option("-d", "--duration", type=float, default=0.0,
              help="Stop after this many seconds (0 = until Ctrl-C).")
@click.option("--hex", "as_hex", is_flag=True, help="Show raw bytes instead of text.")
@pass_state
def serial_monitor(state: State, port, baud, duration, as_hex):
    """Read from a serial port and print what arrives. Receive-only."""
    try:
        import serial as pyserial
    except ImportError:
        state.console.print("[red]pyserial is not installed[/]")
        raise SystemExit(1)

    node = port if port.startswith("/dev/") else f"/dev/{port}"
    try:
        conn = pyserial.Serial(node, baud, timeout=0.5)
    except Exception as e:
        state.console.print(f"[red]cannot open {node}:[/] {e}")
        raise SystemExit(1)

    state.console.print(
        f"[green]listening[/] on [bold]{node}[/] at {baud} baud "
        f"[dim](receive-only, Ctrl-C to stop)[/]"
    )
    deadline = time.time() + duration if duration else None
    try:
        with conn:
            while deadline is None or time.time() < deadline:
                data = conn.read(256)
                if not data:
                    continue
                if as_hex:
                    click.echo(" ".join(f"{b:02x}" for b in data))
                else:
                    sys.stdout.write(data.decode("utf-8", "replace"))
                    sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    state.console.print("\n[dim]closed[/]")


@cli.group()
def gpio():
    """The 40-pin header and GPIO chips."""


@gpio.command("pins")
@pass_state
def gpio_pins(state: State):
    """Draw the 40-pin header with each pin's live state."""
    from .backends.gpio import PIN_FUNCTIONS, _HEADER, read_pin_state

    pins = read_pin_state()
    if state.as_json:
        state.emit({
            "pins": {
                str(pin): {
                    "label": PIN_FUNCTIONS[pin][0],
                    "function": PIN_FUNCTIONS[pin][1],
                    **(pins.get(_HEADER[pin], {}) if pin in _HEADER else {}),
                }
                for pin in sorted(PIN_FUNCTIONS)
            }
        })
        return
    if not pins:
        state.console.print("[yellow]`pinctrl` is unavailable — cannot read pin state[/]")
        state.console.print("[dim]this command needs a Raspberry Pi with raspi-utils installed[/]")
        raise SystemExit(1)

    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, header_style="dim bold")
    for col in ("state", "function", "name", "pin", "", "pin", "name", "function", "state"):
        table.add_column(col, justify="center" if col == "pin" else "left", no_wrap=True)

    for left in range(1, 40, 2):
        right = left + 1
        lc = _pin_cells(left, pins)
        rc = _pin_cells(right, pins)
        table.add_row(
            lc["state"], lc["function"], lc["name"],
            Text(f"{left:>2}", style="bold"),
            Text("│", style="dim"),
            Text(f"{right:<2}", style="bold"),
            rc["name"], rc["function"], rc["state"],
        )

    legend = Text()
    legend.append("  power ", style="bold red")
    legend.append(" ground ", style="bold white on grey23")
    legend.append(" input ", style="green")
    legend.append(" output ", style="bold yellow")
    legend.append(" alt-function ", style="bold cyan")
    legend.append(" unused", style="dim")
    state.console.print(Panel(
        Group(table, Text(), legend),
        title="40-pin header", title_align="left",
        border_style="cyan", box=box.ROUNDED,
    ))


def _pin_cells(pin: int, pins: dict) -> dict[str, Text]:
    from .backends.gpio import PIN_FUNCTIONS, _HEADER

    label, func = PIN_FUNCTIONS.get(pin, ("?", ""))
    if label in ("3V3", "5V"):
        return {"name": Text(label, style="bold red"),
                "function": Text(func, style="dim"),
                "state": Text("", style="dim")}
    if label == "GND":
        return {"name": Text(label, style="bold white on grey23"),
                "function": Text(func, style="dim"),
                "state": Text("", style="dim")}

    gpio_num = _HEADER.get(pin)
    info = pins.get(gpio_num, {}) if gpio_num is not None else {}
    mode = info.get("function", "?")
    level = info.get("level", "")
    alt = info.get("alt", "")

    if mode == "input":
        style, shown = "green", f"in {level}"
    elif mode == "output":
        style, shown = "bold yellow", f"out {level}"
    elif mode.startswith("alt"):
        style, shown = "bold cyan", (alt or mode)
    else:
        style, shown = "dim", "-"
    pull = {"pull-up": "↑", "pull-down": "↓"}.get(info.get("pull", ""), "")
    if pull and mode in ("input", "output"):
        shown += f" {pull}"
    return {
        "name": Text(label, style="bold" if mode != "none" else "dim"),
        "function": Text(func, style="dim"),
        "state": Text(shown, style=style),
    }


@cli.command()
@pass_state
def power(state: State):
    """Per-rail PMIC breakdown and total board draw."""
    result = state.scan(include=frozenset({"host"}))
    dev = result.by_uid().get("host:power")
    if not dev:
        state.console.print("[yellow]no PMIC telemetry[/] [dim](vcgencmd unavailable?)[/]")
        raise SystemExit(1)
    if state.as_json:
        state.emit(dev.as_dict())
        return
    state.console.print(render.device_detail(dev))


# ==========================================================================
# panel — the ST7735S front panel
# ==========================================================================

_PANEL_WIRING = """[dim]VCC[/]→3V3  [dim]GND[/]→GND  [dim]SCL[/]→GPIO11  [dim]SDA[/]→GPIO10
[dim]RES[/]→GPIO25  [dim]DC[/]→GPIO24  [dim]CS[/]→GPIO8 (CE0)  [dim]BLK[/]→3V3"""


def _panel_options(f):
    """Wiring flags shared by every panel subcommand."""
    for option in reversed([
        click.option("--spi", "target", default="0.0", show_default=True,
                     metavar="BUS.CS", help="Which spidev node the panel is on."),
        click.option("--dc", type=int, default=24, show_default=True,
                     help="BCM pin wired to DC."),
        click.option("--rst", type=int, default=25, show_default=True,
                     help="BCM pin wired to RES."),
        click.option("--backlight", type=int, default=None,
                     help="BCM pin wired to BLK, if you drive it."),
        click.option("--speed", type=int, default=8_000_000, show_default=True,
                     help="SPI clock in Hz."),
        click.option("--bgr", is_flag=True,
                     help="Swap red and blue — some ST7735S panels are wired BGR."),
        click.option("--capture", type=click.Path(file_okay=False), default=None,
                     help="Also save every frame here as PNG."),
    ]):
        f = option(f)
    return f


def _open_panel(state: State, target, dc, rst, backlight, speed, bgr, capture,
                required: bool = True):
    from .panel import PanelUnavailable, open_panel

    port, cs = _parse_spi_target(target)
    try:
        screen = open_panel(port=port, cs=cs, dc=dc, rst=rst, backlight=backlight,
                            speed_hz=speed, bgr=bgr,
                            capture=Path(capture) if capture else None,
                            required=required)
    except PanelUnavailable as e:
        state.console.print(f"[red]no panel:[/] {e}")
        state.console.print(Panel(_PANEL_WIRING, title="expected wiring",
                                  title_align="left", border_style="dim",
                                  box=box.ROUNDED))
        raise SystemExit(1) from e
    if not screen.live:
        state.console.print("[yellow]no panel — drawing to memory only[/]")
    return screen


@cli.group()
def panel():
    """The ST7735S front panel: what the board is doing, on the glass.

    A 128x160 SPI display showing the same scan the CLI and GUI show. `updev
    panel boot` is meant to run at startup — it leaves its summary on screen
    after it exits, so the panel keeps reading as a status display with
    nothing running.
    """


@panel.command("boot")
@_panel_options
@click.option("--splash", type=float, default=0.8, show_default=True,
              help="Minimum seconds to hold the splash before progress rows.")
@click.option("--hold", type=float, default=0.0,
              help="Seconds to sit on the summary before exiting.")
@click.option("--blank", is_flag=True,
              help="Clear the panel on exit instead of leaving the summary up.")
@pass_state
def panel_boot(state: State, target, dc, rst, backlight, speed, bgr, capture,
               splash, hold, blank):
    """Boot screen: splash, live backend progress, then the summary.

    The progress rows are the useful part — each backend ticks over as its
    report lands, so a bus that hangs is named on screen instead of showing up
    as a display that never changes.
    """
    from .panel import boot as run_boot

    screen = _open_panel(state, target, dc, rst, backlight, speed, bgr, capture)
    try:
        result = run_boot(screen, state.scanner, state.ctx,
                          splash_s=splash, hold_s=hold)
    finally:
        screen.close(blank=blank)

    if state.as_json:
        state.emit(result.as_dict())
        return
    ok = sum(1 for r in result.reports if r.available and r.ok)
    state.console.print(
        f"[green]panel[/] {len(result.devices)} devices · "
        f"{ok}/{len(result.reports)} backends · {result.duration:.1f}s"
        + ("" if blank else " [dim](summary left on screen)[/]")
    )


@panel.command("test")
@_panel_options
@pass_state
def panel_test(state: State, target, dc, rst, backlight, speed, bgr, capture):
    """Colour bars and a pixel grid — is it wired right, and is it BGR?

    If the bars read blue-green-red instead of red-green-blue, the panel is
    BGR: re-run with `--bgr`.
    """
    from .panel.screen import ACCENT, BLACK, DIM, FG, WIDTH

    screen = _open_panel(state, target, dc, rst, backlight, speed, bgr, capture)
    try:
        screen.clear(BLACK)
        bars = [("R", (255, 0, 0)), ("G", (0, 255, 0)), ("B", (0, 0, 255)),
                ("W", (255, 255, 255))]
        for i, (label, colour) in enumerate(bars):
            screen.fill((0, i * 20, WIDTH, i * 20 + 19), colour)
            screen.text(4, i * 20 + 4, label, BLACK, size=11, bold=True)
        y = 88
        y = screen.text(4, y, f"spidev{target}", FG, size=9)
        y = screen.text(4, y, f"DC {dc}  RST {rst}", DIM, size=9)
        y = screen.text(4, y, f"{speed / 1e6:.0f} MHz", DIM, size=9)
        y = screen.text(4, y, "BGR" if bgr else "RGB", DIM, size=9)
        # Corner marks: if any is clipped, the offsets are wrong for this panel.
        for cx, cy in ((0, 0), (WIDTH - 1, 0), (0, 159), (WIDTH - 1, 159)):
            screen.fill((cx - 3, cy - 3, cx + 3, cy + 3), ACCENT)
        screen.flush()
    finally:
        screen.close()
    state.console.print(
        "[green]drew test pattern[/] [dim]— bars top to bottom should read "
        "red, green, blue, white; all four corner marks should be visible[/]"
    )


@panel.command("off")
@_panel_options
@pass_state
def panel_off(state: State, target, dc, rst, backlight, speed, bgr, capture):
    """Blank the panel and let go of it."""
    screen = _open_panel(state, target, dc, rst, backlight, speed, bgr, capture)
    screen.close(blank=True)
    state.console.print("[dim]panel cleared[/]")


# ==========================================================================
# helpers
# ==========================================================================

def _filter(devices, kinds, statuses, tags):
    out = devices
    if kinds:
        wanted = {k.lower() for k in kinds}
        out = [d for d in out if str(d.kind) in wanted]
    if statuses:
        wanted = {s.lower() for s in statuses}
        out = [d for d in out if str(d.status) in wanted]
    if tags:
        wanted = {t.lower() for t in tags}
        out = [d for d in out if wanted & {t.lower() for t in d.tags}]
    return out


def _parse_int(text: str) -> int:
    """Accept 0x3c, 60, 0b1010 — whatever the datasheet used."""
    text = str(text).strip()
    try:
        return int(text, 0)
    except ValueError:
        try:
            return int(text, 16)
        except ValueError as e:
            raise click.BadParameter(f"{text!r} is not a number") from e


def _parse_spi_target(target: str) -> tuple[int, int]:
    cleaned = target.removeprefix("/dev/").removeprefix("spidev")
    if "." not in cleaned:
        raise click.BadParameter(f"expected BUS.CS (e.g. 0.0), got {target!r}")
    bus, _, cs = cleaned.partition(".")
    try:
        return int(bus), int(cs)
    except ValueError as e:
        raise click.BadParameter(f"bad SPI target {target!r}") from e


def _hex_panel(data: list[int], title: str, base: int = 0) -> Panel:
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, header_style="dim")
    table.add_column("", style="dim")
    for col in range(16):
        table.add_column(f"{col:x}", justify="center", width=2)
    table.add_column("ascii", style="dim")

    for row_start in range(0, len(data), 16):
        chunk = data[row_start:row_start + 16]
        cells = [f"{base + row_start:04x}"]
        ascii_bits = []
        for value in chunk:
            if value < 0:
                cells.append(Text("--", style="dim"))
                ascii_bits.append(".")
            else:
                cells.append(Text(f"{value:02x}", style="cyan" if value else "dim"))
                ascii_bits.append(chr(value) if 32 <= value < 127 else ".")
        cells.extend([""] * (16 - len(chunk)))
        cells.append("".join(ascii_bits))
        table.add_row(*cells)
    return Panel(table, title=title, title_align="left", border_style="bright_green",
                 box=box.ROUNDED)


def _cidr_for_iface(iface: str) -> str:
    import ipaddress
    import socket

    try:
        import psutil
    except ImportError:
        return ""
    for addr in psutil.net_if_addrs().get(iface, []):
        if getattr(addr, "family", None) == socket.AF_INET and addr.netmask:
            try:
                net = ipaddress.ip_network(f"{addr.address}/{addr.netmask}", strict=False)
                return str(net)
            except ValueError:
                continue
    return ""


def main() -> None:
    try:
        cli()
    except KeyboardInterrupt:
        click.echo("\ninterrupted", err=True)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
