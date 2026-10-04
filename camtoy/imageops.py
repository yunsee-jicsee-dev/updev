"""Image maths for camtoy: levels, edges, dithering, palettes.

Everything here is numpy on uint8 (H, W, 3) RGB or float32 (H, W) luma.
There is no OpenCV and no scipy on the target Pi, which turns out to be
freeing: a 3x3 Sobel is six array slices, and an ordered dither is one
broadcast compare. The only outside help is Pillow for resampling, because
its C resize beats anything worth hand-rolling here.

Functions never mutate their input.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

# --------------------------------------------------------------------------
# basics
# --------------------------------------------------------------------------

# Rec.601 luma. The cheap sensors this targets have noisy blue channels, and
# 601 leans on green, which is the quietest one.
_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def luma(rgb: np.ndarray) -> np.ndarray:
    """(H, W, 3) uint8 -> (H, W) float32 in 0..255."""
    return rgb.astype(np.float32) @ _LUMA


def to_u8(a: np.ndarray) -> np.ndarray:
    return np.clip(a, 0, 255).astype(np.uint8)


def gray_to_rgb(gray: np.ndarray) -> np.ndarray:
    return np.repeat(to_u8(gray)[:, :, None], 3, axis=2)


def resize(img: np.ndarray, width: int, height: int) -> np.ndarray:
    """Resample to exactly (height, width).

    BOX when shrinking so every source pixel contributes — nearest-neighbour
    downscaling of a noisy sensor just amplifies the noise. BILINEAR when
    growing, which is only used by the window backend.
    """
    h, w = img.shape[:2]
    if (w, h) == (width, height):
        return img
    shrinking = width * height < w * h
    resample = Image.BOX if shrinking else Image.BILINEAR
    return np.asarray(Image.fromarray(to_u8(img)).resize((width, height), resample))


# --------------------------------------------------------------------------
# levels
# --------------------------------------------------------------------------

def autolevel(rgb: np.ndarray, low: float = 2.0, high: float = 98.0) -> np.ndarray:
    """Percentile contrast stretch — the single most useful filter here.

    These UVC modules run auto-exposure with no manual override, so pointing
    one at a bright wall pins the whole histogram into the top third and the
    picture looks blank. Rescaling between the 2nd and 98th percentile of
    *luma* (not per channel, which would wreck the white balance) pulls the
    detail back out.
    """
    y = luma(rgb)
    lo, hi = np.percentile(y[::4, ::4], (low, high))    # subsample: same answer, 16x cheaper
    if hi - lo < 1e-3:
        return rgb
    scaled = (rgb.astype(np.float32) - lo) * (255.0 / (hi - lo))
    return to_u8(scaled)


def gamma(rgb: np.ndarray, g: float) -> np.ndarray:
    if abs(g - 1.0) < 1e-3:
        return rgb
    lut = to_u8((np.linspace(0, 1, 256) ** (1.0 / g)) * 255.0)
    return lut[rgb]


# --------------------------------------------------------------------------
# edges
# --------------------------------------------------------------------------

def sobel(gray: np.ndarray) -> np.ndarray:
    """3x3 Sobel gradient magnitude, edge-padded back to the input shape."""
    a = gray.astype(np.float32)
    gx = ((a[:-2, 2:] + 2 * a[1:-1, 2:] + a[2:, 2:])
          - (a[:-2, :-2] + 2 * a[1:-1, :-2] + a[2:, :-2]))
    gy = ((a[2:, :-2] + 2 * a[2:, 1:-1] + a[2:, 2:])
          - (a[:-2, :-2] + 2 * a[:-2, 1:-1] + a[:-2, 2:]))
    mag = np.hypot(gx, gy)
    return np.pad(mag, 1, mode="edge")


def box_blur(gray: np.ndarray, radius: int = 1) -> np.ndarray:
    """Separable box blur via cumulative sums: O(n) regardless of radius."""
    if radius < 1:
        return gray
    a = gray.astype(np.float32)
    for axis in (0, 1):
        n = a.shape[axis]
        pad = [(0, 0), (0, 0)]
        pad[axis] = (radius + 1, radius)
        c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis)
        lo = np.take(c, np.arange(0, n), axis=axis)
        hi = np.take(c, np.arange(2 * radius + 1, n + 2 * radius + 1), axis=axis)
        a = (hi - lo) / (2 * radius + 1)
    return a


# --------------------------------------------------------------------------
# dithering
# --------------------------------------------------------------------------

def _bayer(n: int) -> np.ndarray:
    """Recursive Bayer threshold matrix, normalised to 0..1."""
    m = np.array([[0]], dtype=np.float32)
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m + 0.5) / m.size


_BAYER8 = _bayer(8)


def ordered_dither(gray: np.ndarray, levels: int = 2) -> np.ndarray:
    """8x8 Bayer dither.

    Deliberately chosen over Floyd-Steinberg for live video: error diffusion
    is sequential, so a one-pixel change at the top left reshuffles the whole
    frame and the result boils violently between frames. An ordered matrix is
    a fixed function of position, so still areas stay still.
    """
    h, w = gray.shape
    thresh = np.tile(_BAYER8, (h // 8 + 1, w // 8 + 1))[:h, :w]
    steps = max(levels - 1, 1)
    q = np.floor(gray / 255.0 * steps + thresh) / steps
    return to_u8(np.clip(q, 0, 1) * 255.0)


def floyd_steinberg(gray: np.ndarray, levels: int = 2) -> np.ndarray:
    """Error-diffusion dither for stills.

    Sequential and therefore slow — fine for a saved frame, never in the live
    path. Use ordered_dither() there.
    """
    a = gray.astype(np.float32).copy()
    h, w = a.shape
    steps = max(levels - 1, 1)
    for y in range(h):
        row, below = a[y], a[y + 1] if y + 1 < h else None
        for x in range(w):
            old = row[x]
            new = round(old / 255.0 * steps) / steps * 255.0
            row[x] = new
            err = old - new
            if x + 1 < w:
                row[x + 1] += err * 7 / 16
            if below is not None:
                if x:
                    below[x - 1] += err * 3 / 16
                below[x] += err * 5 / 16
                if x + 1 < w:
                    below[x + 1] += err * 1 / 16
    return to_u8(a)


# --------------------------------------------------------------------------
# palettes
# --------------------------------------------------------------------------

def _ramp(stops: list[tuple[float, tuple[int, int, int]]]) -> np.ndarray:
    """Build a 256x3 uint8 LUT by interpolating between colour stops."""
    xs = np.linspace(0, 1, 256)
    pos = [p for p, _ in stops]
    lut = np.stack(
        [np.interp(xs, pos, [c[i] for _, c in stops]) for i in range(3)], axis=1
    )
    return to_u8(lut)


PALETTES: dict[str, np.ndarray] = {
    "mono": _ramp([(0.0, (0, 0, 0)), (1.0, (255, 255, 255))]),
    # Classic ironbow: black -> purple -> red -> orange -> yellow -> white.
    "thermal": _ramp([
        (0.00, (0, 0, 8)), (0.25, (78, 8, 120)), (0.50, (200, 40, 40)),
        (0.72, (255, 140, 0)), (0.88, (255, 232, 60)), (1.00, (255, 255, 255)),
    ]),
    "amber": _ramp([(0.0, (8, 3, 0)), (0.55, (170, 88, 0)), (1.0, (255, 205, 120))]),
    "green": _ramp([(0.0, (0, 8, 2)), (0.55, (0, 170, 60)), (1.0, (190, 255, 200))]),
    "ice": _ramp([(0.0, (0, 4, 16)), (0.5, (0, 110, 190)), (1.0, (220, 250, 255))]),
    # Steps hard between six hues — reads as posterised comic-book shading.
    "pop": _ramp([
        (0.00, (24, 0, 48)), (0.20, (24, 0, 48)), (0.21, (190, 20, 90)),
        (0.45, (190, 20, 90)), (0.46, (250, 120, 20)), (0.70, (250, 120, 20)),
        (0.71, (255, 235, 90)), (1.00, (255, 255, 220)),
    ]),
}


def apply_palette(gray: np.ndarray, name: str) -> np.ndarray:
    """Map a 0..255 luma plane through a named palette to RGB."""
    return PALETTES[name][to_u8(gray)]


def normalize(a: np.ndarray) -> np.ndarray:
    """Stretch any float plane to fill 0..255."""
    lo, hi = float(a.min()), float(a.max())
    if hi - lo < 1e-6:
        return np.zeros_like(a, dtype=np.float32)
    return (a - lo) * (255.0 / (hi - lo))
