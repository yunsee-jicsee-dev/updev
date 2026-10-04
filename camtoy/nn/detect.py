"""Object detection: YOLOX and NanoDet, both 80-class COCO.

YOLOX is anchor-free. Its 8400 predictions are the concatenation of three
feature maps — 80x80, 40x40 and 20x20 over a 640 input, at strides 8, 16 and
32 — and the four box numbers are offsets *in cells*, not pixels: centre is
`(raw + cell_index) * stride`, size is `exp(raw) * stride`. Skip the stride
multiply and every box collapses toward the top-left corner, which looks
plausible enough on a busy frame to be missed.

NanoDet predicts distances to the four box edges as a distribution over 8
bins per side (Generalised Focal Loss), so its regression head needs a
softmax and an expectation before it means anything in pixels.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import preprocess as pre
from .labels import COCO80
from .session import Model


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]      # x1, y1, x2, y2 in frame pixels
    score: float
    label: str
    index: int


def _grid(strides, height, width):
    """Cell centres and their stride, laid out to match the model's output."""
    centres, scales = [], []
    for stride in strides:
        gh, gw = height // stride, width // stride
        ys, xs = np.meshgrid(np.arange(gh), np.arange(gw), indexing="ij")
        centres.append(np.stack([xs.ravel(), ys.ravel()], axis=1))
        scales.append(np.full(gh * gw, stride, dtype=np.float32))
    return np.concatenate(centres).astype(np.float32), np.concatenate(scales)


class YoloX:
    STRIDES = (8, 16, 32)

    def __init__(self, key: str = "detect/yolox") -> None:
        self.model = Model(key)
        self.height, self.width = self.model.input_hw()
        self._grid, self._stride = _grid(self.STRIDES, self.height, self.width)

    def __call__(self, rgb: np.ndarray, score_threshold: float = 0.35,
                 iou_threshold: float = 0.45) -> list[Detection]:
        padded, box_map = pre.letterbox(rgb, self.width, self.height)
        # YOLOX-S from the zoo takes raw 0..255 with no mean/std.
        tensor = pre.to_tensor(padded)
        raw = self.model.run(tensor)[0][0]              # (8400, 85)

        centres = (raw[:, :2] + self._grid) * self._stride[:, None]
        sizes = np.exp(np.clip(raw[:, 2:4], -10, 10)) * self._stride[:, None]
        scores = raw[:, 4:5] * raw[:, 5:]              # objectness x class
        best = scores.argmax(axis=1)
        confidence = scores[np.arange(len(scores)), best]

        keep = confidence >= score_threshold
        if not keep.any():
            return []
        centres, sizes = centres[keep], sizes[keep]
        confidence, best = confidence[keep], best[keep]

        boxes = np.concatenate([centres - sizes / 2, centres + sizes / 2], axis=1)
        boxes = box_map.clip_boxes(box_map.to_source(boxes.reshape(-1, 2, 2)).reshape(-1, 4))

        picked = pre.nms(boxes, confidence, iou_threshold)
        return [Detection(tuple(boxes[i]), float(confidence[i]),
                          COCO80[int(best[i])], int(best[i])) for i in picked]


class NanoDet:
    STRIDES = (8, 16, 32)
    BINS = 8                                    # regression bins per box side

    def __init__(self, key: str = "detect/nanodet") -> None:
        self.model = Model(key)
        self.height, self.width = self.model.input_hw()
        self._grid, self._stride = _grid(self.STRIDES, self.height, self.width)
        # Trained with these statistics in raw 0..255 units, not 0..1.
        self._mean = np.array([103.53, 116.28, 123.675], np.float32)
        self._std = np.array([57.375, 57.12, 58.395], np.float32)

    def __call__(self, rgb: np.ndarray, score_threshold: float = 0.35,
                 iou_threshold: float = 0.45) -> list[Detection]:
        padded, box_map = pre.letterbox(rgb, self.width, self.height)
        tensor = pre.to_tensor(padded, bgr=True, mean=self._mean, std=self._std)
        outputs = self.model.run(tensor)

        # Six heads: three classification maps then three regression maps,
        # each triple ordered by stride. Pair them by cell count, which is
        # unambiguous, rather than by the graph's output names.
        cls = sorted((o for o in outputs if o.shape[-1] == 80), key=lambda a: -a.shape[1])
        reg = sorted((o for o in outputs if o.shape[-1] == 4 * self.BINS),
                     key=lambda a: -a.shape[1])
        scores = np.concatenate([c[0] for c in cls], axis=0)
        distances = np.concatenate([r[0] for r in reg], axis=0)

        best = scores.argmax(axis=1)
        confidence = scores[np.arange(len(scores)), best]
        keep = confidence >= score_threshold
        if not keep.any():
            return []

        # Expectation over the softmaxed bins gives a distance in cells.
        bins = pre.softmax(distances[keep].reshape(-1, 4, self.BINS), axis=2)
        edges = (bins * np.arange(self.BINS, dtype=np.float32)).sum(axis=2)
        edges = edges * self._stride[keep][:, None]

        centre = (self._grid[keep] + 0.5) * self._stride[keep][:, None]
        boxes = np.stack([centre[:, 0] - edges[:, 0], centre[:, 1] - edges[:, 1],
                          centre[:, 0] + edges[:, 2], centre[:, 1] + edges[:, 3]], axis=1)
        boxes = box_map.clip_boxes(box_map.to_source(boxes.reshape(-1, 2, 2)).reshape(-1, 4))

        confidence, best = confidence[keep], best[keep]
        picked = pre.nms(boxes, confidence, iou_threshold)
        return [Detection(tuple(boxes[i]), float(confidence[i]),
                          COCO80[int(best[i])], int(best[i])) for i in picked]


DETECTORS = {"yolox": YoloX, "nanodet": NanoDet}
