"""Class names, and stable colours to draw them in.

COCO's 80 names are short enough to live in the source — a detector that
cannot say what it found because a text file is missing is useless. ImageNet's
1000 come from a downloaded list.
"""

from __future__ import annotations

import numpy as np

from .registry import MODEL_DIR

COCO80 = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
)

# opencv_zoo's facial_expression_recognition, in its training order.
EXPRESSIONS = ("angry", "disgust", "fearful", "happy", "neutral", "sad", "surprised")

# ONNX zoo emotion-ferplus.
FERPLUS = ("neutral", "happiness", "surprise", "sadness",
           "anger", "disgust", "fear", "contempt")

# CRNN's character set: blank first (CTC), then digits and lowercase letters.
CRNN_CHARSET = "0123456789abcdefghijklmnopqrstuvwxyz"

_imagenet: tuple[str, ...] | None = None


def imagenet() -> tuple[str, ...]:
    """1000 ImageNet class names, loaded once from models/labels/."""
    global _imagenet
    if _imagenet is None:
        path = MODEL_DIR / "labels" / "imagenet.txt"
        if not path.is_file():
            raise FileNotFoundError(
                f"{path} is missing. Fetch it with:  camtoy models pull --labels"
            )
        names = [line.strip() for line in path.read_text().splitlines() if line.strip()]
        _imagenet = tuple(names)
    return _imagenet


def palette(n: int) -> np.ndarray:
    """n visually distinct RGB colours, stable across runs.

    Golden-ratio hue stepping so neighbouring class ids never get neighbouring
    colours — class 41 and 42 sitting side by side must not look alike.
    """
    hues = (np.arange(n) * 0.61803398875) % 1.0
    sat, val = 0.75, 1.0
    i = (hues * 6).astype(int) % 6
    f = hues * 6 - np.floor(hues * 6)
    p, q, t = val * (1 - sat), val * (1 - sat * f), val * (1 - sat * (1 - f))
    v = np.full(n, val)
    r = np.select([i == 0, i == 1, i == 2, i == 3, i == 4], [v, q, p, p, t], t)
    g = np.select([i == 0, i == 1, i == 2, i == 3, i == 4], [t, v, v, q, p], p)
    b = np.select([i == 0, i == 1, i == 2, i == 3, i == 4], [p, p, t, v, v], q)
    return (np.stack([r, g, b], axis=1) * 255).astype(np.uint8)


COCO_COLORS = palette(len(COCO80))
