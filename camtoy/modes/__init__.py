"""camtoy's modes: four that are pure numpy, ten that run a network.

The neural ones are imported lazily. They pull in onnxruntime, which is a
slow import and is absent on a machine that only wants the numpy toys.
"""

from __future__ import annotations

from .base import Mode, run, save_png
from .lightpaint import LightPaintMode
from .live import LiveMode
from .slitscan import SlitScanMode
from .theremin import ThereminMode

MODES = {
    "live": LiveMode,
    "slit": SlitScanMode,
    "paint": LightPaintMode,
    "theremin": ThereminMode,
}

NN_MODE_NAMES = ("detect", "classify", "face", "pose", "hand",
                 "segment", "depth", "style", "text", "track")


def nn_mode(name: str):
    """Import and return a neural mode class by name."""
    from .nn import NN_MODES
    return NN_MODES[name]


def mode_keys(name: str) -> tuple[tuple[str, str], ...]:
    """Keybindings for any mode, without importing onnxruntime to find out."""
    if name in MODES:
        return MODES[name].keys
    return nn_mode(name).keys


__all__ = [
    "MODES", "NN_MODE_NAMES", "Mode", "run", "save_png", "nn_mode", "mode_keys",
    "LiveMode", "SlitScanMode", "LightPaintMode", "ThereminMode",
]
