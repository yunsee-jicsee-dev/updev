"""Display backends. Import the heavy one only if it is actually asked for."""

from __future__ import annotations

from .base import Display

BACKENDS = ("term", "window")


def open_display(kind: str = "term", **kwargs) -> Display:
    """Build a display. `kind` is 'term' or 'window'."""
    if kind == "term":
        from .terminal import TerminalDisplay
        return TerminalDisplay(**{k: v for k, v in kwargs.items()
                                  if k in {"truecolor", "max_width"}})
    if kind == "window":
        # pygame pulls in SDL, which is a slow import and a hard failure on a
        # headless box — so it stays behind this branch.
        from .window import WindowDisplay
        return WindowDisplay(**{k: v for k, v in kwargs.items()
                                if k in {"width", "height", "scale", "title"}})
    raise ValueError(f"unknown display backend {kind!r} (expected one of {BACKENDS})")


__all__ = ["Display", "open_display", "BACKENDS"]
