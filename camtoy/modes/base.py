"""The mode contract and the loop that drives it.

A mode is a small stateful object: frames go in, frames come out, keys poke
at whatever it keeps in between. It never touches the camera or the display,
so every mode works identically in a terminal and in a window.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from .. import imageops as io
from ..capture import Camera
from ..render.base import Display

DEFAULT_OUTDIR = Path("camtoy-shots")


class Mode:
    """Base class. Subclasses override render() and usually on_key()."""

    name = "mode"
    # (key, description) pairs, shown in the status bar and in --help.
    keys: tuple[tuple[str, str], ...] = ()

    def work_size(self, display_size: tuple[int, int]) -> tuple[int, int]:
        """Resolution to hand render(). Defaults to whatever the display wants,
        which keeps the numpy work proportional to what is actually visible."""
        return display_size

    def render(self, frame: np.ndarray) -> np.ndarray:
        return frame

    def on_key(self, key: str) -> str | None:
        """Handle a keystroke. Return a short message to flash, if any."""
        return None

    def status(self) -> str:
        return ""

    def close(self) -> None:
        pass


def save_png(rgb: np.ndarray, outdir: Path, prefix: str) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}.png"
    Image.fromarray(io.to_u8(rgb)).save(path)
    return path


class _Fps:
    """Frame rate over a short sliding window, so it settles but still reacts."""

    def __init__(self, window: float = 1.0) -> None:
        self.window, self._marks, self.value = window, [], 0.0

    def tick(self) -> float:
        now = time.monotonic()
        self._marks.append(now)
        cutoff = now - self.window
        while self._marks and self._marks[0] < cutoff:
            self._marks.pop(0)
        if len(self._marks) > 1:
            self.value = (len(self._marks) - 1) / (self._marks[-1] - self._marks[0])
        return self.value


def run(mode: Mode, camera: Camera, display: Display, show_fps: bool = True) -> str:
    """Pump frames from camera through mode into display until asked to stop.

    Returns a human-readable reason for stopping.
    """
    fps = _Fps()
    toast, toast_until = "", 0.0

    for frame in camera.frames():
        if not display.running:
            return "closed"

        for key in display.keys():
            if key in {"esc", "q"}:
                return "quit"
            if message := mode.on_key(key):
                toast, toast_until = message, time.monotonic() + 2.0

        work = mode.work_size(display.target_size())
        out = mode.render(io.resize(frame, *work))

        fps.tick()
        if time.monotonic() > toast_until:
            toast = ""
        display.status(_status_line(mode, fps.value if show_fps else None, toast))
        display.show(out)

    return camera.diagnosis()


def _status_line(mode: Mode, fps: float | None, toast: str) -> str:
    parts = [mode.name]
    if fps is not None:
        parts.append(f"{fps:4.1f}fps")
    if detail := mode.status():
        parts.append(detail)
    parts.append("q quit")
    line = "  ".join(parts)
    return f"{line}   ** {toast} **" if toast else line
