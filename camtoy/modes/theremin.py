"""Motion becomes music.

The frame is sliced into vertical bands, one per voice. How much of a band is
moving sets that voice's volume; how high up the movement is sets its timbre.
Bands run left-to-right up a pentatonic scale, which is the cheap trick that
makes the whole thing sound intentional — on a pentatonic scale there is no
combination of notes that sounds wrong, so flailing at the camera produces
music rather than noise.

Motion is measured as the *fraction of pixels in the band that changed by
more than a threshold*, not the mean difference. On a noisy sensor the mean
never settles, but a thresholded count sits at a solid zero when the room is
still, which is what makes the instrument playable.
"""

from __future__ import annotations

import numpy as np

from .. import imageops as io
from ..synth import SCALES, Synth
from .base import Mode

SCALE_CYCLE = tuple(SCALES)


class ThereminMode(Mode):
    name = "theremin"
    keys = (
        ("[ ]", "sensitivity"), ("- +", "volume"), ("k", "scale"),
        ("o/O", "octave"), ("m", "mute"),
    )

    def __init__(self, voices: int = 8, threshold: int = 14) -> None:
        self.voices = voices
        self.threshold = threshold
        self.muted = False
        self.scale_ix = 0
        self.root = 45                       # A2

        self.synth = Synth(voices=voices)
        self.audio_ok = self.synth.start()

        self._prev: np.ndarray | None = None
        self._energy = np.zeros(voices)
        self._height = np.zeros(voices)

    # -- analysis ----------------------------------------------------------

    def render(self, frame: np.ndarray) -> np.ndarray:
        gray = io.luma(frame)
        h, w = gray.shape

        if self._prev is None or self._prev.shape != gray.shape:
            self._prev = gray
            return io.to_u8(frame * 0.35)

        moving = np.abs(gray - self._prev) > self.threshold
        self._prev = gray

        # Trim to a whole number of bands so the reshape is exact.
        bands = self.voices
        usable = (w // bands) * bands
        cells = moving[:, :usable].reshape(h, bands, usable // bands)

        energy = cells.mean(axis=(0, 2))                     # 0..1 per band
        rows = np.arange(h, dtype=np.float32)[:, None]
        weight = cells.sum(axis=2).astype(np.float32)        # (h, bands)
        total = weight.sum(axis=0)
        # Vertical centre of mass of the motion, 1.0 at the top of frame.
        height = np.where(total > 0, 1.0 - (weight * rows).sum(axis=0) / np.maximum(total, 1) / h, 0.0)

        # Smooth, asymmetrically: rise fast so a gesture speaks immediately,
        # fall slowly so the note rings out instead of chattering.
        gain = np.clip(energy * 6.0, 0, 1) ** 0.7
        rise = gain > self._energy
        self._energy = np.where(rise, gain, self._energy * 0.82 + gain * 0.18)
        self._height = self._height * 0.7 + height * 0.3

        if self.audio_ok:
            self.synth.targets = np.zeros(bands) if self.muted else self._energy * 0.5
            self.synth.brightness = self._height

        return self._draw(frame, moving, usable)

    # -- visuals -----------------------------------------------------------

    def _draw(self, frame: np.ndarray, moving: np.ndarray, usable: int) -> np.ndarray:
        out = io.to_u8(frame.astype(np.float32) * 0.30)
        h, w = out.shape[:2]

        # Moving pixels glow in the palette colour of their band.
        band_w = usable // self.voices
        hue = io.PALETTES["thermal"][np.linspace(70, 255, self.voices).astype(np.uint8)]
        for i in range(self.voices):
            x0, x1 = i * band_w, (i + 1) * band_w
            mask = moving[:, x0:x1]
            if mask.any():
                region = out[:, x0:x1]
                region[mask] = np.maximum(region[mask], hue[i])

            # Level meter along the bottom edge.
            level = int(self._energy[i] * h * 0.4)
            if level > 0:
                out[h - level:h, x0 + 1:x1 - 1] = hue[i]

        return out

    # -- keys --------------------------------------------------------------

    def on_key(self, key: str) -> str | None:
        if key in "[]":
            self.threshold = int(min(max(self.threshold + (2 if key == "[" else -2), 2), 80))
            return f"sensitivity {81 - self.threshold}"
        if key in "-+=":
            delta = -0.05 if key == "-" else 0.05
            self.synth.master = round(min(max(self.synth.master + delta, 0.0), 1.0), 2)
            return f"volume {self.synth.master:g}"
        if key == "k":
            self.scale_ix = (self.scale_ix + 1) % len(SCALE_CYCLE)
            self.synth.set_scale(SCALE_CYCLE[self.scale_ix], self.root)
            return f"scale {SCALE_CYCLE[self.scale_ix]}"
        if key in "oO":
            self.root = int(min(max(self.root + (12 if key == "O" else -12), 21), 81))
            self.synth.set_scale(SCALE_CYCLE[self.scale_ix], self.root)
            return f"root midi {self.root}"
        if key == "m":
            self.muted = not self.muted
            return "muted" if self.muted else "unmuted"
        return None

    def status(self) -> str:
        if not self.audio_ok:
            return f"NO AUDIO ({self.synth.error[:40]})"
        bits = [SCALE_CYCLE[self.scale_ix], f"sens{81 - self.threshold}", f"vol{self.synth.master:g}"]
        if self.muted:
            bits.append("MUTED")
        return " ".join(bits)

    def close(self) -> None:
        self.synth.stop()
