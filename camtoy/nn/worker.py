"""Inference on its own thread, so the video never waits for the model.

The slowest model here takes two seconds a frame and the camera delivers
thirty a second. Calling a model from render() would drop the preview to the
model's rate and make the whole thing feel broken — you would be looking at
a two-second-old picture of yourself.

So the worker keeps only the newest submitted frame, and the display keeps
drawing live video with the most recent *result* overlaid. The overlay lags,
which is honest and obvious, but the picture does not.

onnxruntime releases the GIL inside its kernels, so this genuinely overlaps
with the terminal writer rather than just interleaving with it.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import numpy as np


class Worker:
    """Runs `fn(frame)` on the latest frame, forever, on one thread."""

    def __init__(self, fn: Callable[[np.ndarray], Any], name: str = "camtoy-nn") -> None:
        self._fn = fn
        self._pending: np.ndarray | None = None
        self._result: Any = None
        self._cv = threading.Condition()
        self._stopping = False

        self.latency = 0.0          # seconds for the last completed inference
        self.error: str | None = None
        self.completed = 0

        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def submit(self, frame: np.ndarray) -> None:
        """Offer a frame. Replaces any frame not yet picked up."""
        with self._cv:
            self._pending = frame
            self._cv.notify()

    @property
    def result(self) -> Any:
        with self._cv:
            return self._result

    @property
    def busy(self) -> bool:
        with self._cv:
            return self._pending is not None

    def _loop(self) -> None:
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._pending is not None or self._stopping)
                if self._stopping:
                    return
                frame, self._pending = self._pending, None

            started = time.perf_counter()
            try:
                value = self._fn(frame)
                error = None
            except Exception as e:                  # a bad frame must not kill the mode
                value, error = None, f"{type(e).__name__}: {e}"

            with self._cv:
                if error is None:
                    self._result = value
                    self.completed += 1
                self.error = error
                self.latency = time.perf_counter() - started

    def close(self) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        self._thread.join(timeout=2)

    @property
    def fps(self) -> float:
        return 1.0 / self.latency if self.latency > 0 else 0.0
