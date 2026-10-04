"""evdev, read straight from /dev/input.

A keyboard or a mouse is the one class of device where the descriptor tells
you almost nothing interesting — you already know a HID boot keyboard sends
keys. What you actually want to see is *this* keyboard sending *this* key, and
the kernel has already done the decoding: it publishes `input_event` structs
on /dev/input/eventN.

So there is no library here and none needed. The struct is 24 bytes on a
64-bit kernel (two longs of timeval, then type/code/value) and `struct` gets
the width right on its own, which is why armhf and arm64 both work.

Reading a keyboard's event node means reading every keystroke typed into the
machine, passwords included. That is why the target is mandatory — there is no
"watch everything" mode — and why the reader prints what it is about to do.
"""

from __future__ import annotations

import os
import select
import struct
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "EVENT_SIZE",
    "EventNode",
    "InputEvent",
    "describe",
    "event_nodes",
    "parse_event",
    "resolve",
    "watch",
]

#: timeval (two longs) + __u16 type + __u16 code + __s32 value.
_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(_FORMAT)

EV_SYN, EV_KEY, EV_REL, EV_ABS, EV_MSC, EV_SW = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
EV_LED, EV_SND, EV_REP = 0x11, 0x12, 0x14

TYPE_NAMES = {
    EV_SYN: "SYN", EV_KEY: "KEY", EV_REL: "REL", EV_ABS: "ABS", EV_MSC: "MSC",
    EV_SW: "SW", EV_LED: "LED", EV_SND: "SND", EV_REP: "REP",
}

_ROWS = [
    (1, "ESC 1 2 3 4 5 6 7 8 9 0 MINUS EQUAL BACKSPACE TAB "
        "Q W E R T Y U I O P LEFTBRACE RIGHTBRACE ENTER LEFTCTRL "
        "A S D F G H J K L SEMICOLON APOSTROPHE GRAVE LEFTSHIFT BACKSLASH "
        "Z X C V B N M COMMA DOT SLASH RIGHTSHIFT KPASTERISK LEFTALT SPACE "
        "CAPSLOCK F1 F2 F3 F4 F5 F6 F7 F8 F9 F10 NUMLOCK SCROLLLOCK "
        "KP7 KP8 KP9 KPMINUS KP4 KP5 KP6 KPPLUS KP1 KP2 KP3 KP0 KPDOT"),
    (87, "F11 F12"),
    (96, "KPENTER RIGHTCTRL KPSLASH SYSRQ RIGHTALT LINEFEED HOME UP PAGEUP "
         "LEFT RIGHT END DOWN PAGEDOWN INSERT DELETE MACRO MUTE VOLUMEDOWN "
         "VOLUMEUP POWER KPEQUAL KPPLUSMINUS PAUSE"),
    (125, "LEFTMETA RIGHTMETA COMPOSE STOP AGAIN PROPS UNDO FRONT COPY OPEN "
          "PASTE FIND CUT HELP MENU CALC SETUP SLEEP WAKEUP"),
]

KEY_NAMES: dict[int, str] = {}
for _base, _names in _ROWS:
    for _i, _name in enumerate(_names.split()):
        KEY_NAMES[_base + _i] = f"KEY_{_name}"

KEY_NAMES.update({
    0x110: "BTN_LEFT", 0x111: "BTN_RIGHT", 0x112: "BTN_MIDDLE", 0x113: "BTN_SIDE",
    0x114: "BTN_EXTRA", 0x115: "BTN_FORWARD", 0x116: "BTN_BACK", 0x117: "BTN_TASK",
    0x120: "BTN_TRIGGER", 0x121: "BTN_THUMB", 0x130: "BTN_SOUTH (A)",
    0x131: "BTN_EAST (B)", 0x133: "BTN_NORTH (X)", 0x134: "BTN_WEST (Y)",
    0x136: "BTN_TL", 0x137: "BTN_TR", 0x138: "BTN_TL2", 0x139: "BTN_TR2",
    0x13a: "BTN_SELECT", 0x13b: "BTN_START", 0x13c: "BTN_MODE",
    0x13d: "BTN_THUMBL", 0x13e: "BTN_THUMBR", 0x14a: "BTN_TOUCH",
})

REL_NAMES = {
    0x00: "REL_X", 0x01: "REL_Y", 0x02: "REL_Z", 0x03: "REL_RX", 0x04: "REL_RY",
    0x05: "REL_RZ", 0x06: "REL_HWHEEL", 0x07: "REL_DIAL", 0x08: "REL_WHEEL",
    0x09: "REL_MISC", 0x0b: "REL_WHEEL_HI_RES", 0x0c: "REL_HWHEEL_HI_RES",
}

ABS_NAMES = {
    0x00: "ABS_X", 0x01: "ABS_Y", 0x02: "ABS_Z", 0x03: "ABS_RX", 0x04: "ABS_RY",
    0x05: "ABS_RZ", 0x06: "ABS_THROTTLE", 0x07: "ABS_RUDDER", 0x08: "ABS_WHEEL",
    0x09: "ABS_GAS", 0x0a: "ABS_BRAKE", 0x10: "ABS_HAT0X", 0x11: "ABS_HAT0Y",
    0x18: "ABS_PRESSURE", 0x28: "ABS_MISC", 0x2f: "ABS_MT_SLOT",
    0x35: "ABS_MT_POSITION_X", 0x36: "ABS_MT_POSITION_Y", 0x39: "ABS_MT_TRACKING_ID",
}

MSC_NAMES = {0x00: "MSC_SERIAL", 0x01: "MSC_PULSELED", 0x02: "MSC_GESTURE",
             0x03: "MSC_RAW", 0x04: "MSC_SCAN", 0x05: "MSC_TIMESTAMP"}

_KEY_VALUE = {0: "release", 1: "press", 2: "repeat"}


@dataclass(slots=True)
class InputEvent:
    when: float
    type: int
    code: int
    value: int

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, f"type 0x{self.type:02x}")

    @property
    def code_name(self) -> str:
        if self.type == EV_KEY:
            return KEY_NAMES.get(self.code, f"code {self.code} (0x{self.code:03x})")
        if self.type == EV_REL:
            return REL_NAMES.get(self.code, f"code {self.code}")
        if self.type == EV_ABS:
            return ABS_NAMES.get(self.code, f"code {self.code}")
        if self.type == EV_MSC:
            return MSC_NAMES.get(self.code, f"code {self.code}")
        if self.type == EV_SYN:
            return "SYN_REPORT" if self.code == 0 else f"code {self.code}"
        return f"code {self.code}"

    @property
    def is_noise(self) -> bool:
        """SYN_REPORT and MSC_SCAN frame every real event; they drown a log."""
        return self.type == EV_SYN or (self.type == EV_MSC and self.code == 0x04)

    def as_dict(self) -> dict:
        return {
            "ts": self.when,
            "type": self.type_name,
            "code": self.code_name,
            "value": self.value,
            "text": describe(self),
        }


def parse_event(blob: bytes) -> InputEvent:
    """One 24-byte (or 16-byte, on a 32-bit kernel) input_event."""
    if len(blob) != EVENT_SIZE:
        raise ValueError(f"input_event is {EVENT_SIZE} bytes, got {len(blob)}")
    sec, usec, etype, code, value = struct.unpack(_FORMAT, blob)
    return InputEvent(sec + usec / 1e6, etype, code, value)


def describe(event: InputEvent) -> str:
    """A line a human can read: `KEY_A press`, `REL_X -3`."""
    if event.type == EV_KEY:
        return f"{event.code_name} {_KEY_VALUE.get(event.value, event.value)}"
    if event.type in (EV_REL, EV_ABS):
        return f"{event.code_name} {event.value:+d}" if event.type == EV_REL \
            else f"{event.code_name} {event.value}"
    if event.type == EV_MSC and event.code == 0x04:
        return f"MSC_SCAN 0x{event.value:x}"
    if event.type == EV_SYN:
        return event.code_name
    return f"{event.type_name}/{event.code_name} {event.value}"


# --------------------------------------------------------------------------
# finding the nodes
# --------------------------------------------------------------------------

@dataclass(slots=True)
class EventNode:
    path: str            # /dev/input/event5
    name: str            # "YICHIP 2.4G Receiver Keyboard"
    capabilities: list[str] = None      # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.capabilities is None:
            self.capabilities = []

    @property
    def readable(self) -> bool:
        return os.access(self.path, os.R_OK)

    def as_dict(self) -> dict:
        return {"path": self.path, "name": self.name,
                "capabilities": self.capabilities, "readable": self.readable}


def event_nodes(sysfs_input: Path) -> list[EventNode]:
    """The eventN nodes belonging to one /sys/class/input/inputN directory."""
    out = []
    name = _read(sysfs_input / "name") or sysfs_input.name
    caps = _capabilities(sysfs_input)
    for child in sorted(sysfs_input.glob("event*")):
        out.append(EventNode(f"/dev/input/{child.name}", name, caps))
    return out


def resolve(target: str) -> list[EventNode]:
    """Accept whatever the user has in front of them.

    A USB address (`1-2.2`), an input directory (`input29`), an event node
    (`event5` or `/dev/input/event5`), or part of a device name. A composite
    receiver is genuinely several event nodes, so this returns a list.
    """
    want = target.strip()
    root = Path("/sys/class/input")
    if not root.is_dir():
        return []

    bare = want.removeprefix("/dev/input/")
    if bare.startswith("event") and (root / bare).is_dir():
        parent = (root / bare).resolve().parent
        return [n for n in event_nodes(parent) if n.path.endswith(f"/{bare}")]

    if bare.startswith("input") and (root / bare).is_dir():
        return event_nodes(root / bare)

    inputs = sorted(p for p in root.glob("input*") if p.is_dir())

    # A USB address: the input directory lives under .../usb1/1-2/1-2.2/...
    hits: list[EventNode] = []
    for entry in inputs:
        try:
            real = str(entry.resolve())
        except OSError:
            continue
        if f"/{want}/" in real or real.endswith(f"/{want}"):
            hits.extend(event_nodes(entry))
    if hits:
        return hits

    lowered = want.lower()
    for entry in inputs:
        if lowered and lowered in (_read(entry / "name") or "").lower():
            hits.extend(event_nodes(entry))
    return hits


def _capabilities(entry: Path) -> list[str]:
    """Which event types this device emits, from the capability bitmasks."""
    out = []
    for attr, label in (("key", "keys"), ("rel", "relative"), ("abs", "absolute"),
                        ("led", "leds"), ("sw", "switches")):
        raw = _read(entry / "capabilities" / attr)
        if raw and any(int(chunk, 16) for chunk in raw.split() if chunk):
            out.append(label)
    return out


def watch(nodes: list[EventNode], duration: float = 0.0, quiet: bool = True):
    """Yield (node, event) as they arrive. Blocks until Ctrl-C or `duration`.

    `quiet` drops SYN_REPORT and MSC_SCAN, which frame every real event and
    otherwise triple the log for nothing.
    """
    handles: dict[int, tuple[EventNode, int]] = {}
    try:
        for node in nodes:
            try:
                fd = os.open(node.path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            handles[fd] = (node, fd)
        if not handles:
            return
        deadline = time.time() + duration if duration else None
        while True:
            if deadline and time.time() >= deadline:
                return
            budget = 0.25 if deadline is None else min(0.25, max(0.0, deadline - time.time()))
            ready, _, _ = select.select(list(handles), [], [], budget)
            for fd in ready:
                node, _ = handles[fd]
                while True:
                    try:
                        blob = os.read(fd, EVENT_SIZE)
                    except BlockingIOError:
                        break
                    except OSError:
                        return
                    if len(blob) != EVENT_SIZE:
                        break
                    event = parse_event(blob)
                    if quiet and event.is_noise:
                        continue
                    yield node, event
    finally:
        for fd in handles:
            try:
                os.close(fd)
            except OSError:
                pass


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""
