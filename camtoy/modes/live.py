"""Live video with a stack of togglable numpy effects.

Each effect is independent and cheap; the fun is in the combinations. Edges
plus the thermal palette plus a long trail turns a webcam pointed at a hand
into something that looks nothing like a webcam pointed at a hand.
"""

from __future__ import annotations

import numpy as np

from .. import imageops as io
from .base import DEFAULT_OUTDIR, Mode, save_png

PALETTE_CYCLE = (None, "mono", "thermal", "amber", "green", "ice", "pop")
TRAIL_CYCLE = (0.0, 0.5, 0.8, 0.93)
DITHER_CYCLE = (0, 2, 4)          # 0 = off, else number of levels


class LiveMode(Mode):
    name = "live"
    keys = (
        ("l", "auto-levels"), ("e", "edges"), ("p", "palette"),
        ("d", "dither"), ("t", "trails"), ("i", "invert"),
        ("[ ]", "gamma"), ("s", "save png"),
    )

    def __init__(self, outdir=DEFAULT_OUTDIR) -> None:
        self.outdir = outdir
        self.autolevel = True     # on by default: these sensors need it
        self.edges = False
        self.invert = False
        self.palette_ix = 0
        self.trail_ix = 0
        self.dither_ix = 0
        self.gamma = 1.0
        self._prev: np.ndarray | None = None
        self._last: np.ndarray | None = None

    # -- effects -----------------------------------------------------------

    def render(self, frame: np.ndarray) -> np.ndarray:
        out = io.autolevel(frame) if self.autolevel else frame
        out = io.gamma(out, self.gamma)

        palette = PALETTE_CYCLE[self.palette_ix]
        levels = DITHER_CYCLE[self.dither_ix]

        # Anything that reduces to a single plane has to happen before the
        # palette, which is what turns a plane back into colour.
        if self.edges:
            plane = io.normalize(io.sobel(io.box_blur(io.luma(out), 1)))
            out = io.apply_palette(plane, palette or "mono")
        elif palette:
            out = io.apply_palette(io.luma(out), palette)

        if levels:
            plane = io.ordered_dither(io.luma(out), levels)
            # Dithering a colour image looks muddy, so re-tint the dithered
            # luma through the active palette instead of dropping the colour.
            out = io.apply_palette(plane, palette or "mono") if palette else io.gray_to_rgb(plane)

        if self.invert:
            out = 255 - out

        decay = TRAIL_CYCLE[self.trail_ix]
        if decay and self._prev is not None and self._prev.shape == out.shape:
            # Lighten-blend against a fading copy of the past: bright things
            # smear, dark backgrounds stay clean.
            out = np.maximum(out, io.to_u8(self._prev.astype(np.float32) * decay))
        self._prev = out

        self._last = out
        return out

    # -- keys --------------------------------------------------------------

    def on_key(self, key: str) -> str | None:
        if key == "l":
            self.autolevel = not self.autolevel
            return f"auto-levels {'on' if self.autolevel else 'off'}"
        if key == "e":
            self.edges = not self.edges
            return f"edges {'on' if self.edges else 'off'}"
        if key == "i":
            self.invert = not self.invert
            return f"invert {'on' if self.invert else 'off'}"
        if key == "p":
            self.palette_ix = (self.palette_ix + 1) % len(PALETTE_CYCLE)
            return f"palette {PALETTE_CYCLE[self.palette_ix] or 'off'}"
        if key == "d":
            self.dither_ix = (self.dither_ix + 1) % len(DITHER_CYCLE)
            lv = DITHER_CYCLE[self.dither_ix]
            return f"dither {lv if lv else 'off'}"
        if key == "t":
            self.trail_ix = (self.trail_ix + 1) % len(TRAIL_CYCLE)
            self._prev = None
            return f"trails {TRAIL_CYCLE[self.trail_ix] or 'off'}"
        if key in "[]":
            self.gamma = round(min(max(self.gamma + (0.1 if key == "]" else -0.1), 0.3), 3.0), 2)
            return f"gamma {self.gamma}"
        if key == "s" and self._last is not None:
            return f"saved {save_png(self._last, self.outdir, 'live').name}"
        return None

    def status(self) -> str:
        on = [name for name, flag in
              (("lvl", self.autolevel), ("edge", self.edges), ("inv", self.invert)) if flag]
        if p := PALETTE_CYCLE[self.palette_ix]:
            on.append(p)
        if lv := DITHER_CYCLE[self.dither_ix]:
            on.append(f"dither{lv}")
        if d := TRAIL_CYCLE[self.trail_ix]:
            on.append(f"trail{d:g}")
        if abs(self.gamma - 1.0) > 0.05:
            on.append(f"g{self.gamma:g}")
        return "+".join(on) if on else "raw"
