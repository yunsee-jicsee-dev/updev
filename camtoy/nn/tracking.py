"""VitTrack: pick a box once, follow it after that.

A transformer tracker compares a fixed 128px *template* of the target against
a 256px *search* window cropped around wherever the target was last seen. The
head answers on a 16x16 grid: a score per cell, a sub-cell offset, and a size.

The part that decides whether this works at all is the search window. Too
tight and a fast subject leaves it and is lost forever; too wide and the
target is a handful of pixels the model cannot recognise. The classic factor
is 4x the target's geometric mean side, and that is what SEARCH_FACTOR is.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from . import preprocess as pre
from .session import Model

TEMPLATE_FACTOR = 2.0
SEARCH_FACTOR = 4.0


def _context_crop(rgb: np.ndarray, box, factor: float, out: int):
    """Square crop around a box, padded at the edges. Returns crop + mapping."""
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    side = float(np.sqrt(max(x2 - x1, 1) * max(y2 - y1, 1)) * factor)

    scale = side / out
    matrix = (scale, 0.0, cx - side / 2, 0.0, scale, cy - side / 2)
    crop = Image.fromarray(rgb).transform(
        (out, out), Image.AFFINE, matrix, resample=Image.BILINEAR)
    return np.asarray(crop), (cx - side / 2, cy - side / 2, scale)


class VitTrack:
    """Single-object tracker. `start()` once, then `update()` per frame."""

    GRID = 16

    def __init__(self, key: str = "track/vittrack") -> None:
        self.model = Model(key)
        self.template_size = self.model.input_hw("template")[0]
        self.search_size = self.model.input_hw("search")[0]
        self._template: np.ndarray | None = None
        self.box: tuple[float, float, float, float] | None = None
        self.score = 0.0

    def start(self, rgb: np.ndarray, box) -> None:
        crop, _ = _context_crop(rgb, box, TEMPLATE_FACTOR, self.template_size)
        self._template = pre.to_tensor(crop, scale=1 / 255.0)
        self.box = tuple(float(v) for v in box)
        self.score = 1.0

    @property
    def active(self) -> bool:
        return self._template is not None and self.box is not None

    def update(self, rgb: np.ndarray) -> tuple[tuple[float, float, float, float], float]:
        if not self.active:
            raise RuntimeError("call start() with a box before update()")

        crop, (origin_x, origin_y, scale) = _context_crop(
            rgb, self.box, SEARCH_FACTOR, self.search_size)
        search = pre.to_tensor(crop, scale=1 / 255.0)
        outputs = self.model.run({"template": self._template, "search": search})

        score_map = next(o for o in outputs if o.shape[1] == 1)[0, 0]
        planes = [o for o in outputs if o.shape[1] == 2]
        # Sizes are strictly positive and offsets are centred near zero, so
        # the mean tells the two 2-channel heads apart without name-guessing.
        sizes, offsets = sorted(planes, key=lambda o: -float(np.mean(o)))

        cell = int(np.argmax(score_map))
        row, col = divmod(cell, self.GRID)
        self.score = float(score_map[row, col])

        dx, dy = offsets[0, 0, row, col], offsets[0, 1, row, col]
        w = float(sizes[0, 0, row, col]) * self.search_size
        h = float(sizes[0, 1, row, col]) * self.search_size
        cx = (col + float(dx)) / self.GRID * self.search_size
        cy = (row + float(dy)) / self.GRID * self.search_size

        # Back into frame pixels.
        fx, fy = origin_x + cx * scale, origin_y + cy * scale
        fw, fh = w * scale, h * scale
        height, width = rgb.shape[:2]
        self.box = (float(np.clip(fx - fw / 2, 0, width - 1)),
                    float(np.clip(fy - fh / 2, 0, height - 1)),
                    float(np.clip(fx + fw / 2, 0, width - 1)),
                    float(np.clip(fy + fh / 2, 0, height - 1)))
        return self.box, self.score

    def stop(self) -> None:
        self._template = None
        self.box = None
        self.score = 0.0
