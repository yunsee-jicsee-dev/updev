"""Overlays: boxes, skeletons, labels.

Pillow's ImageDraw does the work. Hand-rolling filled rectangles in numpy is
easy; hand-rolling antialiased text is not, and a detector whose labels are
unreadable has not really told you anything.

Every function takes and returns a uint8 (H, W, 3) RGB array so overlays
compose with the numpy effects in imageops.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


@lru_cache(maxsize=8)
def font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _canvas(rgb: np.ndarray):
    image = Image.fromarray(np.ascontiguousarray(rgb))
    return image, ImageDraw.Draw(image)


def _label_size(size: int, height: int) -> int:
    """Scale text with the frame so a 160px terminal render stays legible."""
    return max(9, min(size, height // 18))


def boxes(rgb: np.ndarray, xyxy: np.ndarray, texts: list[str] | None = None,
          colors: np.ndarray | None = None, width: int = 2) -> np.ndarray:
    """Draw detection boxes with optional labels above each."""
    if len(xyxy) == 0:
        return rgb
    image, draw = _canvas(rgb)
    size = _label_size(15, rgb.shape[0])
    face = font(size)

    for i, box in enumerate(np.asarray(xyxy)):
        colour = tuple(int(c) for c in (colors[i] if colors is not None else (0, 255, 128)))
        x1, y1, x2, y2 = (float(v) for v in box[:4])
        draw.rectangle([x1, y1, x2, y2], outline=colour, width=width)
        if not texts or i >= len(texts) or not texts[i]:
            continue
        text = texts[i]
        tw = draw.textlength(text, font=face)
        th = size + 3
        # Flip the caption inside the box when it would fall off the top edge.
        ty = y1 - th if y1 - th >= 0 else y1
        draw.rectangle([x1, ty, x1 + tw + 6, ty + th], fill=colour)
        draw.text((x1 + 3, ty + 1), text, fill=(0, 0, 0), font=face)

    return np.asarray(image)


def points(rgb: np.ndarray, xy: np.ndarray, colour=(255, 80, 80),
           radius: int = 3) -> np.ndarray:
    if len(xy) == 0:
        return rgb
    image, draw = _canvas(rgb)
    colour = tuple(int(c) for c in colour)
    for x, y in np.asarray(xy)[:, :2]:
        draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                     fill=colour, outline=(0, 0, 0))
    return np.asarray(image)


def skeleton(rgb: np.ndarray, xy: np.ndarray, edges, colour=(80, 220, 255),
             width: int = 2, radius: int = 3,
             visible: np.ndarray | None = None) -> np.ndarray:
    """Joints plus the bones between them, skipping low-confidence points."""
    image, draw = _canvas(rgb)
    pts = np.asarray(xy, dtype=np.float32)
    ok = np.ones(len(pts), bool) if visible is None else np.asarray(visible, bool)
    line_colour = tuple(int(c) for c in colour)

    for a, b in edges:
        if a < len(pts) and b < len(pts) and ok[a] and ok[b]:
            draw.line([tuple(pts[a][:2]), tuple(pts[b][:2])], fill=line_colour, width=width)
    for i, (x, y) in enumerate(pts[:, :2]):
        if ok[i]:
            draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                         fill=(255, 255, 255), outline=line_colour)
    return np.asarray(image)


def caption(rgb: np.ndarray, lines: list[str], corner: str = "tl",
            colour=(255, 255, 255)) -> np.ndarray:
    """A translucent panel of text in one corner — top-1 lists, timings."""
    if not lines:
        return rgb
    image, draw = _canvas(rgb)
    size = _label_size(16, rgb.shape[0])
    face = font(size)
    step = size + 4
    widest = max(draw.textlength(line, font=face) for line in lines)

    height, width = rgb.shape[:2]
    x = 4 if corner.endswith("l") else width - widest - 10
    y = 4 if corner.startswith("t") else height - step * len(lines) - 6

    panel = Image.new("RGBA", (int(widest) + 10, step * len(lines) + 6), (0, 0, 0, 140))
    image.paste(Image.alpha_composite(
        image.crop((int(x), int(y), int(x) + panel.width, int(y) + panel.height)).convert("RGBA"),
        panel).convert("RGB"), (int(x), int(y)))

    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((x + 5, y + 3 + i * step), line, fill=tuple(colour), font=face)
    return np.asarray(image)


def mask_overlay(rgb: np.ndarray, mask: np.ndarray, colour=(0, 255, 160),
                 alpha: float = 0.5) -> np.ndarray:
    """Tint where mask (H, W) in 0..1 is high."""
    tint = np.asarray(colour, np.float32)
    a = np.clip(mask, 0, 1)[..., None] * alpha
    return np.clip(rgb.astype(np.float32) * (1 - a) + tint * a, 0, 255).astype(np.uint8)
