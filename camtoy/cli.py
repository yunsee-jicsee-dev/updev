"""The `camtoy` command line.

Every mode takes the same camera and display options, so they live in one
decorator and each command adds only what is genuinely its own.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .capture import Camera, CameraMode, CaptureError, pick_mode, probe_modes
from .modes import MODES, Mode, mode_keys, nn_mode, run
from .modes.base import DEFAULT_OUTDIR
from .render import open_display

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}
console = Console()


def parse_size(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise click.BadParameter(f"expected WIDTHxHEIGHT, got {text!r}") from None


def camera_options(f):
    """Options shared by every mode."""
    for option in reversed([
        click.option("-d", "--device", default="/dev/video0", show_default=True,
                     help="V4L2 capture device."),
        click.option("--size", default=None, metavar="WxH",
                     help="Capture size. Default: the camera's fastest usable mode."),
        click.option("--fps", type=float, default=None,
                     help="Requested frame rate. Cheap sensors deliver less than they promise."),
        click.option("--mirror/--no-mirror", default=True, show_default=True,
                     help="Flip horizontally so the picture behaves like a mirror."),
        click.option("--display", "display_kind", type=click.Choice(["term", "window"]),
                     default="term", show_default=True, help="Where to draw."),
        click.option("--scale", type=float, default=2.0, show_default=True,
                     help="Window magnification (--display window only)."),
        click.option("--max-width", type=int, default=0,
                     help="Cap terminal columns used. 0 means use the whole width."),
        click.option("--outdir", type=click.Path(path_type=Path), default=DEFAULT_OUTDIR,
                     show_default=True, help="Where saved PNGs go."),
    ]):
        f = option(f)
    return f


def build_camera(device: str, size: str | None, fps: float | None, mirror: bool) -> Camera:
    available = probe_modes(device)
    if wanted := parse_size(size):
        chosen = CameraMode(wanted[0], wanted[1], fps or 30.0,
                            available[0].pixfmt if available else "yuyv422")
    else:
        chosen = pick_mode(available)
        if fps:
            chosen = CameraMode(chosen.width, chosen.height, fps, chosen.pixfmt)
    return Camera(device=device, mode=chosen, mirror=mirror)


def _terminate(signum, frame):
    """Turn a kill signal into an exception so `finally` still runs.

    Dying inside the signal handler would skip teardown, and teardown is what
    restores the terminal and writes out an unsaved light painting.
    """
    raise SystemExit(128 + signum)


def launch(mode_factory, *, display_kind: str, scale: float, max_width: int,
           device: str, size: str | None, fps: float | None, mirror: bool) -> None:
    """Open camera and display, run the mode, and always tear down in order."""
    camera = build_camera(device, size, fps, mirror)
    try:
        camera.open()
    except CaptureError as e:
        raise click.ClickException(str(e)) from None

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous[sig] = signal.signal(sig, _terminate)
        except (ValueError, OSError):
            pass

    mode: Mode | None = None
    display = None
    reason = "quit"
    try:
        display = open_display(
            display_kind,
            scale=scale, max_width=max_width, title=f"camtoy — {camera.mode}",
            width=camera.mode.width, height=camera.mode.height,
        )
        mode = mode_factory(camera)
        reason = run(mode, camera, display)
    finally:
        # Order matters. The display goes first because it owns the alt screen
        # and raw mode, and anything printed after that — a traceback, an
        # autosave message — should land in the user's real shell. Catches
        # BaseException too, so ctrl-C keeps the painting.
        if display is not None:
            display.close()
        if mode is not None:
            mode.close()
        camera.close()
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, TypeError):
                pass

    if reason not in {"quit", "closed"}:
        console.print(f"[yellow]camera stopped:[/] {reason}")


def keys_epilog(name: str) -> str:
    # A lone \b tells click to stop rewrapping the paragraph that follows it,
    # which is the only way to keep a key table looking like a key table.
    rows = list(mode_keys(name)) + [("q/esc", "quit")]
    body = "\n".join(f"  {key:<7} {desc}" for key, desc in rows)
    return f"Keys:\n\n\b\n{body}"


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(__version__, "-V", "--version", prog_name="camtoy")
def main() -> None:
    """Camera toys: no OpenCV, no models, just numpy and a cheap webcam."""


@main.command()
@camera_options
def live(device, size, fps, mirror, display_kind, scale, max_width, outdir):
    """Live video with togglable effects: levels, edges, palettes, dither, trails."""
    from .modes import LiveMode
    launch(lambda cam: LiveMode(outdir=outdir), display_kind=display_kind, scale=scale,
           max_width=max_width, device=device, size=size, fps=fps, mirror=mirror)


@main.command()
@camera_options
@click.option("--depth", type=int, default=120, show_default=True,
              help="How many frames the time buffer spans.")
@click.option("--axis", type=click.Choice(["row", "col"]), default="row", show_default=True,
              help="Shear across rows or columns.")
def slit(device, size, fps, mirror, display_kind, scale, max_width, outdir, depth, axis):
    """Slit-scan: every row of the picture comes from a different moment."""
    from .modes import SlitScanMode
    launch(lambda cam: SlitScanMode(depth=depth, axis=axis, outdir=outdir),
           display_kind=display_kind, scale=scale, max_width=max_width,
           device=device, size=size, fps=fps, mirror=mirror)


@main.command()
@camera_options
@click.option("--blend", type=click.Choice(["lighten", "add", "decay"]), default="lighten",
              show_default=True, help="How new light joins the exposure.")
@click.option("--autosave/--no-autosave", default=True, show_default=True,
              help="Write the painting to --outdir on exit.")
def paint(device, size, fps, mirror, display_kind, scale, max_width, outdir, blend, autosave):
    """Long-exposure light painting. Darken the room, wave a torch."""
    from .modes import LightPaintMode
    launch(lambda cam: LightPaintMode(full_size=(cam.mode.width, cam.mode.height),
                                      blend=blend, outdir=outdir, autosave=autosave),
           display_kind=display_kind, scale=scale, max_width=max_width,
           device=device, size=size, fps=fps, mirror=mirror)


@main.command()
@camera_options
@click.option("--voices", type=int, default=8, show_default=True,
              help="Vertical bands, one note each.")
@click.option("--threshold", type=int, default=14, show_default=True,
              help="Per-pixel change that counts as motion. Raise it on a noisy sensor.")
def theremin(device, size, fps, mirror, display_kind, scale, max_width, outdir, voices, threshold):
    """Play the room: motion in each band drives a note on a pentatonic scale."""
    from .modes import ThereminMode
    launch(lambda cam: ThereminMode(voices=voices, threshold=threshold),
           display_kind=display_kind, scale=scale, max_width=max_width,
           device=device, size=size, fps=fps, mirror=mirror)


@main.command()
@click.option("-d", "--device", default="/dev/video0", show_default=True)
def devices(device):
    """List what the camera says it can do, and what camtoy would pick."""
    modes = probe_modes(device)
    if not modes:
        console.print(f"[yellow]no modes reported for {device}[/] — "
                      "is v4l2-ctl installed and the camera plugged in?")
        return

    chosen = pick_mode(modes)
    table = Table(title=f"{device} capture modes", header_style="bold cyan")
    table.add_column("size", justify="right")
    table.add_column("fps", justify="right")
    table.add_column("format")
    table.add_column("")
    for m in modes:
        picked = m == chosen
        table.add_row(m.size, f"{m.fps:g}", m.pixfmt,
                      "[green]default[/]" if picked else "",
                      style="bold" if picked else None)
    console.print(table)
    console.print("[dim]Advertised rates are optimistic; USB bandwidth decides the real one.[/]")


# --------------------------------------------------------------------------
# neural network modes
# --------------------------------------------------------------------------

def nn_launch(name: str, factory_kwargs: dict, common: dict) -> None:
    """Build a neural mode and run it, translating model problems into
    ordinary CLI errors rather than tracebacks."""
    from .nn.session import ModelBroken, ModelMissing

    cls = nn_mode(name)
    try:
        launch(lambda cam: cls(outdir=common["outdir"], **factory_kwargs),
               display_kind=common["display_kind"], scale=common["scale"],
               max_width=common["max_width"], device=common["device"],
               size=common["size"], fps=common["fps"], mirror=common["mirror"])
    except (ModelMissing, ModelBroken) as e:
        raise click.ClickException(str(e)) from None
    except RuntimeError as e:
        if "onnxruntime" in str(e):
            raise click.ClickException(str(e)) from None
        raise


def _common(device, size, fps, mirror, display_kind, scale, max_width, outdir) -> dict:
    return dict(device=device, size=size, fps=fps, mirror=mirror,
                display_kind=display_kind, scale=scale, max_width=max_width,
                outdir=outdir)


@main.command()
@camera_options
@click.option("--model", type=click.Choice(["nanodet", "yolox"]), default="nanodet",
              show_default=True,
              help="nanodet is ~6fps here; yolox is more accurate and ~0.5fps.")
@click.option("--threshold", type=float, default=0.35, show_default=True,
              help="Minimum detection score.")
def detect(device, size, fps, mirror, display_kind, scale, max_width, outdir, model, threshold):
    """Find and label objects: 80 COCO classes."""
    nn_launch("detect", dict(model=model, threshold=threshold),
              _common(device, size, fps, mirror, display_kind, scale, max_width, outdir))


@main.command()
@camera_options
@click.option("--model", type=click.Choice(["classify/mobilenetv2", "classify/ppresnet50"]),
              default="classify/mobilenetv2", show_default=True)
def classify(device, size, fps, mirror, display_kind, scale, max_width, outdir, model):
    """Name what the camera is looking at: 1000 ImageNet classes."""
    nn_launch("classify", dict(model=model),
              _common(device, size, fps, mirror, display_kind, scale, max_width, outdir))


@main.command()
@camera_options
@click.option("--expression/--no-expression", default=True, show_default=True,
              help="Also read the expression on each face.")
def face(device, size, fps, mirror, display_kind, scale, max_width, outdir, expression):
    """Detect faces, read expressions, and recognise ones you press 'r' on."""
    nn_launch("face", dict(expression=expression),
              _common(device, size, fps, mirror, display_kind, scale, max_width, outdir))


@main.command()
@camera_options
def pose(device, size, fps, mirror, display_kind, scale, max_width, outdir):
    """33-point body skeleton."""
    nn_launch("pose", {},
              _common(device, size, fps, mirror, display_kind, scale, max_width, outdir))


@main.command()
@camera_options
def hand(device, size, fps, mirror, display_kind, scale, max_width, outdir):
    """21-point hand skeleton, up to two hands."""
    nn_launch("hand", {},
              _common(device, size, fps, mirror, display_kind, scale, max_width, outdir))


@main.command()
@camera_options
@click.option("--background", type=click.Choice(["blur", "tint", "black", "thermal"]),
              default="blur", show_default=True, help="What to do behind the person.")
def segment(device, size, fps, mirror, display_kind, scale, max_width, outdir, background):
    """Cut the person out of the background."""
    nn_launch("segment", dict(background=background),
              _common(device, size, fps, mirror, display_kind, scale, max_width, outdir))


@main.command()
@camera_options
@click.option("--side", type=int, default=266, show_default=True,
              help="Inference resolution, rounded to a multiple of 14. Lower is faster.")
def depth(device, size, fps, mirror, display_kind, scale, max_width, outdir, side):
    """Monocular depth: how far away everything is, in false colour."""
    nn_launch("depth", dict(side=side),
              _common(device, size, fps, mirror, display_kind, scale, max_width, outdir))


@main.command()
@camera_options
@click.option("--style", "style_name",
              type=click.Choice(["mosaic", "candy", "udnie", "rain_princess", "pointilism"]),
              default="mosaic", show_default=True)
def style(device, size, fps, mirror, display_kind, scale, max_width, outdir, style_name):
    """Repaint the camera feed in the style of a painting."""
    nn_launch("style", dict(style=style_name),
              _common(device, size, fps, mirror, display_kind, scale, max_width, outdir))


@main.command()
@camera_options
def text(device, size, fps, mirror, display_kind, scale, max_width, outdir):
    """Find and read English text held up to the camera."""
    nn_launch("text", {},
              _common(device, size, fps, mirror, display_kind, scale, max_width, outdir))


@main.command()
@camera_options
def track(device, size, fps, mirror, display_kind, scale, max_width, outdir):
    """Aim the box with the arrow keys, press enter, and it follows."""
    nn_launch("track", {},
              _common(device, size, fps, mirror, display_kind, scale, max_width, outdir))


# --------------------------------------------------------------------------
# model management
# --------------------------------------------------------------------------

@main.group()
def models() -> None:
    """Download and inspect the neural network weights."""


@models.command("list")
@click.option("--task", default=None, help="Only show one task's models.")
def models_list(task):
    """What exists, what is downloaded, and how fast it runs here."""
    from .nn.registry import MODELS, by_task, total_bytes

    specs = by_task(task) if task else list(MODELS.values())
    if not specs:
        raise click.ClickException(f"no models for task {task!r}")

    table = Table(header_style="bold cyan", box=None, pad_edge=False)
    table.add_column("model")
    table.add_column("task")
    table.add_column("size", justify="right")
    table.add_column("speed", justify="right")
    table.add_column("state")
    for spec in specs:
        if spec.broken:
            speed, state = "—", "[red]unsupported[/]"
        else:
            speed = f"{spec.ms}ms" if spec.ms else "?"
            state = "[green]ready[/]" if spec.present else "[dim]not fetched[/]"
        table.add_row(spec.key, spec.task, f"{spec.megabytes:.0f}MB", speed, state)
    console.print(table)

    have = sum(s.size for s in specs if s.present)
    console.print(f"\n[dim]{have / 1048576:.0f}MB of {total_bytes(specs) / 1048576:.0f}MB "
                  f"present. Speeds are measured on this board; anything over "
                  f"250ms cannot keep up with the camera.[/]")


@models.command("pull")
@click.argument("keys", nargs=-1)
@click.option("--all", "everything", is_flag=True, help="Fetch every model.")
@click.option("--task", default=None, help="Fetch one task's models.")
def models_pull(keys, everything, task):
    """Download weights. With no arguments, fetches what the modes need."""
    from .nn.download import DownloadError, fetch, fetch_labels
    from .nn.registry import MODELS, by_task

    if keys:
        unknown = [k for k in keys if k not in MODELS]
        if unknown:
            raise click.ClickException(f"unknown model(s): {', '.join(unknown)}")
        wanted = [MODELS[k] for k in keys]
    elif task:
        wanted = by_task(task)
    elif everything:
        wanted = list(MODELS.values())
    else:
        # The default set: everything that can actually keep up with a camera.
        wanted = [m for m in MODELS.values() if m.realtime]

    todo = [m for m in wanted if not m.present and not m.broken]
    if not todo:
        console.print("[green]nothing to fetch — all present.[/]")
    else:
        total = sum(m.size for m in todo) / 1048576
        console.print(f"fetching {len(todo)} model(s), {total:.0f}MB")
        for spec in todo:
            with console.status(f"[cyan]{spec.key}[/] ({spec.megabytes:.0f}MB)"):
                try:
                    fetch(spec)
                except DownloadError as e:
                    console.print(f"[red]failed[/] {e}")
                    continue
            console.print(f"  [green]✓[/] {spec.key}")

    try:
        fetch_labels("imagenet")
    except Exception as e:
        console.print(f"[yellow]labels not fetched:[/] {e}")


@models.command("verify")
def models_verify():
    """Check every downloaded file is the size the registry expects."""
    from .nn.registry import MODELS

    bad = []
    for spec in MODELS.values():
        if not spec.path.exists():
            continue
        actual = spec.path.stat().st_size
        if actual != spec.size:
            bad.append((spec.key, actual, spec.size))
    if bad:
        for key, actual, expected in bad:
            console.print(f"[red]{key}[/]: {actual} bytes, expected {expected}")
        raise click.ClickException(f"{len(bad)} file(s) look truncated; re-pull them")
    present = sum(1 for s in MODELS.values() if s.present)
    console.print(f"[green]{present} model(s) verified.[/]")


# Show each mode's keybindings in its --help.
for _name, _cmd in (("live", live), ("slit", slit), ("paint", paint), ("theremin", theremin),
                    ("detect", detect), ("classify", classify), ("face", face),
                    ("pose", pose), ("hand", hand), ("segment", segment),
                    ("depth", depth), ("style", style), ("text", text), ("track", track)):
    _cmd.epilog = keys_epilog(_name)


if __name__ == "__main__":
    sys.exit(main())
