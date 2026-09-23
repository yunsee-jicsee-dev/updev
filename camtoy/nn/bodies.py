"""Bodies and hands: a detector finds a region, a second model reads it.

These are the MediaPipe-lineage models, and they are two-stage by design. The
detector emits an SSD-style prediction per anchor; two of its keypoints then
define a *rotated* region of interest, and the landmark model only ever sees
that upright crop. Skipping the rotation and feeding an axis-aligned crop
does not fail — it just returns landmarks that drift badly the moment the
subject tilts, because the model has never seen a sideways hand.

The anchor layouts are not documented in the ONNX files, but they are
recoverable: the prediction count pins them down exactly. The palm detector
emits 2016 = 24²x2 + 12²x6 at strides 8 and 16 over a 192 input, and the
person detector emits 2254 = 28²x2 + 14²x2 + 7²x6 at strides 8, 16 and 32
over a 224 input. No other sane configuration lands on those totals.

Two things about these that cost real time to work out. They want input in
-1..1, not 0..1 — feeding the wrong range drops the top score on a clear
full-frame subject from 0.69 to 0.44 and leaves nothing above threshold. And
the box they regress is deliberately *small*: BlazePose's detector localises
the hips, not the body, so a tight box near the waist is the correct output
and the full-body ROI comes from the keypoints instead.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

from . import preprocess as pre
from .session import Model

# (stride, anchors per cell) per feature level.
PALM_LAYOUT = ((8, 2), (16, 6))
PERSON_LAYOUT = ((8, 2), (16, 2), (32, 6))

# MediaPipe's 33-point body topology, as bone pairs.
POSE_EDGES = (
    (0, 2), (2, 7), (0, 5), (5, 8),                     # face
    (9, 10),                                            # mouth
    (11, 12), (11, 23), (12, 24), (23, 24),             # torso
    (11, 13), (13, 15), (15, 17), (15, 19), (15, 21),   # left arm
    (12, 14), (14, 16), (16, 18), (16, 20), (16, 22),   # right arm
    (23, 25), (25, 27), (27, 29), (27, 31),             # left leg
    (24, 26), (26, 28), (28, 30), (28, 32),             # right leg
)

# 21-point hand topology: wrist to each fingertip.
HAND_EDGES = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (0, 9), (9, 10), (10, 11), (11, 12),
    (0, 13), (13, 14), (14, 15), (15, 16),
    (0, 17), (17, 18), (18, 19), (19, 20),
    (5, 9), (9, 13), (13, 17),
)


def anchors(layout, side: int) -> np.ndarray:
    """Normalised (x, y) anchor centres, in the model's own output order."""
    out = []
    for stride, count in layout:
        cells = side // stride
        ys, xs = np.meshgrid(np.arange(cells), np.arange(cells), indexing="ij")
        centres = np.stack([(xs.ravel() + 0.5) / cells,
                            (ys.ravel() + 0.5) / cells], axis=1)
        out.append(np.repeat(centres, count, axis=0))
    return np.concatenate(out).astype(np.float32)


@dataclass(frozen=True)
class Region:
    """A rotated crop request, in frame pixels."""

    cx: float
    cy: float
    size: float
    angle: float                                        # radians, clockwise

    def crop(self, rgb: np.ndarray, out: int) -> tuple[np.ndarray, np.ndarray]:
        """Return the upright crop and the 2x3 matrix mapping crop -> frame."""
        scale = self.size / out
        cos, sin = np.cos(self.angle) * scale, np.sin(self.angle) * scale
        # PIL AFFINE maps output pixels back into the source image.
        matrix = np.array([
            [cos, -sin, self.cx - (cos * out - sin * out) / 2],
            [sin, cos, self.cy - (sin * out + cos * out) / 2],
        ], dtype=np.float32)
        crop = Image.fromarray(rgb).transform(
            (out, out), Image.AFFINE, tuple(matrix.reshape(-1)), resample=Image.BILINEAR)
        return np.asarray(crop), matrix

    @staticmethod
    def project(matrix: np.ndarray, xy: np.ndarray) -> np.ndarray:
        """Map crop-space points back onto the frame."""
        pts = np.asarray(xy, np.float32)
        return pts @ matrix[:, :2].T + matrix[:, 2]


class _SsdDetector:
    """Shared decode for the two MediaPipe detectors."""

    LAYOUT: tuple = ()
    KEYPOINTS = 0
    NORM: dict = {}

    def __init__(self, key: str) -> None:
        self.model = Model(key)
        self.side = self.model.input_hw()[0]
        self._anchors = anchors(self.LAYOUT, self.side)

    def _decode(self, rgb: np.ndarray, threshold: float):
        padded, box_map = pre.letterbox(rgb, self.side, self.side)
        tensor = pre.to_tensor(padded, nhwc=self.model.is_nhwc, **self.NORM)
        outputs = self.model.run(tensor)

        raw = next(o for o in outputs if o.shape[-1] >= 4 + 2 * self.KEYPOINTS)[0]
        logits = next(o for o in outputs if o.shape[-1] == 1)[0].reshape(-1)
        scores = pre.sigmoid(np.clip(logits, -50, 50))

        keep = scores >= threshold
        if not keep.any():
            return [], box_map
        raw, scores, anchor = raw[keep], scores[keep], self._anchors[keep]

        # MediaPipe emits offsets in input pixels against a normalised anchor.
        centre = raw[:, :2] / self.side + anchor
        size = raw[:, 2:4] / self.side
        boxes = np.concatenate([centre - size / 2, centre + size / 2], axis=1) * self.side
        points = (raw[:, 4:4 + 2 * self.KEYPOINTS].reshape(-1, self.KEYPOINTS, 2) / self.side
                  + anchor[:, None, :]) * self.side

        picked = pre.nms(boxes, scores, 0.3)
        return [(boxes[i], scores[i], points[i]) for i in picked], box_map


class PalmDetector(_SsdDetector):
    LAYOUT = PALM_LAYOUT
    KEYPOINTS = 7
    NORM = {"scale": 1 / 255.0}                 # unlike the person detector

    def __call__(self, rgb: np.ndarray, threshold: float = 0.6) -> list[Region]:
        found, box_map = self._decode(rgb, threshold)
        regions = []
        for _box, _score, points in found:
            pts = box_map.to_source(points)
            # Centre on the middle-finger knuckle, not the wrist: measured
            # across a shift/scale sweep, that moves the landmark model's own
            # confidence from 0.26 to 0.99 on the same detection.
            regions.append(_region_from(pts[0], pts[2], scale=2.6,
                                        rotate=-np.pi / 2, shift=1.0))
        return regions


class PersonDetector(_SsdDetector):
    LAYOUT = PERSON_LAYOUT
    KEYPOINTS = 4
    NORM = {"scale": 1 / 127.5, "mean": 1.0}

    def __call__(self, rgb: np.ndarray, threshold: float = 0.5) -> list[Region]:
        found, box_map = self._decode(rgb, threshold)
        regions = []
        for _box, _score, points in found:
            pts = box_map.to_source(points)
            hips, scale_point = pts[0], pts[1]
            regions.append(_region_from(hips, scale_point, scale=2.4, rotate=-np.pi / 2))
        return regions


def _region_from(origin: np.ndarray, target: np.ndarray, scale: float,
                 rotate: float, shift: float = 0.0) -> Region:
    """Turn the detector's two anchor keypoints into a rotated square ROI.

    `shift` slides the centre along the origin->target axis in units of their
    separation: 0 keeps it on `origin`, 1 puts it on `target`.
    """
    delta = target - origin
    distance = float(np.hypot(*delta)) or 1.0
    angle = float(np.arctan2(delta[1], delta[0])) - rotate
    centre = origin + delta * shift
    return Region(float(centre[0]), float(centre[1]), distance * scale, angle)


class HandLandmarks:
    """21 points from a palm ROI, plus a handedness/confidence score."""

    NORM = {"scale": 1 / 255.0}

    def __init__(self, key: str = "hand/landmark") -> None:
        self.model = Model(key)
        self.side = self.model.input_hw()[0]

    def __call__(self, rgb: np.ndarray, region: Region):
        crop, matrix = region.crop(rgb, self.side)
        tensor = pre.to_tensor(crop, nhwc=self.model.is_nhwc, **self.NORM)
        outputs = self.model.run(tensor)
        coords = next(o for o in outputs if o.size == 63).reshape(21, 3)
        score = float(np.ravel(next(o for o in outputs if o.size == 1))[0])
        return Region.project(matrix, coords[:, :2]), score


class PoseLandmarks:
    """33 body points from a person ROI."""

    # 0..1, unlike the person *detector* that feeds it. Measured, not assumed:
    # this pairing scores 0.995 with all 33 joints visible on the test frame.
    NORM = {"scale": 1 / 255.0}

    def __init__(self, key: str = "pose/body") -> None:
        self.model = Model(key)
        self.side = self.model.input_hw()[0]

    def __call__(self, rgb: np.ndarray, region: Region):
        crop, matrix = region.crop(rgb, self.side)
        tensor = pre.to_tensor(crop, nhwc=self.model.is_nhwc, **self.NORM)
        outputs = self.model.run(tensor)
        # 195 = 39 points x 5 (x, y, z, visibility, presence); the first 33
        # are the body, the rest are auxiliary.
        flat = next(o for o in outputs if o.size == 195).reshape(39, 5)
        points = flat[:33, :2]
        visible = pre.sigmoid(flat[:33, 3]) > 0.5
        score = float(np.ravel(next(o for o in outputs if o.size == 1))[0])
        return Region.project(matrix, points), visible, score


class HandPipeline:
    """Palm detection then landmarks, the way MediaPipe intends."""

    def __init__(self) -> None:
        self.detector = PalmDetector("hand/palm")
        self.landmarks = HandLandmarks()

    def __call__(self, rgb: np.ndarray, limit: int = 2):
        out = []
        for region in self.detector(rgb)[:limit]:
            points, score = self.landmarks(rgb, region)
            out.append((points, score))
        return out


class PosePipeline:
    def __init__(self) -> None:
        self.detector = PersonDetector("pose/person")
        self.landmarks = PoseLandmarks()

    def __call__(self, rgb: np.ndarray, limit: int = 1):
        out = []
        for region in self.detector(rgb)[:limit]:
            points, visible, score = self.landmarks(rgb, region)
            out.append((points, visible, score))
        return out
