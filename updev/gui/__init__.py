"""`updev gui` — the window.

tkinter, because it is in the standard library and on Raspberry Pi OS it is
already installed. The rest of updev has no runtime dependency beyond rich and
click, and a GUI that dragged in a toolkit would be the first thing to break
that.
"""

from __future__ import annotations

import os

__all__ = ["available", "launch"]


def available() -> tuple[bool, str]:
    """(can we open a window?, why not). Checked before the scan, not after."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False, ("python3-tk is not installed — "
                       "sudo apt install python3-tk")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False, ("no display: neither DISPLAY nor WAYLAND_DISPLAY is set. "
                       "Run it on the desktop, or over ssh -X.")
    return True, ""


def launch(result, target: str = "", deep: bool = False) -> None:
    from .app import DeviceGui

    DeviceGui(result, target=target, deep=deep).run()
