"""Turning camera frames into model input, and coordinates back again.

Two things make this fiddly enough to centralise. Models disagree about
layout (NCHW for most, NHWC for everything MediaPipe-derived), channel order
(the OpenCV zoo trained on BGR, the ONNX zoo on RGB) and normalisation. And
detectors need their boxes mapped back to the original frame, which means the
letterbox padding has to be remembered rather than recomputed.

No OpenCV here either — Pillow resamples, numpy does the rest.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .. import imageops as io

# ImageNet statistics, in 0..1 units.
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


@dataclass(frozen=True)
class Letterbox:
    """How a frame was fitted into a square input, so boxes can come back."""

    scale: float
    pad_x: int
    pad_y: int
    src_w: int
    src_h: int

    def to_source(self, xy: np.ndarray) -> np.ndarray:
        """Map model-space coordinates (..., 2) back onto the original frame."""
        out = xy.astype(np.float32).copy()
        out[..., 0] = (out[..., 0] - self.pad_x) / self.scale
        out[..., 1] = (out[..., 1] - self.pad_y) / self.scale
        return out

    def clip_boxes(self, boxes: np.ndarray) -> np.ndarray:
        """Clamp x1y1x2y2 boxes to the frame."""
        out = boxes.copy()
        out[:, 0::2] = np.clip(out[:, 0::2], 0, self.src_w - 1)
        out[:, 1::2] = np.clip(out[:, 1::2], 0, self.src_h - 1)
        return out


def letterbox(rgb: np.ndarray, width: int, height: int,
              fill: int = 114) -> tuple[np.ndarray, Letterbox]:
    """Resize preserving aspect ratio and pad to exactly (height, width).

    Squashing instead would be simpler and would cost accuracy on anything
    that is not 1:1 — a 4:3 webcam feeding a 640x640 detector included.
    """
    src_h, src_w = rgb.shape[:2]
    scale = min(width / src_w, height / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    resized = io.resize(rgb, new_w, new_h)

    canvas = np.full((height, width, 3), fill, np.uint8)
    pad_x, pad_y = (width - new_w) // 2, (height - new_h) // 2
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
    return canvas, Letterbox(scale, pad_x, pad_y, src_w, src_h)


def stretch(rgb: np.ndarray, width: int, height: int) -> np.ndarray:
    """Plain resize, for classifiers and image-to-image models that expect it."""
    return io.resize(rgb, width, height)


def to_tensor(img: np.ndarray, *, bgr: bool = False, scale: float = 1.0,
              mean: np.ndarray | float | None = None,
              std: np.ndarray | float | None = None,
              nhwc: bool = False) -> np.ndarray:
    """(H, W, 3) uint8 -> float32 batch in the layout the model wants.

    `scale` is applied first (1/255 for models trained on 0..1 inputs, 1.0 for
    the ones that want raw 0..255), then mean/std in those same units.
    """
    x = img.astype(np.float32)
    if bgr:
        x = x[:, :, ::-1]
    if scale != 1.0:
        x = x * scale
    if mean is not None:
        x = x - mean
    if std is not None:
        x = x / std
    x = x[None]                                  # NHWC batch
    return np.ascontiguousarray(x if nhwc else x.transpose(0, 3, 1, 2))


def to_gray_tensor(img: np.ndarray, width: int, height: int,
                   scale: float = 1.0) -> np.ndarray:
    """Single-channel NCHW, for the grayscale models (FER+, CRNN)."""
    gray = io.luma(io.resize(img, width, height)).astype(np.float32) * scale
    return np.ascontiguousarray(gray[None, None])


def softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    shifted = x - x.max(axis=axis, keepdims=True)
    e = np.exp(shifted)
    return e / e.sum(axis=axis, keepdims=True)


def sigmoid(x: np.ndarray) -> np.ndarray:
    # Split by sign so neither exp() overflows on the tail it dominates.
    out = np.empty_like(x, dtype=np.float32)
    pos = x >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-x[pos]))
    ex = np.exp(x[~pos])
    out[~pos] = ex / (1.0 + ex)
    return out


def nms(boxes: np.ndarray, scores: np.ndarray, threshold: float = 0.45,
        limit: int = 100) -> np.ndarray:
    """Greedy non-maximum suppression. Returns indices, best score first."""
    if len(boxes) == 0:
        return np.empty(0, dtype=np.int64)
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)
    order = scores.argsort()[::-1]

    keep = []
    while order.size and len(keep) < limit:
        best = order[0]
        keep.append(best)
        if order.size == 1:
            break
        rest = order[1:]
        ix1, iy1 = np.maximum(x1[best], x1[rest]), np.maximum(y1[best], y1[rest])
        ix2, iy2 = np.minimum(x2[best], x2[rest]), np.minimum(y2[best], y2[rest])
        inter = np.maximum(ix2 - ix1, 0) * np.maximum(iy2 - iy1, 0)
        union = areas[best] + areas[rest] - inter
        iou = np.where(union > 0, inter / np.maximum(union, 1e-9), 0)
        order = rest[iou <= threshold]
    return np.array(keep, dtype=np.int64)
