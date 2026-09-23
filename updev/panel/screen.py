"""The ST7735S as updev's front panel: a 128x160 canvas and one call to push it.

This is updev's third front end. The CLI renders to a terminal, `updev gui`
renders to Tk, and this renders to a physical 1.8" panel wired to SPI0 — same
scan underneath, three surfaces on top.

Two things about the wiring are worth stating plainly, because they shape
everything above:

  * **The panel lives on a bus updev reports on.** ST7735S is a write-only SPI
    slave on SPI0 CE0, so while the panel is drawing, SPI0 is busy — by the
    panel's own doing. A "SPI0 idle" reading taken *on* the panel would be
    measuring the observer. Keep the panel on CE0 and anything you actually
    want to watch on CE1, or move the panel to SPI1.
  * **luma's ST7735 wants landscape, then rotates.** The driver only accepts
    (160,128), (160,80) and (128,128), so a portrait 128x160 module is opened
    as 160x128 with `rotate=1`. `open_panel()` asserts the result really did
    come out 128x160 rather than trusting the arithmetic.

Nothing here talks to the scan model — it takes coordinates and colours, the
same way `ui/render.py` takes a ScanResult and knows nothing about hardware.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..core.model import Status

__all__ = [
    "WIDTH", "HEIGHT", "GRID_W", "GRID_H", "PanelUnavailable", "Screen",
    "open_panel", "STATUS_RGB", "font",
]

# --------------------------------------------------------------------------
# geometry and default wiring
# --------------------------------------------------------------------------

# These are the numbers st7735s_drawpad.py runs on, kept identical on purpose:
# the same panel, on the same wires, driven by two programs. If the wiring ever
# changes, it changes in both places or neither.

GRID_W, GRID_H = 128, 160          # ST7735S 해상도 (128x160 모델 기준)
WIDTH, HEIGHT = GRID_W, GRID_H     # the names the rest of updev uses

USE_PHYSICAL_DISPLAY = True        # 물리 디스플레이 없이 테스트하려면 False
SPI_PORT = 0
SPI_DEVICE = 0

#: BCM pins. The two that must be right are DC and RST; CS comes from the
#: spidev node, and BLK is usually strapped to 3V3 and not driven at all.
PIN_DC = 24
PIN_RST = 25
PIN_BACKLIGHT: int | None = None

#: luma's default, and what the drawpad has been running at. A boot screen is
#: ~14 full frames, so 8MHz costs about half a second of SPI in total — not
#: worth trading for the margin that jumper wires need.
SPI_HZ = 8_000_000

# --------------------------------------------------------------------------
# palette — the terminal styles in ui/render.py, as RGB
# --------------------------------------------------------------------------

BLACK = (0, 0, 0)
FG = (232, 232, 232)
DIM = (112, 112, 112)
GRID = (44, 44, 48)
BAND = (18, 22, 28)
ACCENT = (0, 224, 150)
WARN = (255, 186, 0)
BAD = (255, 72, 72)

STATUS_RGB: dict[Status, tuple[int, int, int]] = {
    Status.ONLINE: (0, 224, 120),
    Status.IDLE: (128, 128, 128),
    Status.DEGRADED: WARN,
    Status.DISABLED: (88, 148, 255),
    Status.ABSENT: (208, 96, 208),
    Status.ERROR: BAD,
    Status.UNKNOWN: (96, 96, 96),
}

# --------------------------------------------------------------------------
# fonts
# --------------------------------------------------------------------------

_LATIN = ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono%s.ttf", ("", "-Bold"))
_HANGUL = ("/usr/share/fonts/truetype/nanum/NanumGothicCoding%s.ttf", ("", "Bold"))


@lru_cache(maxsize=32)
def font(size: int = 9, bold: bool = False, hangul: bool = False):
    """A monospace face at `size`, or PIL's builtin if the system has neither.

    Pillow does no font fallback, so the caller has to pick the file that can
    actually draw the string — see `Screen.text`, which sniffs for non-Latin
    characters and comes back here.
    """
    template, suffixes = _HANGUL if hangul else _LATIN
    path = Path(template % suffixes[1 if bold else 0])
    if path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _needs_hangul(text: str) -> bool:
    return any(ord(ch) > 0x2FFF for ch in text)


# --------------------------------------------------------------------------
# the canvas
# --------------------------------------------------------------------------

class PanelUnavailable(RuntimeError):
    """No display to draw on, with the reason a person can act on."""


class Screen:
    """A PIL canvas the size of the panel, plus `flush()` to send it.

    Every draw is local and cheap; `flush()` is the only thing that touches
    SPI, and it ships the whole 40KB frame. Draw a complete screen, flush
    once — a per-pixel flush would spend the entire frame budget on overhead.

    A Screen with `device=None` is headless: all the drawing still happens, so
    layout code can be tested and previewed with `capture=` on a machine with
    no panel attached.
    """

    def __init__(self, device=None, *, size: tuple[int, int] = (WIDTH, HEIGHT),
                 capture: Path | None = None) -> None:
        self.device = device
        self.width, self.height = size
        self.capture = Path(capture) if capture else None
        self.image = Image.new("RGB", size, BLACK)
        self.draw = ImageDraw.Draw(self.image)
        self.frames = 0
        if self.capture:
            self.capture.mkdir(parents=True, exist_ok=True)

    @property
    def live(self) -> bool:
        return self.device is not None

    # -- drawing -----------------------------------------------------------

    def clear(self, colour: tuple[int, int, int] = BLACK) -> None:
        self.draw.rectangle((0, 0, self.width, self.height), fill=colour)

    def fill(self, box: tuple[int, int, int, int], colour: tuple[int, int, int]) -> None:
        self.draw.rectangle(box, fill=colour)

    def rule(self, y: int, colour: tuple[int, int, int] = GRID,
             x0: int = 0, x1: int | None = None) -> None:
        self.draw.line((x0, y, self.width if x1 is None else x1, y), fill=colour)

    def text(self, x: int, y: int, text: str, colour: tuple[int, int, int] = FG,
             size: int = 9, bold: bool = False, right: bool = False) -> int:
        """Draw one line; return the y a following line should use.

        `right` treats `x` as the right edge instead of the left, which is what
        every value column on a 128px-wide panel wants.
        """
        face = font(size, bold, hangul=_needs_hangul(text))
        if right:
            x -= int(self.draw.textlength(text, font=face))
        self.draw.text((x, y), text, font=face, fill=colour)
        return y + size + 3

    def dot(self, x: int, y: int, colour: tuple[int, int, int],
            filled: bool = True, r: int = 2) -> None:
        """A status dot, drawn rather than typed — ● and ○ are unreliable at 9px."""
        box = (x - r, y - r, x + r, y + r)
        if filled:
            self.draw.ellipse(box, fill=colour)
        else:
            self.draw.ellipse(box, outline=colour)

    def bar(self, x: int, y: int, w: int, h: int, fraction: float,
            colour: tuple[int, int, int] = ACCENT,
            back: tuple[int, int, int] = GRID) -> None:
        self.draw.rectangle((x, y, x + w, y + h), fill=back)
        filled = max(0, min(w, int(w * fraction)))
        if filled:
            self.draw.rectangle((x, y, x + filled, y + h), fill=colour)

    def truncate(self, text: str, width: int, size: int = 9, bold: bool = False) -> str:
        """Clip to `width` pixels, ellipsis included — panel columns are narrow."""
        face = font(size, bold, hangul=_needs_hangul(text))
        if self.draw.textlength(text, font=face) <= width:
            return text
        while text and self.draw.textlength(text + "…", font=face) > width:
            text = text[:-1]
        return text + "…"

    # -- the wire ----------------------------------------------------------

    def flush(self) -> None:
        """Push the canvas. The only method that costs milliseconds."""
        if self.capture:
            self.image.save(self.capture / f"frame_{self.frames:03d}.png")
        self.frames += 1
        if self.device is not None:
            self.device.display(self.image)

    def close(self, blank: bool = False) -> None:
        """Release the panel. By default the last frame *stays on the glass*.

        That default is the whole point of a boot screen: the process exits,
        the picture remains. Getting it takes more than luma's `persist` flag,
        which only suppresses the DISPLAYOFF command:

        `luma.core.interface.serial.spi.cleanup()` also hands DC and RST back
        to the GPIO library, which sets them to inputs. There is no pull-up on
        RES on these modules, so a released RES floats, drifts, and resets the
        controller — the picture survives the exit and then vanishes a few
        seconds later. luma registers that cleanup as an atexit hook of its
        own, so it is not enough to simply not call it.

        So the persisting path never releases the pins. They stay outputs with
        RES held high, which is what keeps the image up; the kernel closes the
        spidev handle at exit, and the next run re-initialises the panel from
        scratch anyway.
        """
        if self.device is None:
            return
        if blank:
            self.device.persist = False
            self.device.cleanup()          # DISPLAYOFF, clear, release the pins
        else:
            self.device.persist = True
            self.device.cleanup = lambda: None      # defuse luma's atexit hook
        self.device = None


# --------------------------------------------------------------------------
# opening the real thing
# --------------------------------------------------------------------------

def open_panel(*, port: int = SPI_PORT, cs: int = SPI_DEVICE, dc: int = PIN_DC,
               rst: int = PIN_RST, backlight: int | None = PIN_BACKLIGHT,
               speed_hz: int = SPI_HZ, bgr: bool = False,
               h_offset: int = 0, v_offset: int = 0,
               capture: Path | None = None, required: bool = True) -> Screen:
    """Open the ST7735S on SPI, or explain why not.

    With `required=False` a missing panel is not an error: you get a headless
    Screen that draws into memory, which is what `--capture` and the tests use.
    Setting `USE_PHYSICAL_DISPLAY = False` does the same thing globally, for
    working on layout on a board with nothing wired to it.
    """
    if not USE_PHYSICAL_DISPLAY:
        # An explicit "no hardware today" — degrade even when the caller asked
        # for a real panel, because that is the whole point of the switch.
        return Screen(None, capture=capture)

    node = Path(f"/dev/spidev{port}.{cs}")
    try:
        if not node.exists():
            raise PanelUnavailable(
                f"{node} is missing — enable SPI (dtparam=spi=on) and reboot"
            )
        if not os.access(node, os.W_OK):
            raise PanelUnavailable(f"{node} is not writable by this user (group 'spi')")

        from luma.core.interface.serial import spi
        from luma.lcd.device import st7735

        serial = spi(port=port, device=cs, bus_speed_hz=speed_hz,
                     gpio_DC=dc, gpio_RST=rst, gpio_LIGHT=backlight)
        # Landscape then rotate: see the module docstring.
        device = st7735(serial, width=HEIGHT, height=WIDTH, rotate=1, bgr=bgr,
                        h_offset=h_offset, v_offset=v_offset)
        if (device.width, device.height) != (WIDTH, HEIGHT):
            raise PanelUnavailable(
                f"panel came up {device.width}x{device.height}, expected {WIDTH}x{HEIGHT}"
            )
        device.persist = True          # keep the last frame after we exit
        if backlight is not None:
            device.backlight(True)
        return Screen(device, capture=capture)
    except PanelUnavailable:
        if required:
            raise
    except ImportError as e:
        if required:
            raise PanelUnavailable(
                f"{e.name} is missing — pip install luma.lcd (or apt install python3-luma.lcd)"
            ) from e
    except Exception as e:                                   # pragma: no cover - hardware
        if required:
            raise PanelUnavailable(f"{type(e).__name__}: {e}") from e
    return Screen(None, capture=capture)
