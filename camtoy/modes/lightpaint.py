"""Long-exposure light painting.

A camera is a bucket for photons; a video camera just empties the bucket 20
times a second. Keep the frames instead of discarding them and you get the
long exposure back — wave a phone torch around a dark room and your hand
writes on the picture.

This is the one mode that runs at the sensor's full resolution rather than
the display's. The output is a keepsake PNG, and downsampling it to fit a
terminal first would be a waste of a good exposure.
"""

from __future__ import annotations

import numpy as np

from .. import imageops as io
from .base import DEFAULT_OUTDIR, Mode, save_png

BLENDS = ("lighten", "add", "decay")


class LightPaintMode(Mode):
    name = "paint"
    keys = (
        ("c", "clear"), ("m", "blend"), ("k", "set background"),
        ("[ ]", "gain/decay"), ("space", "pause"), ("b", "preview"), ("s", "save png"),
    )

    def __init__(self, full_size: tuple[int, int], blend: str = "lighten",
                 outdir=DEFAULT_OUTDIR, autosave: bool = True) -> None:
        self.full_size = full_size
        self.blend = blend if blend in BLENDS else "lighten"
        self.outdir = outdir
        self.autosave = autosave

        self.gain = 0.35          # 'add' exposure per frame
        self.decay = 0.97         # 'decay' persistence per frame
        self.paused = False
        self.preview = True

        self._canvas: np.ndarray | None = None
        self._ref: np.ndarray | None = None
        self._live: np.ndarray | None = None
        self._dirty = False

    def work_size(self, display_size: tuple[int, int]) -> tuple[int, int]:
        return self.full_size

    # -- accumulation ------------------------------------------------------

    def render(self, frame: np.ndarray) -> np.ndarray:
        self._live = frame
        light = frame.astype(np.float32)
        if self._ref is not None:
            # Only *new* light counts, so an ambient room does not slowly
            # fog the exposure.
            light = np.clip(light - self._ref, 0, 255)

        if self._canvas is None or self._canvas.shape != light.shape:
            self._canvas = np.zeros_like(light)
            self._dirty = False

        if not self.paused:
            if self.blend == "lighten":
                np.maximum(self._canvas, light, out=self._canvas)
            elif self.blend == "add":
                self._canvas += light * self.gain
            else:
                np.maximum(self._canvas * self.decay, light, out=self._canvas)
            self._dirty = True

        out = io.to_u8(self._canvas)
        if self.preview:
            # Ghost the live frame under the painting so you can still aim.
            out = np.maximum(out, io.to_u8(frame.astype(np.float32) * 0.18))
        return out

    @property
    def painting(self) -> np.ndarray:
        return io.to_u8(self._canvas if self._canvas is not None else np.zeros((1, 1, 3)))

    # -- keys --------------------------------------------------------------

    def on_key(self, key: str) -> str | None:
        if key == "c":
            self._canvas = None
            return "cleared"
        if key == "m":
            self.blend = BLENDS[(BLENDS.index(self.blend) + 1) % len(BLENDS)]
            return f"blend {self.blend}"
        if key == "k":
            if self._ref is not None:
                self._ref = None
                return "background cleared"
            self._ref = self._last_ref()
            # Whatever ambient light already fogged the exposure was captured
            # before there was a reference to subtract it against, so setting
            # one has to start the exposure over or it achieves nothing.
            self._canvas = None
            return "background set"
        if key == "space":
            self.paused = not self.paused
            return "paused" if self.paused else "exposing"
        if key == "b":
            self.preview = not self.preview
            return f"preview {'on' if self.preview else 'off'}"
        if key in "[]":
            if self.blend == "add":
                self.gain = round(min(max(self.gain + (0.05 if key == "]" else -0.05), 0.02), 2.0), 2)
                return f"gain {self.gain}"
            self.decay = round(min(max(self.decay + (0.005 if key == "]" else -0.005), 0.80), 0.999), 3)
            return f"decay {self.decay}"
        if key == "s":
            path = save_png(self.painting, self.outdir, "paint")
            self._dirty = False
            return f"saved {path.name}"
        return None

    def _last_ref(self) -> np.ndarray | None:
        """Freeze the current live view as the ambient reference."""
        return self._live.astype(np.float32) if self._live is not None else None

    def status(self) -> str:
        bits = [self.blend]
        if self.blend == "add":
            bits.append(f"gain{self.gain:g}")
        elif self.blend == "decay":
            bits.append(f"decay{self.decay:g}")
        if self._ref is not None:
            bits.append("bg")
        if self.paused:
            bits.append("PAUSED")
        return " ".join(bits)

    def close(self) -> None:
        if self.autosave and self._dirty and self._canvas is not None and self._canvas.max() > 8:
            save_png(self.painting, self.outdir, "paint")
