"""Faces: find them, align them, then read identity and expression off them.

YuNet is the detector — 227KB, five landmarks per face, three strides sharing
the same anchor-free decode as YOLOX but with `sqrt(cls * obj)` for the score.

Everything downstream needs an *aligned* crop, not just a cropped box. SFace
and the expression model were both trained on faces warped onto the ArcFace
five-point template, so a raw crop of a tilted head reads as a different
person and a different mood. The similarity transform that does the warping
is the part of this file worth reading twice.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

from . import preprocess as pre
from .labels import EXPRESSIONS, FERPLUS
from .session import Model

# The canonical five points (eyes, nose, mouth corners) for a 112x112 crop.
ARCFACE_TEMPLATE = np.array([
    [38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
    [41.5493, 92.3655], [70.7299, 92.2041],
], dtype=np.float32)


@dataclass(frozen=True)
class Face:
    box: tuple[float, float, float, float]
    score: float
    landmarks: np.ndarray                       # (5, 2) in frame pixels


def similarity_transform(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Umeyama: the rotation+scale+translation best mapping src onto dst.

    A full affine fit would also shear and stretch, which would happily
    "correct" a face into the template and destroy the very geometry the
    recognition model reads. Constraining it to a similarity keeps the face
    rigid and only removes pose.
    """
    src_mean, dst_mean = src.mean(axis=0), dst.mean(axis=0)
    src_c, dst_c = src - src_mean, dst - dst_mean

    covariance = dst_c.T @ src_c / len(src)
    u, s, vt = np.linalg.svd(covariance)

    correction = np.eye(2, dtype=np.float32)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        correction[1, 1] = -1                   # keep it a rotation, not a flip
    rotation = u @ correction @ vt

    variance = src_c.var(axis=0).sum()
    scale = 1.0 if variance < 1e-8 else (s * np.diag(correction)).sum() / variance

    matrix = np.eye(3, dtype=np.float32)
    matrix[:2, :2] = rotation * scale
    matrix[:2, 2] = dst_mean - (rotation * scale) @ src_mean
    return matrix


def align(rgb: np.ndarray, landmarks: np.ndarray, size: int = 112) -> np.ndarray:
    """Warp a face onto the ArcFace template."""
    scale = size / 112.0
    matrix = similarity_transform(landmarks.astype(np.float32), ARCFACE_TEMPLATE * scale)
    # PIL's AFFINE maps output -> input, so it wants the inverse.
    inverse = np.linalg.inv(matrix)
    coeffs = inverse[:2].reshape(-1)
    warped = Image.fromarray(rgb).transform(
        (size, size), Image.AFFINE, tuple(coeffs), resample=Image.BILINEAR)
    return np.asarray(warped)


class YuNet:
    STRIDES = (8, 16, 32)

    def __init__(self, key: str = "face/yunet") -> None:
        self.model = Model(key)
        self.height, self.width = self.model.input_hw()
        self._cells = {}
        for stride in self.STRIDES:
            gh, gw = self.height // stride, self.width // stride
            ys, xs = np.meshgrid(np.arange(gh), np.arange(gw), indexing="ij")
            self._cells[stride] = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(np.float32)

    def __call__(self, rgb: np.ndarray, score_threshold: float = 0.6,
                 iou_threshold: float = 0.3) -> list[Face]:
        padded, box_map = pre.letterbox(rgb, self.width, self.height)
        tensor = pre.to_tensor(padded)
        out = self.model.run_named(tensor)

        boxes, scores, points = [], [], []
        for stride in self.STRIDES:
            cells = self._cells[stride]
            cls = out[f"cls_{stride}"][0].reshape(-1)
            obj = out[f"obj_{stride}"][0].reshape(-1)
            bbox = out[f"bbox_{stride}"][0]
            kps = out[f"kps_{stride}"][0]

            # Geometric mean of the two heads, as the reference decoder does.
            score = np.sqrt(np.clip(cls, 0, 1) * np.clip(obj, 0, 1))
            centre = (cells + bbox[:, :2]) * stride
            size = np.exp(np.clip(bbox[:, 2:4], -10, 10)) * stride
            boxes.append(np.concatenate([centre - size / 2, centre + size / 2], axis=1))
            scores.append(score)
            points.append((np.repeat(cells, 5, axis=0).reshape(-1, 5, 2)
                           + kps.reshape(-1, 5, 2)) * stride)

        boxes = np.concatenate(boxes)
        scores = np.concatenate(scores)
        points = np.concatenate(points)

        keep = scores >= score_threshold
        if not keep.any():
            return []
        boxes, scores, points = boxes[keep], scores[keep], points[keep]

        picked = pre.nms(boxes, scores, iou_threshold)
        boxes = box_map.clip_boxes(box_map.to_source(boxes.reshape(-1, 2, 2)).reshape(-1, 4))
        points = box_map.to_source(points)
        return [Face(tuple(boxes[i]), float(scores[i]), points[i]) for i in picked]


class Expression:
    """Seven expressions from an aligned 112x112 crop."""

    LABELS = EXPRESSIONS

    def __init__(self, key: str = "face/expression") -> None:
        self.model = Model(key)

    def __call__(self, rgb: np.ndarray, face: Face) -> tuple[str, float]:
        crop = align(rgb, face.landmarks, 112)
        tensor = pre.to_tensor(crop, scale=1 / 255.0, mean=0.5, std=0.5)
        probs = pre.softmax(self.model.run(tensor)[0].reshape(-1))
        best = int(probs.argmax())
        return self.LABELS[best], float(probs[best])


class FerPlus:
    """Eight emotions from a 64x64 grayscale crop — no alignment, just a box."""

    LABELS = FERPLUS

    def __init__(self, key: str = "emotion/ferplus") -> None:
        self.model = Model(key)

    def __call__(self, rgb: np.ndarray, face: Face) -> tuple[str, float]:
        x1, y1, x2, y2 = (int(round(v)) for v in face.box)
        crop = rgb[max(y1, 0):max(y2, 1), max(x1, 0):max(x2, 1)]
        if crop.size == 0:
            return "unknown", 0.0
        tensor = pre.to_gray_tensor(crop, 64, 64)
        probs = pre.softmax(self.model.run(tensor)[0].reshape(-1))
        best = int(probs.argmax())
        return self.LABELS[best], float(probs[best])


class SFace:
    """128-d identity embeddings. Compare with cosine similarity."""

    def __init__(self, key: str = "face/sface") -> None:
        self.model = Model(key)

    def embed(self, rgb: np.ndarray, face: Face) -> np.ndarray:
        crop = align(rgb, face.landmarks, 112)
        vector = self.model.run(pre.to_tensor(crop))[0].reshape(-1)
        norm = np.linalg.norm(vector)
        return vector / norm if norm > 0 else vector

    @staticmethod
    def similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Cosine similarity of two normalised embeddings. ~0.36 is SFace's
        published same-person threshold."""
        return float(np.dot(a, b))
