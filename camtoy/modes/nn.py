"""Camera modes backed by neural networks.

Each one is the same shape: hand the newest frame to a Worker, draw whatever
the Worker last finished on top of the live picture. The overlay is always a
little behind the video — by 100ms for the small models and by two seconds
for the big ones — and the status line reports the model's own rate next to
the display's so the difference is never a mystery.

Inference runs at a fixed WORK_SIZE rather than the display's size. A
terminal might only be 120 cells wide, and a detector fed a 120px frame finds
nothing; conversely a window at 640x480 would make the slow models slower for
no visible gain.
"""

from __future__ import annotations

import numpy as np

from .. import imageops as io
from ..nn import draw
from ..nn.worker import Worker
from .base import DEFAULT_OUTDIR, Mode, save_png


class NNMode(Mode):
    """Shared plumbing: a worker, a saved frame, and an honest status line."""

    # Big enough for detectors to work with, small enough to stay responsive.
    WORK_SIZE = (480, 360)

    def __init__(self, outdir=DEFAULT_OUTDIR) -> None:
        self.outdir = outdir
        self.overlay = True
        self._worker: Worker | None = None
        self._last: np.ndarray | None = None

    # -- lifecycle ---------------------------------------------------------

    def build(self):
        """Return the callable the worker runs. Called once, lazily, so model
        loading happens after the display is up and can report progress."""
        raise NotImplementedError

    @property
    def worker(self) -> Worker:
        if self._worker is None:
            self._worker = Worker(self.build(), name=f"camtoy-{self.name}")
        return self._worker

    def close(self) -> None:
        if self._worker is not None:
            self._worker.close()

    def work_size(self, display_size: tuple[int, int]) -> tuple[int, int]:
        return self.WORK_SIZE

    # -- frame flow --------------------------------------------------------

    def render(self, frame: np.ndarray) -> np.ndarray:
        self.worker.submit(frame)
        out = frame if not self.overlay else self.paint(frame, self.worker.result)
        self._last = out
        return out

    def paint(self, frame: np.ndarray, result) -> np.ndarray:
        """Draw `result` (possibly None, on the first frames) onto `frame`."""
        return frame

    def on_key(self, key: str) -> str | None:
        if key == "o":
            self.overlay = not self.overlay
            return f"overlay {'on' if self.overlay else 'off'}"
        if key == "s" and self._last is not None:
            return f"saved {save_png(self._last, self.outdir, self.name).name}"
        return None

    def status(self) -> str:
        worker = self._worker
        if worker is None:
            return "loading model…"
        if worker.error:
            return f"ERROR {worker.error[:48]}"
        if worker.completed == 0:
            return "warming up…"
        return f"model {worker.fps:4.1f}fps {self.detail()}"

    def detail(self) -> str:
        return ""


# --------------------------------------------------------------------------
# detection and classification
# --------------------------------------------------------------------------

class DetectMode(NNMode):
    name = "detect"
    keys = (("o", "overlay"), ("s", "save png"))

    def __init__(self, model: str = "nanodet", threshold: float = 0.35, **kw) -> None:
        super().__init__(**kw)
        self.model_name = model
        self.threshold = threshold
        self._count = 0

    def build(self):
        from ..nn.detect import DETECTORS
        detector = DETECTORS[self.model_name]()
        return lambda frame: detector(frame, self.threshold)

    def paint(self, frame, result):
        if not result:
            return frame
        from ..nn.labels import COCO_COLORS
        self._count = len(result)
        boxes = np.array([d.box for d in result])
        colours = np.array([COCO_COLORS[d.index] for d in result])
        texts = [f"{d.label} {d.score:.2f}" for d in result]
        return draw.boxes(frame, boxes, texts, colours)

    def detail(self) -> str:
        return f"{self.model_name} {self._count} obj"


class ClassifyMode(NNMode):
    name = "classify"
    keys = (("o", "overlay"), ("s", "save png"))

    def __init__(self, model: str = "classify/mobilenetv2", **kw) -> None:
        super().__init__(**kw)
        self.key = model

    def build(self):
        from ..nn.classify import Classifier
        classifier = Classifier(self.key)
        return lambda frame: classifier(frame, top=5)

    def paint(self, frame, result):
        if not result:
            return frame
        lines = [f"{p.score:5.1%}  {p.label}" for p in result]
        return draw.caption(frame, lines)


# --------------------------------------------------------------------------
# faces and bodies
# --------------------------------------------------------------------------

class FaceMode(NNMode):
    name = "face"
    keys = (("e", "expression"), ("r", "remember face"), ("o", "overlay"), ("s", "save png"))

    def __init__(self, expression: bool = True, **kw) -> None:
        super().__init__(**kw)
        self.expression = expression
        self.remember = False
        self._known: list[np.ndarray] = []
        self._faces = 0

    def build(self):
        from ..nn.faces import Expression, SFace, YuNet
        detector, mood, ident = YuNet(), Expression(), SFace()

        def run(frame):
            faces = detector(frame)
            out = []
            for face in faces:
                label, score = mood(frame, face) if self.expression else ("", 0.0)
                who = ""
                embedding = ident.embed(frame, face)
                if self.remember:
                    self._known.append(embedding)
                    self.remember = False
                    who = f"saved #{len(self._known)}"
                elif self._known:
                    sims = [SFace.similarity(embedding, k) for k in self._known]
                    best = int(np.argmax(sims))
                    who = f"#{best + 1} {sims[best]:.2f}" if sims[best] > 0.36 else "unknown"
                out.append((face, label, score, who))
            return out

        return run

    def paint(self, frame, result):
        if not result:
            return frame
        self._faces = len(result)
        boxes = np.array([f.box for f, *_ in result])
        texts = [" ".join(p for p in (f"{label} {score:.2f}" if label else "", who) if p).strip()
                 or f"{f.score:.2f}" for f, label, score, who in result]
        out = draw.boxes(frame, boxes, texts)
        for face, *_ in result:
            out = draw.points(out, face.landmarks, (255, 220, 0), 2)
        return out

    def on_key(self, key: str) -> str | None:
        if key == "e":
            self.expression = not self.expression
            return f"expression {'on' if self.expression else 'off'}"
        if key == "r":
            self.remember = True
            return "next face will be remembered"
        return super().on_key(key)

    def detail(self) -> str:
        return f"{self._faces} face(s), {len(self._known)} known"


class PoseMode(NNMode):
    name = "pose"
    keys = (("o", "overlay"), ("s", "save png"))

    def build(self):
        from ..nn.bodies import PosePipeline
        pipeline = PosePipeline()
        return lambda frame: pipeline(frame, limit=1)

    def paint(self, frame, result):
        from ..nn.bodies import POSE_EDGES
        out = frame
        for points, visible, _score in result or []:
            out = draw.skeleton(out, points, POSE_EDGES, visible=visible)
        return out

    def detail(self) -> str:
        return "no body" if not (self._worker and self._worker.result) else "tracking"


class HandMode(NNMode):
    name = "hand"
    keys = (("o", "overlay"), ("s", "save png"))

    def build(self):
        from ..nn.bodies import HandPipeline
        pipeline = HandPipeline()
        return lambda frame: pipeline(frame, limit=2)

    def paint(self, frame, result):
        from ..nn.bodies import HAND_EDGES
        out = frame
        for points, _score in result or []:
            out = draw.skeleton(out, points, HAND_EDGES, colour=(255, 150, 0),
                                radius=2, width=2)
        return out

    def detail(self) -> str:
        result = self._worker.result if self._worker else None
        return f"{len(result or [])} hand(s)"


# --------------------------------------------------------------------------
# pixels
# --------------------------------------------------------------------------

class SegmentMode(NNMode):
    name = "segment"
    keys = (("b", "background"), ("o", "overlay"), ("s", "save png"))

    BACKGROUNDS = ("tint", "blur", "black", "thermal")

    def __init__(self, background: str = "blur", **kw) -> None:
        super().__init__(**kw)
        self.background = background if background in self.BACKGROUNDS else "blur"

    def build(self):
        from ..nn.pixels import HumanSegment
        segment = HumanSegment()
        return segment

    def paint(self, frame, result):
        if result is None or result.shape != frame.shape[:2]:
            return frame
        mask = result[..., None]
        if self.background == "tint":
            return draw.mask_overlay(frame, result, (0, 255, 160), 0.45)
        if self.background == "black":
            back = np.zeros_like(frame, np.float32)
        elif self.background == "thermal":
            back = io.apply_palette(io.luma(frame), "thermal").astype(np.float32)
        else:
            back = io.resize(io.resize(frame, frame.shape[1] // 12, frame.shape[0] // 12),
                             frame.shape[1], frame.shape[0]).astype(np.float32)
        return io.to_u8(frame.astype(np.float32) * mask + back * (1 - mask))

    def on_key(self, key: str) -> str | None:
        if key == "b":
            i = self.BACKGROUNDS.index(self.background)
            self.background = self.BACKGROUNDS[(i + 1) % len(self.BACKGROUNDS)]
            return f"background {self.background}"
        return super().on_key(key)

    def detail(self) -> str:
        return self.background


class DepthMode(NNMode):
    name = "depth"
    keys = (("p", "palette"), ("m", "mix"), ("o", "overlay"), ("s", "save png"))

    PALETTES = ("thermal", "ice", "mono", "amber", "pop")

    def __init__(self, side: int = 266, **kw) -> None:
        super().__init__(**kw)
        self.side = side
        self.palette_ix = 0
        self.mix = False

    def build(self):
        from ..nn.pixels import Depth
        depth = Depth(side=self.side)
        return depth

    def paint(self, frame, result):
        if result is None or result.shape != frame.shape[:2]:
            return frame
        coloured = io.apply_palette(result, self.PALETTES[self.palette_ix])
        if not self.mix:
            return coloured
        return io.to_u8(frame.astype(np.float32) * 0.5 + coloured.astype(np.float32) * 0.5)

    def on_key(self, key: str) -> str | None:
        if key == "p":
            self.palette_ix = (self.palette_ix + 1) % len(self.PALETTES)
            return f"palette {self.PALETTES[self.palette_ix]}"
        if key == "m":
            self.mix = not self.mix
            return f"mix {'on' if self.mix else 'off'}"
        return super().on_key(key)

    def detail(self) -> str:
        return self.PALETTES[self.palette_ix]


class StyleMode(NNMode):
    name = "style"
    keys = (("n", "next style"), ("o", "overlay"), ("s", "save png"))
    # Style output is the whole picture, so there is no point rendering it
    # larger than the display will show.
    WORK_SIZE = (320, 240)

    def __init__(self, style: str = "mosaic", **kw) -> None:
        super().__init__(**kw)
        from ..nn.pixels import STYLE_KEYS
        self.keys_available = list(STYLE_KEYS)
        wanted = f"style/{style}"
        self.index = self.keys_available.index(wanted) if wanted in self.keys_available else 0
        self._cache: dict[str, object] = {}

    def build(self):
        from ..nn.pixels import Style

        def run(frame):
            key = self.keys_available[self.index]
            model = self._cache.get(key)
            if model is None:
                model = self._cache[key] = Style(key)
            return model(frame)

        return run

    def paint(self, frame, result):
        if result is None:
            return frame
        return result if result.shape == frame.shape else io.resize(
            result, frame.shape[1], frame.shape[0])

    def on_key(self, key: str) -> str | None:
        if key == "n":
            self.index = (self.index + 1) % len(self.keys_available)
            return f"style {self.keys_available[self.index].split('/')[1]}"
        return super().on_key(key)

    def detail(self) -> str:
        return self.keys_available[self.index].split("/")[1]


# --------------------------------------------------------------------------
# text and tracking
# --------------------------------------------------------------------------

class TextMode(NNMode):
    name = "text"
    keys = (("o", "overlay"), ("s", "save png"))
    WORK_SIZE = (640, 480)          # small text needs the pixels

    def build(self):
        from ..nn.reading import TextPipeline
        pipeline = TextPipeline()
        return lambda frame: pipeline(frame, limit=6)

    def paint(self, frame, result):
        if not result:
            return frame
        boxes = np.array([t.box for t in result])
        return draw.boxes(frame, boxes, [t.text for t in result],
                          np.tile(np.array([[255, 210, 0]]), (len(result), 1)))

    def detail(self) -> str:
        result = self._worker.result if self._worker else None
        return f"{len(result or [])} string(s)"


class TrackMode(NNMode):
    """Point the box at something with the arrow keys, press enter, follow it.

    The tracker is stateful and fast, so unlike the other modes it runs
    inline: at 37ms it keeps up with the camera, and threading it would only
    add a frame of lag to something whose whole job is to not lag.
    """

    name = "track"
    keys = (("arrows", "move box"), ("+ -", "resize"), ("enter", "lock on"),
            ("c", "clear"), ("s", "save png"))

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._tracker = None
        self._box = None
        self._pending = None

    def build(self):
        return lambda frame: None                # unused; see render()

    @property
    def tracker(self):
        if self._tracker is None:
            from ..nn.tracking import VitTrack
            self._tracker = VitTrack()
        return self._tracker

    def render(self, frame: np.ndarray) -> np.ndarray:
        height, width = frame.shape[:2]
        if self._box is None:
            side = min(width, height) // 4
            self._box = [width // 2 - side // 2, height // 2 - side // 2,
                         width // 2 + side // 2, height // 2 + side // 2]

        if self._pending == "start":
            self.tracker.start(frame, self._box)
            self._pending = None
        elif self._pending == "clear":
            self.tracker.stop()
            self._pending = None

        if self.tracker.active:
            box, score = self.tracker.update(frame)
            out = draw.boxes(frame, np.array([box]), [f"tracking {score:.2f}"],
                             np.array([[0, 255, 128]]))
        else:
            out = draw.boxes(frame, np.array([self._box]), ["enter to lock on"],
                             np.array([[255, 255, 0]]), width=1)
        self._last = out
        return out

    def on_key(self, key: str) -> str | None:
        step = 12
        if self._box is None:
            return None
        if key in ("up", "down", "left", "right"):
            dx = (key == "right") - (key == "left")
            dy = (key == "down") - (key == "up")
            self._box = [self._box[0] + dx * step, self._box[1] + dy * step,
                         self._box[2] + dx * step, self._box[3] + dy * step]
            return None
        if key in "+=-":
            grow = step if key in "+=" else -step
            self._box = [self._box[0] - grow, self._box[1] - grow,
                         self._box[2] + grow, self._box[3] + grow]
            return None
        if key == "enter":
            self._pending = "start"
            return "locked on"
        if key == "c":
            self._pending = "clear"
            return "cleared"
        if key == "s" and self._last is not None:
            return f"saved {save_png(self._last, self.outdir, self.name).name}"
        return None

    def status(self) -> str:
        if self._tracker is not None and self._tracker.active:
            return f"tracking  score {self._tracker.score:.2f}"
        return "arrows to aim, enter to lock on"

    def close(self) -> None:
        if self._worker is not None:
            self._worker.close()


NN_MODES = {
    "detect": DetectMode, "classify": ClassifyMode, "face": FaceMode,
    "pose": PoseMode, "hand": HandMode, "segment": SegmentMode,
    "depth": DepthMode, "style": StyleMode, "text": TextMode, "track": TrackMode,
}
