"""Slit-scan: one image, many moments.

A ring buffer holds the last N frames. The output takes its first row from the
newest frame, its last row from the oldest, and interpolates the rows between
— so the picture is a diagonal cut through time instead of a slice across it.
Wave a hand and it stretches into taffy; walk past and you leave a smear.

Low frame rates make this *better*, not worse. The wider the gap between
stored frames, the more violent the shear.
"""

from __future__ import annotations

import numpy as np

from .base import DEFAULT_OUTDIR, Mode, save_png

# Ring buffers get big fast: 480x360 RGB is half a megabyte per frame, so a
# 120-deep buffer is 62MB. Cap the allocation rather than the frame count and
# the same defaults stay sane on a small terminal and a large window alike.
MEMORY_BUDGET = 96 << 20


class SlitScanMode(Mode):
    name = "slit"
    keys = (
        ("a", "axis"), ("r", "reverse"), ("[ ]", "depth"),
        ("space", "freeze"), ("c", "clear"), ("s", "save png"),
    )

    def __init__(self, depth: int = 120, axis: str = "row", outdir=DEFAULT_OUTDIR) -> None:
        self.requested_depth = max(depth, 2)
        self.axis = axis
        self.reverse = False
        self.frozen = False
        self.outdir = outdir

        self._ring: np.ndarray | None = None
        self._head = 0
        self._filled = False
        self._last: np.ndarray | None = None

    @property
    def depth(self) -> int:
        return 0 if self._ring is None else self._ring.shape[0]

    # -- buffer ------------------------------------------------------------

    def _ensure_ring(self, frame: np.ndarray) -> None:
        """Allocate, or reallocate when the display geometry changed."""
        if self._ring is not None and self._ring.shape[1:] == frame.shape:
            return
        h, w = frame.shape[:2]
        affordable = max(MEMORY_BUDGET // max(h * w * 3, 1), 2)
        depth = int(min(self.requested_depth, affordable))
        self._ring = np.repeat(frame[None], depth, axis=0)   # start full, no black flash
        self._head = 0
        self._filled = True

    def render(self, frame: np.ndarray) -> np.ndarray:
        self._ensure_ring(frame)
        ring = self._ring
        depth = ring.shape[0]

        if not self.frozen:
            self._head = (self._head + 1) % depth
            ring[self._head] = frame

        h, w = frame.shape[:2]
        n = h if self.axis == "row" else w
        # Spread the whole buffer across the axis: line 0 is now, line n-1 is
        # `depth` frames ago.
        offsets = np.arange(n) * (depth - 1) // max(n - 1, 1)
        if self.reverse:
            offsets = offsets[::-1]
        idx = (self._head - offsets) % depth

        if self.axis == "row":
            out = ring[idx, np.arange(h)]
        else:
            out = ring[idx[None, :], np.arange(h)[:, None], np.arange(w)[None, :]]

        self._last = out
        return out

    # -- keys --------------------------------------------------------------

    def on_key(self, key: str) -> str | None:
        if key == "a":
            self.axis = "col" if self.axis == "row" else "row"
            return f"axis {self.axis}"
        if key == "r":
            self.reverse = not self.reverse
            return f"reverse {'on' if self.reverse else 'off'}"
        if key == "space":
            self.frozen = not self.frozen
            return "frozen" if self.frozen else "running"
        if key in "[]":
            step = 8 if key == "]" else -8
            self.requested_depth = max(self.requested_depth + step, 2)
            self._ring = None                      # rebuild at the new depth
            return f"depth {self.requested_depth}"
        if key == "c":
            self._ring = None
            return "cleared"
        if key == "s" and self._last is not None:
            return f"saved {save_png(self._last, self.outdir, 'slit').name}"
        return None

    def status(self) -> str:
        bits = [f"{self.axis} d{self.depth}"]
        if self.reverse:
            bits.append("rev")
        if self.frozen:
            bits.append("FROZEN")
        return " ".join(bits)
