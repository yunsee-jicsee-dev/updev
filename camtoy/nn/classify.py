"""Whole-image classification: what is this a picture of.

Channel order was worth measuring rather than assuming. These ship in the
OpenCV zoo, whose demos hand them BGR, so BGR is the obvious guess — and it
is wrong. Feeding RGB with ImageNet statistics moves top-1 confidence on the
sample images from 0.11/0.34/0.56 to 0.75/0.69/0.82 and fixes the labels
(lemon, cowboy hat, soccer ball). Swapped channels never raise; they just
quietly cost you most of the model's accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import preprocess as pre
from .labels import imagenet
from .session import Model


@dataclass(frozen=True)
class Prediction:
    index: int
    label: str
    score: float


class Classifier:
    """MobileNetV2 (fast) or PP-ResNet50 (accurate, and much slower)."""

    def __init__(self, key: str = "classify/mobilenetv2") -> None:
        self.model = Model(key)
        self.height, self.width = self.model.input_hw() or (224, 224)
        self._labels = imagenet()

    def __call__(self, rgb: np.ndarray, top: int = 5) -> list[Prediction]:
        resized = pre.stretch(rgb, self.width, self.height)
        tensor = pre.to_tensor(resized, scale=1 / 255.0,
                               mean=pre.IMAGENET_MEAN, std=pre.IMAGENET_STD)
        logits = self.model.run(tensor)[0].reshape(-1)
        probs = pre.softmax(logits)

        order = probs.argsort()[::-1][:top]
        return [Prediction(int(i), self._labels[int(i)], float(probs[i])) for i in order]
