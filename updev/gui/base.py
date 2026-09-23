"""Shared furniture for the device editors.

Every editor is a panel that owns one device, so the shell can swap them in
and out without knowing what any of them do. Three rules they all follow:

  * **Hardware is opened late and closed on the way out.** `close()` always
    runs, so an spidev handle or an open block device never survives a panel
    switch.
  * **Nothing is written without a dialog that shows the exact bytes.** The
    terminal side of updev requires `--yes` for anything that drives a bus;
    this is the same rule wearing a window.
  * **A panel that cannot do its job says why**, in place, instead of being
    missing from the list.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

from ..core.model import Device

__all__ = [
    "COLORS",
    "Editor",
    "HexView",
    "block_node",
    "confirm_write",
    "mono",
    "notice",
]

#: Terminal palette, so the window and the CLI look like one program.
COLORS = {
    "bg": "#12131a",
    "panel": "#1a1c25",
    "raised": "#232634",
    "line": "#2e3242",
    "fg": "#d6d9e3",
    "dim": "#7d8496",
    "accent": "#c678dd",        # bright_magenta — the zone's colour
    "ok": "#89d185",
    "warn": "#e5c07b",
    "danger": "#e06c75",
    "cyan": "#56b6c2",
    "select": "#2f3446",
}

_MONO_CANDIDATES = ("DejaVu Sans Mono", "Liberation Mono", "Courier New", "TkFixedFont")


def block_node(device: Device) -> str:
    """The /dev/sdX behind a device, which is *not* `device.node` for USB.

    A USB device's node is its sysfs directory — the block device it owns is
    one level down, discovered through the same `device_nodes()` the path view
    uses. Reading `device.node` directly gets you a directory and an EISDIR,
    which is a confusing way to learn this.
    """
    if device.node.startswith("/dev/"):
        return device.node
    if device.address:
        from ..usbrole import device_nodes

        try:
            blocks = device_nodes(device.address).get("block") or []
        except OSError:
            return ""
        if blocks:
            return blocks[0]
    return ""


def mono(size: int = 11, weight: str = "normal") -> tkfont.Font:
    """A monospace font that exists on this machine.

    Hex grids and ASCII art both stop making sense in a proportional face, and
    Tk silently substitutes rather than failing, so the family is checked.
    """
    families = set(tkfont.families())
    for family in _MONO_CANDIDATES:
        if family in families or family == "TkFixedFont":
            return tkfont.Font(family=family, size=size, weight=weight)
    return tkfont.Font(size=size, weight=weight)


def notice(parent: tk.Widget, text: str, colour: str = "dim") -> tk.Label:
    """A one-line explanation, styled like the CLI's dim italics."""
    label = tk.Label(parent, text=text, bg=COLORS["panel"], fg=COLORS[colour],
                     font=mono(10), justify="left", anchor="w", wraplength=760)
    label.pack(fill="x", padx=12, pady=(2, 6))
    return label


def confirm_write(parent, title: str, what: str, detail: str,
                  danger: str = "") -> bool:
    """The gate in front of every write. Shows the bytes, defaults to no.

    `danger` is for the writes that are not merely destructive but final —
    a MIFARE sector trailer with wrong access bits locks that sector forever,
    and no amount of retrying gets it back.
    """
    body = f"{what}\n\n{detail}"
    if danger:
        body += f"\n\n⚠ {danger}"
    body += "\n\n계속할까요?"
    return messagebox.askyesno(title, body, default="no", icon="warning",
                               parent=parent)


class Editor(tk.Frame):
    """One device, one panel.

    Subclasses set `TITLE`/`SUBTITLE`, implement `build()`, and override
    `close()` if they hold hardware open.
    """

    TITLE = "장치"
    SUBTITLE = ""
    #: True when this panel can change the device. Shown as a badge, so a
    #: read-only panel is visibly read-only before anything is clicked.
    WRITES = False

    def __init__(self, parent: tk.Widget, device: Device, app=None) -> None:
        super().__init__(parent, bg=COLORS["panel"])
        self.device = device
        self.app = app
        self._after_ids: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    def build(self) -> None:                     # pragma: no cover - UI
        raise NotImplementedError

    def close(self) -> None:
        """Release hardware and cancel timers. Always called on a swap."""
        for handle in self._after_ids:
            try:
                self.after_cancel(handle)
            except Exception:
                pass
        self._after_ids.clear()

    def every(self, ms: int, callback) -> None:
        """A repeating timer that stops itself when the panel goes away."""
        def tick():
            if not self.winfo_exists():
                return
            callback()
            self._after_ids.append(self.after(ms, tick))
        self._after_ids.append(self.after(ms, tick))

    # -- shared chrome -----------------------------------------------------

    def status(self, text: str, colour: str = "dim") -> None:
        if self.app is not None:
            self.app.status(text, colour)

    def header(self, text: str) -> tk.Label:
        label = tk.Label(self, text=text, bg=COLORS["panel"], fg=COLORS["fg"],
                         font=mono(11, "bold"), anchor="w")
        label.pack(fill="x", padx=12, pady=(10, 2))
        return label

    def button(self, parent: tk.Widget, text: str, command, danger: bool = False,
               **kwargs) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command,
            bg=COLORS["raised"], fg=COLORS["danger"] if danger else COLORS["fg"],
            activebackground=COLORS["select"], activeforeground=COLORS["fg"],
            font=mono(10), relief="flat", padx=10, pady=4,
            highlightthickness=1,
            highlightbackground=COLORS["danger"] if danger else COLORS["line"],
            **kwargs,
        )

    def row(self) -> tk.Frame:
        frame = tk.Frame(self, bg=COLORS["panel"])
        frame.pack(fill="x", padx=12, pady=4)
        return frame


class HexView(tk.Frame):
    """16 bytes to a line: offset, hex, ASCII — with optional editing.

    Editing is per byte-cell rather than free text, because a hex editor that
    lets you delete a character silently changes the length of what gets
    written, and for a 16-byte MIFARE block or a 512-byte sector the length is
    not yours to change.
    """

    def __init__(self, parent: tk.Widget, editable: bool = False,
                 on_change=None, width: int = 78, height: int = 18) -> None:
        super().__init__(parent, bg=COLORS["panel"])
        self.editable = editable
        self.on_change = on_change
        self.data = bytearray()
        self.base = 0

        self.text = tk.Text(
            self, bg=COLORS["bg"], fg=COLORS["fg"], font=mono(10),
            insertbackground=COLORS["accent"], relief="flat", height=height,
            width=width, wrap="none", padx=10, pady=8,
            selectbackground=COLORS["select"],
        )
        scroll = tk.Scrollbar(self, command=self.text.yview,
                              bg=COLORS["panel"], troughcolor=COLORS["bg"],
                              relief="flat", highlightthickness=0, width=10)
        self.text.configure(yscrollcommand=scroll.set, state="disabled")
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.text.tag_configure("offset", foreground=COLORS["dim"])
        self.text.tag_configure("ascii", foreground=COLORS["cyan"])
        self.text.tag_configure("changed", foreground=COLORS["warn"])
        self.text.tag_configure("note", foreground=COLORS["dim"])
        self._dirty: set[int] = set()

        if editable:
            self.text.bind("<Double-Button-1>", self._edit_at_cursor)

    # -- content -----------------------------------------------------------

    def show(self, data: bytes, base: int = 0, notes: dict[int, str] | None = None) -> None:
        self.data = bytearray(data)
        self.base = base
        self.notes = notes or {}
        self._dirty.clear()
        self.render()

    def render(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for offset in range(0, len(self.data), 16):
            chunk = self.data[offset:offset + 16]
            self.text.insert("end", f"{self.base + offset:08X}  ", "offset")
            for i, byte in enumerate(chunk):
                tag = "changed" if (offset + i) in self._dirty else ""
                self.text.insert("end", f"{byte:02X} ", tag)
            self.text.insert("end", "   " * (16 - len(chunk)))
            ascii_run = "".join(chr(b) if 32 <= b < 127 else "·" for b in chunk)
            self.text.insert("end", f" |{ascii_run}|", "ascii")
            note = self.notes.get(self.base + offset)
            if note:
                self.text.insert("end", f"   {note}", "note")
            self.text.insert("end", "\n")
        self.text.configure(state="disabled")

    @property
    def dirty(self) -> bool:
        return bool(self._dirty)

    def mark_clean(self) -> None:
        self._dirty.clear()
        self.render()

    # -- editing -----------------------------------------------------------

    def _edit_at_cursor(self, event) -> str:
        """Double-click a hex pair to change that one byte."""
        index = self.text.index(f"@{event.x},{event.y}")
        line, column = (int(part) for part in index.split("."))
        offset = self._offset_for(line, column)
        if offset is None or offset >= len(self.data):
            return "break"
        current = self.data[offset]
        answer = _ask_byte(self, self.base + offset, current)
        if answer is None or answer == current:
            return "break"
        self.data[offset] = answer
        self._dirty.add(offset)
        self.render()
        if self.on_change:
            self.on_change(offset, answer)
        return "break"

    @staticmethod
    def _offset_for(line: int, column: int) -> int | None:
        """Column maths for `00000000  DE AD ...` — 10 of prefix, 3 per byte."""
        if column < 10:
            return None
        index = (column - 10) // 3
        if index > 15:
            return None
        return (line - 1) * 16 + index


def _ask_byte(parent: tk.Widget, offset: int, current: int) -> int | None:
    """A one-byte prompt that refuses anything that isn't one byte."""
    from tkinter import simpledialog

    answer = simpledialog.askstring(
        "바이트 편집",
        f"offset 0x{offset:04X}\n현재값 0x{current:02X} ({current})\n\n"
        f"새 값 (hex, 00-FF):",
        initialvalue=f"{current:02X}",
        parent=parent,
    )
    if answer is None:
        return None
    try:
        value = int(answer.strip(), 16)
    except ValueError:
        messagebox.showerror("바이트 편집", f"{answer!r} 는 hex가 아닙니다",
                             parent=parent)
        return None
    if not 0 <= value <= 0xFF:
        messagebox.showerror("바이트 편집", "한 바이트는 00-FF 입니다", parent=parent)
        return None
    return value
