"""camtoy — camera toys for a Raspberry Pi and a cheap USB webcam.

Four modes over one capture core:

    live        live video in the terminal with a chain of numpy effects
    slit        slit-scan: every output row comes from a different moment
    paint       long-exposure light painting
    theremin    motion drives a synthesiser

No OpenCV, no mediapipe, no models. Frames come from ffmpeg, everything after
that is numpy.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
