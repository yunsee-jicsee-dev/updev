"""onnxruntime sessions, tuned for a Raspberry Pi 5 and loaded lazily.

Sessions are cached by model key. Loading a 100MB graph takes a second or
two, which is fine once and intolerable per frame, and several modes reach
for more than one model (a detector feeding a classifier, say).

Nothing here knows what any particular model means — that is each wrapper's
job. This is only about getting a session and getting arrays through it.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import numpy as np

from .registry import MODELS, ModelSpec

# onnxruntime probes /sys/class/drm looking for GPUs it will not find on a
# Pi and warns loudly about each miss. Severity 3 = errors only.
_LOG_SEVERITY = 3


class ModelMissing(RuntimeError):
    """A model is not on disk yet. The message says how to get it."""


class ModelBroken(RuntimeError):
    """A model is present but cannot execute under this onnxruntime."""


def _import_ort():
    try:
        import onnxruntime as ort
    except ImportError as e:
        raise RuntimeError(
            "onnxruntime is not installed. It carries the neural-network modes:\n"
            "    python3 -m venv --system-site-packages .venv-nn\n"
            "    .venv-nn/bin/pip install onnxruntime"
        ) from e
    ort.set_default_logger_severity(_LOG_SEVERITY)
    return ort


def resolve(key: str) -> ModelSpec:
    try:
        spec = MODELS[key]
    except KeyError:
        raise KeyError(f"unknown model {key!r}; see `camtoy models list`") from None
    if spec.broken:
        # Failing here beats failing on the first frame, halfway into a mode
        # that has already taken over the terminal.
        raise ModelBroken(f"{key} does not run on this machine: {spec.broken}")
    if not spec.present:
        raise ModelMissing(
            f"{key} is not downloaded ({spec.megabytes:.0f}MB). "
            f"Get it with:  camtoy models pull {key}"
        )
    return spec


_LOADED: set[str] = set()


@lru_cache(maxsize=None)
def session(key: str):
    """Cached InferenceSession for a registry key."""
    ort = _import_ort()
    spec = resolve(key)
    _LOADED.add(key)

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    # Four A76 cores. Letting ORT spawn its default pool oversubscribes them
    # and costs more in contention than it wins in parallelism.
    options.intra_op_num_threads = int(os.environ.get("CAMTOY_THREADS") or os.cpu_count() or 4)
    options.inter_op_num_threads = 1
    options.log_severity_level = _LOG_SEVERITY

    return ort.InferenceSession(str(spec.path), options,
                                providers=["CPUExecutionProvider"])


class Model:
    """A session plus the shape bookkeeping every wrapper otherwise repeats."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.session = session(key)
        self.inputs = [i.name for i in self.session.get_inputs()]
        self.outputs = [o.name for o in self.session.get_outputs()]
        self.input_shapes = {i.name: i.shape for i in self.session.get_inputs()}

    @property
    def input_name(self) -> str:
        return self.inputs[0]

    def input_hw(self, name: str | None = None) -> tuple[int, int]:
        """(height, width) the model wants, or (0, 0) where it is dynamic.

        Handles both layouts: NCHW models report [N, C, H, W] and the
        MediaPipe-derived ones report [N, H, W, C].
        """
        shape = self.input_shapes[name or self.input_name]
        if len(shape) != 4:
            return (0, 0)
        dims = [d if isinstance(d, int) else 0 for d in shape]
        if dims[1] in (1, 3) and dims[3] not in (1, 3):
            return dims[2], dims[3]             # NCHW
        if dims[3] in (1, 3):
            return dims[1], dims[2]             # NHWC
        return dims[2], dims[3]

    @property
    def is_nhwc(self) -> bool:
        shape = self.input_shapes[self.input_name]
        return len(shape) == 4 and shape[3] in (1, 3) and shape[1] not in (1, 3)

    def run(self, feed: dict[str, np.ndarray] | np.ndarray) -> list[np.ndarray]:
        if isinstance(feed, np.ndarray):
            feed = {self.input_name: feed}
        return self.session.run(None, feed)

    def run_named(self, feed) -> dict[str, np.ndarray]:
        return dict(zip(self.outputs, self.run(feed)))


def loaded_keys() -> list[str]:
    """Which sessions are currently cached — used by the status line."""
    return sorted(_LOADED)
