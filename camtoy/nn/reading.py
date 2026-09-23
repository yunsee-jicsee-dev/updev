"""Text in the scene, and identity across frames.

PP-OCR's detector emits a per-pixel text probability map, so turning it into
boxes needs connected components — normally a one-liner in OpenCV, which is
not installed. The union-find below is that one-liner's replacement: a single
raster pass linking each foreground pixel to its west and north neighbours,
then a pass collecting each root's extent. Two passes over a 480x480 mask is
nothing next to the inference that produced it.

CRNN reads each box. Its 37 outputs are 36 characters plus a CTC blank, and
the greedy decode has to collapse runs *before* dropping blanks — do it the
other way round and "book" becomes "bok".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .. import imageops as io
from . import preprocess as pre
from .labels import CRNN_CHARSET
from .session import Model


@dataclass(frozen=True)
class TextBox:
    box: tuple[int, int, int, int]
    text: str = ""
    score: float = 0.0


def connected_boxes(mask: np.ndarray, min_area: int = 24) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of 4-connected True regions, via union-find."""
    height, width = mask.shape
    parent = np.full(height * width, -1, dtype=np.int32)

    def find(i: int) -> int:
        root = i
        while parent[root] != root:
            root = parent[root]
        while parent[i] != root:                # path compression
            parent[i], i = root, parent[i]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    ys, xs = np.nonzero(mask)
    for y, x in zip(ys.tolist(), xs.tolist()):
        i = y * width + x
        parent[i] = i
        if x and mask[y, x - 1]:
            union(i, i - 1)
        if y and mask[y - 1, x]:
            union(i, i - width)

    extents: dict[int, list[int]] = {}
    for y, x in zip(ys.tolist(), xs.tolist()):
        root = find(y * width + x)
        box = extents.get(root)
        if box is None:
            extents[root] = [x, y, x, y]
        else:
            box[0] = min(box[0], x)
            box[1] = min(box[1], y)
            box[2] = max(box[2], x)
            box[3] = max(box[3], y)

    return [tuple(b) for b in extents.values()
            if (b[2] - b[0] + 1) * (b[3] - b[1] + 1) >= min_area]


class TextDetector:
    """PP-OCRv3 differentiable-binarisation detector."""

    def __init__(self, key: str = "text/detect_en", side: int = 480) -> None:
        self.model = Model(key)
        # The graph takes any size but the network stride is 32.
        self.side = max(32, round(side / 32) * 32)

    def __call__(self, rgb: np.ndarray, threshold: float = 0.3,
                 pad: float = 0.15) -> list[tuple[int, int, int, int]]:
        resized = pre.stretch(rgb, self.side, self.side)
        tensor = pre.to_tensor(resized, scale=1 / 255.0,
                               mean=pre.IMAGENET_MEAN, std=pre.IMAGENET_STD)
        probs = np.asarray(self.model.run(tensor)[0]).reshape(self.side, self.side)

        boxes = connected_boxes(probs > threshold)
        scale_x, scale_y = rgb.shape[1] / self.side, rgb.shape[0] / self.side
        out = []
        for x1, y1, x2, y2 in boxes:
            w, h = (x2 - x1) * scale_x, (y2 - y1) * scale_y
            # DB shrinks each text region during training, so grow it back.
            gx, gy = w * pad, h * pad
            out.append((max(int(x1 * scale_x - gx), 0), max(int(y1 * scale_y - gy), 0),
                        min(int(x2 * scale_x + gx), rgb.shape[1] - 1),
                        min(int(y2 * scale_y + gy), rgb.shape[0] - 1)))
        return out


class TextRecognizer:
    """CRNN over a 32x100 grayscale crop, greedy CTC decode."""

    BLANK = 0

    def __init__(self, key: str = "text/recog_en") -> None:
        self.model = Model(key)

    def __call__(self, rgb: np.ndarray, box) -> tuple[str, float]:
        x1, y1, x2, y2 = box
        crop = rgb[y1:max(y2, y1 + 1), x1:max(x2, x1 + 1)]
        if crop.size == 0:
            return "", 0.0
        gray = io.luma(io.resize(crop, 100, 32)).astype(np.float32)
        tensor = ((gray / 255.0 - 0.5) / 0.5)[None, None]
        logits = np.asarray(self.model.run(tensor)[0])      # (T, 1, 37)
        probs = pre.softmax(logits.reshape(logits.shape[0], -1), axis=1)

        indices = probs.argmax(axis=1)
        confidence = float(probs.max(axis=1).mean())
        chars, previous = [], -1
        for index in indices.tolist():
            # Collapse repeats first, then drop blanks: a genuine double
            # letter is separated by a blank in a correct CTC alignment.
            if index != previous and index != self.BLANK:
                position = index - 1
                if 0 <= position < len(CRNN_CHARSET):
                    chars.append(CRNN_CHARSET[position])
            previous = index
        return "".join(chars), confidence


class TextPipeline:
    def __init__(self, side: int = 480) -> None:
        self.detector = TextDetector(side=side)
        self.recognizer = TextRecognizer()

    def __call__(self, rgb: np.ndarray, limit: int = 8) -> list[TextBox]:
        found = self.detector(rgb)
        # Biggest first: the readable text is rarely the smallest blob.
        found.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
        out = []
        for box in found[:limit]:
            text, score = self.recognizer(rgb, box)
            if text:
                out.append(TextBox(box, text, score))
        return out


class PersonReid:
    """YoutuReID embeddings — tell two people apart across frames."""

    def __init__(self, key: str = "reid/person") -> None:
        self.model = Model(key)
        self.height, self.width = self.model.input_hw()

    def embed(self, rgb: np.ndarray) -> np.ndarray:
        resized = pre.stretch(rgb, self.width, self.height)
        tensor = pre.to_tensor(resized, scale=1 / 255.0,
                               mean=pre.IMAGENET_MEAN, std=pre.IMAGENET_STD)
        vector = np.asarray(self.model.run(tensor)[0]).reshape(-1)
        norm = np.linalg.norm(vector)
        return vector / norm if norm > 0 else vector

    @staticmethod
    def similarity(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b))
