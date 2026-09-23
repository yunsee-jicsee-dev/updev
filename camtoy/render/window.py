"""An SDL window, for when the terminal's ~100x50 grid is not enough.

Same Display contract as the terminal backend: modes render at target_size()
and this scales the result up to the window. Scaling happens here rather than
in the mode so the effects always run on the small array — on a Pi that is
the difference between 20fps and 6.
"""

from __future__ import annotations

import os

import numpy as np

# SDL prints a banner on import and probes audio we never use.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402


class WindowDisplay:
    """Upscaled video in a resizable SDL window."""

    _NAMES = {
        pygame.K_ESCAPE: "esc", pygame.K_SPACE: "space", pygame.K_RETURN: "enter",
        pygame.K_UP: "up", pygame.K_DOWN: "down", pygame.K_LEFT: "left",
        pygame.K_RIGHT: "right",
    }

    def __init__(self, width: int = 480, height: int = 360, scale: float = 2.0,
                 title: str = "camtoy") -> None:
        self.running = True
        self._w, self._h = width, height

        pygame.display.init()
        pygame.font.init()
        self._screen = pygame.display.set_mode(
            (int(width * scale), int(height * scale)), pygame.RESIZABLE
        )
        pygame.display.set_caption(title)
        self._font = pygame.font.SysFont(None, 22)
        self._surface = pygame.Surface((width, height))
        self._status = ""

    def target_size(self) -> tuple[int, int]:
        return self._w, self._h

    def show(self, rgb: np.ndarray) -> None:
        if rgb.shape[:2] != (self._h, self._w):
            from .. import imageops as io
            rgb = io.resize(rgb, self._w, self._h)
        # surfarray is column-major: it wants (W, H, 3).
        pygame.surfarray.blit_array(self._surface, np.ascontiguousarray(rgb.transpose(1, 0, 2)))
        pygame.transform.scale(self._surface, self._screen.get_size(), self._screen)
        if self._status:
            self._draw_status()
        pygame.display.flip()

    def _draw_status(self) -> None:
        label = self._font.render(self._status, True, (255, 255, 255))
        w, h = self._screen.get_size()
        strip = pygame.Surface((w, label.get_height() + 6))
        strip.set_alpha(150)
        strip.fill((0, 0, 0))
        self._screen.blit(strip, (0, h - label.get_height() - 6))
        self._screen.blit(label, (6, h - label.get_height() - 3))

    def status(self, text: str) -> None:
        self._status = text

    def keys(self) -> list[str]:
        out = []
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
                out.append("esc")
            elif event.type == pygame.KEYDOWN:
                out.append(self._NAMES.get(event.key) or (event.unicode or "").lower())
        return [k for k in out if k]

    def close(self) -> None:
        self.running = False
        pygame.display.quit()

    def __enter__(self) -> "WindowDisplay":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
