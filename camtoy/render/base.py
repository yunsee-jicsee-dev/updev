"""The contract every camtoy display backend implements.

A mode never knows whether it is drawing to a terminal or an SDL window. It
asks the display how many pixels it wants, hands back an RGB array that size,
and reads whatever keys arrived. That is the whole interface.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class Display(Protocol):
    """A place to put frames and a source of keystrokes."""

    running: bool

    def target_size(self) -> tuple[int, int]:
        """(width, height) in pixels that show() wants. May change per frame
        — terminals get resized while running."""

    def show(self, rgb: np.ndarray) -> None:
        """Present one uint8 (H, W, 3) frame."""

    def keys(self) -> list[str]:
        """Keys pressed since the last call: lowercase letters/digits, plus
        'esc', 'space', 'enter', 'up', 'down', 'left', 'right'."""

    def status(self, text: str) -> None:
        """Show a one-line caption. Backends may ignore this."""

    def close(self) -> None:
        ...
