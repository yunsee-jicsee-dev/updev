"""updev's physical front panel — an ST7735S on SPI, driven from the same scan.

    from updev.panel import open_panel, boot

    screen = open_panel()
    boot(screen)
    screen.close()          # the summary stays on the glass

See `screen.py` for the wiring and the one caveat that matters (the panel sits
on a bus updev also reports on), and `boot.py` for what gets drawn.
"""

from __future__ import annotations

from .boot import boot, progress, splash, summary
from .screen import HEIGHT, WIDTH, PanelUnavailable, Screen, open_panel

__all__ = [
    "WIDTH", "HEIGHT", "Screen", "PanelUnavailable", "open_panel",
    "boot", "splash", "progress", "summary",
]
