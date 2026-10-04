"""Video in a terminal, using half-block characters.

U+2580 UPPER HALF BLOCK fills the top half of a cell with the foreground
colour and leaves the bottom half showing the background, so one character
carries two stacked pixels. Terminal cells are about twice as tall as they
are wide, which means those two pixels come out very close to square.

The interesting part is how the escape codes get built. The obvious loop —
an f-string per cell — spends its whole budget in the interpreter: a modest
120x40 grid is 4800 cells, and at 20fps that is a six-figure rate of string
formatting. Instead every cell is written as a *fixed-width* template with
zero-padded colour components, which makes the output one flat byte array
that numpy can fill with a handful of vectorised assignments. Rendering a
frame becomes about a dozen array writes regardless of grid size.
"""

from __future__ import annotations

import atexit
import os
import select
import shutil
import signal
import sys
import termios
import tty

import numpy as np

from .. import imageops as io

ESC = b"\x1b"
ALT_SCREEN_ON = ESC + b"[?1049h"
ALT_SCREEN_OFF = ESC + b"[?1049l"
CURSOR_HIDE = ESC + b"[?25l"
CURSOR_SHOW = ESC + b"[?25h"
CURSOR_HOME = ESC + b"[H"
CLEAR = ESC + b"[2J"
RESET = ESC + b"[0m"

# Three ASCII digits per colour component keeps every cell the same length.
_DIGITS = np.frombuffer(
    b"".join(f"{i:03d}".encode() for i in range(256)), dtype=np.uint8
).reshape(256, 3)


class _CellFormat:
    """A fixed-width per-cell byte template and where the colours go in it."""

    def __init__(self, template: bytes, fg_at: tuple[int, ...], bg_at: tuple[int, ...]):
        self.template = np.frombuffer(template, dtype=np.uint8)
        self.width = len(template)
        self.fg_at = fg_at
        self.bg_at = bg_at


# \x1b[38;2;RRR;GGG;BBB;48;2;RRR;GGG;BBBm▀
TRUECOLOR = _CellFormat(
    b"\x1b[38;2;000;000;000;48;2;000;000;000m\xe2\x96\x80",
    fg_at=(7, 11, 15),
    bg_at=(24, 28, 32),
)
# \x1b[38;5;NNN;48;5;NNNm▀  — one index each, so only the red slot is used.
COLOR256 = _CellFormat(
    b"\x1b[38;5;000;48;5;000m\xe2\x96\x80",
    fg_at=(7,),
    bg_at=(16,),
)


def _xterm256(rgb: np.ndarray) -> np.ndarray:
    """Quantise (..., 3) uint8 to xterm-256 indices.

    Greys get the 24-step ramp at 232..255, which is much finer than the
    colour cube's diagonal; everything else lands in the 6x6x6 cube at 16.
    """
    r, g, b = rgb[..., 0].astype(np.int16), rgb[..., 1].astype(np.int16), rgb[..., 2].astype(np.int16)
    cube = 16 + 36 * (r * 5 // 255) + 6 * (g * 5 // 255) + (b * 5 // 255)
    grey_level = (r + g + b) // 3
    grey = 232 + np.clip((grey_level - 8) * 24 // 238, 0, 23)
    is_grey = (np.abs(r - g) < 12) & (np.abs(g - b) < 12) & (np.abs(r - b) < 12)
    return np.where(is_grey, grey, cube).astype(np.uint8)


def supports_truecolor() -> bool:
    if os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return True
    term = os.environ.get("TERM", "")
    return "direct" in term or "24bit" in term


class TerminalDisplay:
    """Half-block video on stdout, with raw-mode key polling on stdin."""

    # One row is reserved at the bottom for the status line.
    STATUS_ROWS = 1

    def __init__(self, truecolor: bool | None = None, max_width: int = 0) -> None:
        self.running = True
        self.fmt = TRUECOLOR if (supports_truecolor() if truecolor is None else truecolor) else COLOR256
        self.max_width = max_width
        self._status = ""
        self._out = sys.stdout.buffer
        self._grid = (0, 0)

        # Raw mode only makes sense on a real tty; piping output is still
        # useful (`camtoy live | head`) and must not blow up.
        self._tty = sys.stdin.isatty()
        self._saved: list | None = None
        self._closed = False
        if self._tty:
            self._saved = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())

        # This process now owns the alt screen, a hidden cursor and a terminal
        # in cbreak mode. Dying without undoing that leaves the user's shell
        # unusable, so cover the ways we might die: atexit handles a normal
        # return or an unhandled exception, and the signal handlers cover a
        # `kill` or a closed terminal window.
        atexit.register(self.close)
        self._prev_signals: dict[int, object] = {}
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                self._prev_signals[sig] = signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                pass                            # not the main thread; nothing to do

        self._write(ALT_SCREEN_ON + CURSOR_HIDE + CLEAR)

    def _on_signal(self, signum, frame) -> None:
        # Raise rather than tear down and exit here: unwinding lets every
        # `finally` above us run, which is how a caller gets to close its own
        # resources — an unsaved light painting, say — before we go.
        raise SystemExit(128 + signum)

    # -- geometry ----------------------------------------------------------

    def _cells(self) -> tuple[int, int]:
        size = shutil.get_terminal_size(fallback=(80, 24))
        cols = max(size.columns - 1, 8)         # -1 dodges the auto-wrap column
        if self.max_width:
            cols = min(cols, self.max_width)
        rows = max(size.lines - self.STATUS_ROWS, 4)
        return cols, rows

    def target_size(self) -> tuple[int, int]:
        cols, rows = self._cells()
        return cols, rows * 2                   # two pixels stacked per cell

    # -- output ------------------------------------------------------------

    def _write(self, data: bytes) -> None:
        try:
            self._out.write(data)
            self._out.flush()
        except (BrokenPipeError, ValueError):
            self.running = False

    def show(self, rgb: np.ndarray) -> None:
        cols, rows = self._cells()
        want_h = rows * 2
        if rgb.shape[:2] != (want_h, cols):
            rgb = io.resize(rgb, cols, want_h)

        top, bottom = rgb[0::2], rgb[1::2]
        if self.fmt is COLOR256:
            top, bottom = _xterm256(top)[..., None], _xterm256(bottom)[..., None]

        fmt = self.fmt
        # One row of cells plus a CRLF, laid out as raw bytes.
        line_end = b"\r\n"
        buf = np.empty((rows, cols * fmt.width + len(line_end)), dtype=np.uint8)
        cells = buf[:, : cols * fmt.width].reshape(rows, cols, fmt.width)
        cells[:] = fmt.template
        for slot, offset in enumerate(fmt.fg_at):
            cells[:, :, offset:offset + 3] = _DIGITS[top[:, :, slot]]
        for slot, offset in enumerate(fmt.bg_at):
            cells[:, :, offset:offset + 3] = _DIGITS[bottom[:, :, slot]]
        buf[:, cols * fmt.width:] = np.frombuffer(line_end, dtype=np.uint8)

        self._write(CURSOR_HOME + buf.tobytes() + RESET + self._status_line(cols))

    def _status_line(self, cols: int) -> bytes:
        text = self._status[: cols - 1]
        return ESC + b"[K" + text.encode("utf-8", "replace")

    def status(self, text: str) -> None:
        self._status = text

    # -- input -------------------------------------------------------------

    _ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}

    def keys(self) -> list[str]:
        if not self._tty:
            return []
        pending = []
        while select.select([sys.stdin], [], [], 0)[0]:
            ch = sys.stdin.read(1)
            if not ch:
                break
            pending.append(ch)
        return self._decode(pending)

    def _decode(self, chars: list[str]) -> list[str]:
        out, i = [], 0
        while i < len(chars):
            c = chars[i]
            if c == "\x1b":
                # CSI arrow keys arrive as ESC [ A..D in one read; a lone ESC
                # (nothing following) is the user asking to quit.
                if i + 2 < len(chars) and chars[i + 1] == "[" and chars[i + 2] in self._ARROWS:
                    out.append(self._ARROWS[chars[i + 2]])
                    i += 3
                    continue
                out.append("esc")
            elif c in "\r\n":
                out.append("enter")
            elif c == " ":
                out.append("space")
            elif c == "\x03":
                out.append("esc")               # ctrl-C: cbreak means we see it ourselves
            else:
                out.append(c.lower())
            i += 1
        return out

    # -- teardown ----------------------------------------------------------

    def close(self) -> None:
        self.running = False
        if self._closed:                        # atexit and an explicit close both fire
            return
        self._closed = True
        for sig, handler in self._prev_signals.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, TypeError):
                pass
        if self._saved is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)
            self._saved = None
        self._write(RESET + CURSOR_SHOW + ALT_SCREEN_OFF)

    def __enter__(self) -> "TerminalDisplay":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
