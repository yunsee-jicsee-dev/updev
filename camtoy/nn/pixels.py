"""Image-to-image models: depth, matting, learned edges, style, restoration.

These share a shape: a frame goes in, a picture comes out, and the only hard
part is agreeing with the model about units. Where the right normalisation
was not obvious from the graph it was measured against sample photographs
rather than guessed — the notes on each class say which.
"""

from __future__ import annotations

import numpy as np

from .. import imageops as io
from . import preprocess as pre
from .session import Model


class Depth:
    """Depth Anything V2 small: relative monocular depth.

    The input is dynamic but the graph floors it to multiples of 14 (it is a
    ViT with 14px patches), so feeding a non-multiple silently resizes the
    output and misaligns it with the frame. `side` is kept on the grid.
    """

    PATCH = 14

    def __init__(self, key: str = "depth/depthanything", side: int = 266) -> None:
        self.model = Model(key)
        self.side = max(self.PATCH * 4, round(side / self.PATCH) * self.PATCH)

    def __call__(self, rgb: np.ndarray) -> np.ndarray:
        """Returns depth in 0..255, near = bright."""
        resized = pre.stretch(rgb, self.side, self.side)
        tensor = pre.to_tensor(resized, scale=1 / 255.0,
                               mean=pre.IMAGENET_MEAN, std=pre.IMAGENET_STD)
        depth = self.model.run({"pixel_values": tensor})[0]
        depth = np.asarray(depth).reshape(depth.shape[-2], depth.shape[-1])
        # The model outputs inverse depth in arbitrary units; only the
        # ordering is meaningful, so normalise per frame.
        return io.resize(io.gray_to_rgb(io.normalize(depth)),
                         rgb.shape[1], rgb.shape[0])[:, :, 0].astype(np.float32)

    def colorized(self, rgb: np.ndarray, palette: str = "thermal") -> np.ndarray:
        return io.apply_palette(self(rgb), palette)


class HumanSegment:
    """PPHumanSeg: a person/background matte at 192x192.

    Trained on RGB scaled to 0..1 then standardised with mean=std=0.5.

    The two output channels are *probabilities*, not logits — they sum to
    exactly 1.0 already. Softmaxing them again is silent and nearly invisible:
    the matte still tracks the person, it just gets squashed into 0.269..0.731
    (which is what softmax([0,1]) returns) and every downstream threshold
    stops working. The giveaway was that the output range did not change at
    all when the input normalisation did.
    """

    def __init__(self, key: str = "segment/pphumanseg") -> None:
        self.model = Model(key)
        self.height, self.width = self.model.input_hw()

    def __call__(self, rgb: np.ndarray) -> np.ndarray:
        """Returns a float mask in 0..1 at the frame's own size."""
        resized = pre.stretch(rgb, self.width, self.height)
        tensor = pre.to_tensor(resized, scale=1 / 255.0, mean=0.5, std=0.5)
        person = self.model.run(tensor)[0][0][1]        # channel 1 of (2, H, W)
        return io.resize(io.gray_to_rgb(person * 255.0),
                         rgb.shape[1], rgb.shape[0])[:, :, 0].astype(np.float32) / 255.0


class DexiNed:
    """Learned edge detection — what Sobel would be if it had seen a dataset.

    Caffe-lineage model: BGR with per-channel mean subtraction and no scaling.
    It emits seven side outputs; the reference fuses them by averaging the
    sigmoids, which suppresses the noise any single stage carries.
    """

    MEAN = np.array([103.939, 116.779, 123.68], dtype=np.float32)

    def __init__(self, key: str = "edge/dexined") -> None:
        self.model = Model(key)
        self.height, self.width = self.model.input_hw()

    def __call__(self, rgb: np.ndarray) -> np.ndarray:
        resized = pre.stretch(rgb, self.width, self.height)
        tensor = pre.to_tensor(resized, bgr=True, mean=self.MEAN)
        maps = [pre.sigmoid(np.asarray(o).reshape(self.height, self.width))
                for o in self.model.run(tensor)]
        fused = np.mean(maps, axis=0)
        edges = io.normalize(fused * 255.0)
        return io.resize(io.gray_to_rgb(edges), rgb.shape[1], rgb.shape[0])[:, :, 0]


class Style:
    """fast-neural-style. Raw 0..255 RGB in and out, no normalisation."""

    def __init__(self, key: str = "style/mosaic") -> None:
        self.model = Model(key)
        self.height, self.width = self.model.input_hw()

    def __call__(self, rgb: np.ndarray) -> np.ndarray:
        resized = pre.stretch(rgb, self.width, self.height)
        out = self.model.run(pre.to_tensor(resized))[0][0]      # (3, H, W)
        styled = io.to_u8(out.transpose(1, 2, 0))
        return io.resize(styled, rgb.shape[1], rgb.shape[0])


class Deblur:
    """NAFNet motion deblurring. Dynamic input, 0..1 RGB.

    The declared input is fully dynamic but the exported graph is not: below
    384px an internal downsampling chain reaches a zero-sized axis and a Pad
    op raises. 384 is the smallest size that runs, and it costs ~22s a frame
    on this board — this is a single-shot tool, not a filter.
    """

    MIN_SIDE = 384

    def __init__(self, key: str = "restore/nafnet", side: int = MIN_SIDE) -> None:
        self.model = Model(key)
        self.side = max(int(side), self.MIN_SIDE)

    def __call__(self, rgb: np.ndarray) -> np.ndarray:
        resized = pre.stretch(rgb, self.side, self.side)
        out = self.model.run(pre.to_tensor(resized, scale=1 / 255.0))[0][0]
        restored = io.to_u8(out.transpose(1, 2, 0) * 255.0)
        return io.resize(restored, rgb.shape[1], rgb.shape[0])


class Inpaint:
    """LaMa: erase whatever the mask covers and hallucinate what was behind."""

    SIDE = 512

    def __init__(self, key: str = "restore/lama") -> None:
        self.model = Model(key)

    def __call__(self, rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """`mask` is (H, W) in 0..1; anything above 0.5 gets erased."""
        image = pre.to_tensor(pre.stretch(rgb, self.SIDE, self.SIDE), scale=1 / 255.0)
        small = io.resize(io.gray_to_rgb(mask * 255.0), self.SIDE, self.SIDE)[:, :, 0]
        holes = (small > 127).astype(np.float32)[None, None]
        out = self.model.run({"image": image, "mask": holes})[0][0]
        # LaMa returns 0..255 floats rather than the 0..1 it was fed.
        return io.resize(io.to_u8(out.transpose(1, 2, 0)), rgb.shape[1], rgb.shape[0])


class OpticalFlow:
    """RAFT dense flow between two frames, drawn as a colour wheel."""

    def __init__(self, key: str = "flow/raft") -> None:
        self.model = Model(key)
        self.height, self.width = self.model.input_hw()

    def __call__(self, previous: np.ndarray, current: np.ndarray) -> np.ndarray:
        """Returns (H, W, 2) flow in model pixels."""
        a = pre.to_tensor(pre.stretch(previous, self.width, self.height))
        b = pre.to_tensor(pre.stretch(current, self.width, self.height))
        outputs = self.model.run(dict(zip(self.model.inputs, [a, b])))
        # Two flow fields come back; the full-resolution one is what we want.
        full = max(outputs, key=lambda o: o.shape[-1])
        return np.asarray(full)[0].transpose(1, 2, 0)

    @staticmethod
    def colorize(flow: np.ndarray) -> np.ndarray:
        """Direction as hue, magnitude as brightness — the standard wheel."""
        angle = np.arctan2(flow[..., 1], flow[..., 0])
        magnitude = np.hypot(flow[..., 0], flow[..., 1])
        hue = (angle / (2 * np.pi) + 0.5) % 1.0
        value = np.clip(magnitude / max(np.percentile(magnitude, 99), 1e-6), 0, 1)

        i = (hue * 6).astype(int) % 6
        f = hue * 6 - np.floor(hue * 6)
        p, q, t = np.zeros_like(value), value * (1 - f), value * f
        r = np.select([i == 0, i == 1, i == 2, i == 3, i == 4], [value, q, p, p, t], value)
        g = np.select([i == 0, i == 1, i == 2, i == 3, i == 4], [t, value, value, q, p], p)
        b = np.select([i == 0, i == 1, i == 2, i == 3, i == 4], [p, p, t, value, value], q)
        return io.to_u8(np.stack([r, g, b], axis=2) * 255.0)


class SegmentAnything:
    """EfficientSAM: one click, one mask."""

    def __init__(self, key: str = "segment/efficientsam") -> None:
        self.model = Model(key)
        self.height, self.width = self.model.input_hw("batched_images")

    def __call__(self, rgb: np.ndarray, point: tuple[float, float]) -> np.ndarray:
        """`point` is (x, y) in frame pixels. Returns a 0..1 mask."""
        image = pre.to_tensor(pre.stretch(rgb, self.width, self.height), scale=1 / 255.0)
        src_h, src_w = rgb.shape[:2]
        coords = np.array([[[[point[0] * self.width / src_w,
                              point[1] * self.height / src_h]]]], np.float32)
        labels = np.ones((1, 1, 1), np.float32)          # 1 = "include this point"
        masks = self.model.run({
            "batched_images": image,
            "batched_point_coords": coords,
            "batched_point_labels": labels,
        })[0]
        best = np.asarray(masks)[0, 0, 0]                # highest-scoring of three
        mask = (best > 0).astype(np.float32) * 255.0
        return io.resize(io.gray_to_rgb(mask), src_w, src_h)[:, :, 0] / 255.0


STYLE_KEYS = ("style/mosaic", "style/candy", "style/udnie",
              "style/rain_princess", "style/pointilism")
