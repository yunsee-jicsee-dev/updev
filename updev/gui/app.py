"""The shell: devices on the left, the editor for the selected one on the right.

The list is the same scan the CLI runs, grouped the same way, in the same
order — the window is a second front end onto one model, not a second program.

Which editor opens is decided by `editors_for()`, which asks `toolkit` what the
device is. A USB floppy drive really is two things at once (a FAT12 disk and a
block device), so it gets both panels and a row of tabs, rather than updev
picking one and being wrong half the time.
"""

from __future__ import annotations

import threading
import tkinter as tk

from ..core.model import Device, Kind, ScanResult
from ..core.registry import ProbeContext, build_scanner
from ..ui.render import KIND_ORDER, KIND_TITLE
from .base import COLORS, Editor, mono
from .floppy import FloppyEditor
from .panels import BlockEditor, EventEditor, InfoEditor, PinEditor, RegisterEditor
from .tag import TagEditor
from .usbpanels import (
    CameraPanel,
    DescriptorPanel,
    HubPanel,
    IdentityPanel,
    NetworkPanel,
    PathPanel,
    SerialPanel,
)

__all__ = ["DeviceGui", "editors_for"]


def editors_for(device: Device) -> list[type[Editor]]:
    """Which panels make sense for this device, best first.

    Deliberately generous: a device that has an inside gets the panel for that
    inside *and* the info panel, because "what is this" is a fair question to
    ask about anything.
    """
    panels: list[type[Editor]] = []

    if device.kind == Kind.NFC:
        panels.append(TagEditor)
    elif device.kind == Kind.USB:
        panels.extend(_usb_panels(device))
    elif device.kind == Kind.STORAGE and device.node:
        panels.append(BlockEditor)
    elif device.kind == Kind.I2C and _is_chip(device):
        panels.append(RegisterEditor)
    elif device.kind == Kind.GPIO:
        panels.append(PinEditor)

    panels.append(InfoEditor)
    return panels


def _usb_panels(device: Device) -> list[type[Editor]]:
    """Role-specific panels first, then the three that fit any USB device.

    Identity, descriptors and path apply to everything on the bus — a device
    updev cannot place is exactly the one whose descriptor you want to read —
    so they are always offered, after whatever the role earned.
    """
    from ..toolkit import recognize
    from ..usbclass import UsbClass
    from ..usbrole import UsbRole

    try:
        recognition = recognize(device)
    except OSError:
        return [IdentityPanel, DescriptorPanel]

    panels: list[type[Editor]] = []
    roles = recognition.roles.roles if recognition.roles else []
    storage = recognition.storage

    if storage is not None and storage.usb_class == UsbClass.FUSB:
        panels.append(FloppyEditor)
    if recognition.nodes.get("block"):
        panels.append(BlockEditor)
    if UsbRole.CAMERA in roles:
        panels.append(CameraPanel)
    if UsbRole.SERIAL in roles or recognition.nodes.get("tty"):
        panels.append(SerialPanel)
    if UsbRole.WIFI in roles or UsbRole.ETHERNET in roles:
        panels.append(NetworkPanel)
    if UsbRole.HUB in roles or "root-hub" in device.tags:
        panels.append(HubPanel)
    if any(r in roles for r in (UsbRole.KEYBOARD, UsbRole.MOUSE, UsbRole.HID,
                                UsbRole.GAMEPAD, UsbRole.TOUCHSCREEN)):
        panels.append(EventEditor)

    panels.extend((IdentityPanel, DescriptorPanel, PathPanel))
    return panels


def _is_chip(device: Device) -> bool:
    """A chip on a bus, rather than the bus itself: `i2c:1:0x3c` has three parts."""
    return len(device.uid.split(":")) >= 3


class DeviceGui:
    """One window. Owns the scan, the list and whichever editor is open."""

    def __init__(self, result: ScanResult, target: str = "", deep: bool = False) -> None:
        self.result = result
        self.target = target
        self.deep = deep
        self.rows: list[Device | None] = []
        self.editor: Editor | None = None
        self.panels: list[type[Editor]] = []

        self.root = tk.Tk()
        self.root.title("updev — 장치 에디터")
        self.root.configure(bg=COLORS["bg"])
        self.root.geometry("1180x760")
        self.root.minsize(920, 560)
        self._build()
        self._fill_list()
        self._select_target()

    # -- chrome ------------------------------------------------------------

    def _build(self) -> None:
        top = tk.Frame(self.root, bg=COLORS["bg"])
        top.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(top, text="updev", bg=COLORS["bg"], fg=COLORS["accent"],
                 font=mono(15, "bold")).pack(side="left")
        tk.Label(top, text="  장치를 고르면 그 장치의 에디터가 열립니다",
                 bg=COLORS["bg"], fg=COLORS["dim"], font=mono(10)).pack(side="left")
        tk.Button(top, text="다시 스캔", command=self.rescan, bg=COLORS["raised"],
                  fg=COLORS["fg"], activebackground=COLORS["select"],
                  activeforeground=COLORS["fg"], font=mono(10), relief="flat",
                  padx=10, pady=3, highlightthickness=1,
                  highlightbackground=COLORS["line"]).pack(side="right")

        body = tk.Frame(self.root, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        left = tk.Frame(body, bg=COLORS["bg"])
        left.pack(side="left", fill="y")
        self.listbox = tk.Listbox(
            left, bg=COLORS["panel"], fg=COLORS["fg"], font=mono(10), width=34,
            relief="flat", highlightthickness=1, highlightbackground=COLORS["line"],
            selectbackground=COLORS["select"], selectforeground=COLORS["fg"],
            activestyle="none", exportselection=False,
        )
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        right = tk.Frame(body, bg=COLORS["panel"], highlightthickness=1,
                         highlightbackground=COLORS["line"])
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        head = tk.Frame(right, bg=COLORS["panel"])
        head.pack(fill="x", padx=12, pady=(10, 0))
        self.title = tk.Label(head, text="장치를 고르세요", bg=COLORS["panel"],
                              fg=COLORS["fg"], font=mono(13, "bold"), anchor="w")
        self.title.pack(side="left")
        self.badge = tk.Label(head, text="", bg=COLORS["panel"], fg=COLORS["dim"],
                              font=mono(9))
        self.badge.pack(side="right")
        self.subtitle = tk.Label(right, text="", bg=COLORS["panel"], fg=COLORS["dim"],
                                 font=mono(9), anchor="w")
        self.subtitle.pack(fill="x", padx=12)

        self.tabs = tk.Frame(right, bg=COLORS["panel"])
        self.tabs.pack(fill="x", padx=12, pady=(6, 0))

        self.area = tk.Frame(right, bg=COLORS["panel"])
        self.area.pack(fill="both", expand=True)

        self.status_label = tk.Label(
            self.root, text="준비됨", bg=COLORS["bg"], fg=COLORS["dim"],
            font=mono(9), anchor="w", padx=16, pady=6)
        self.status_label.pack(fill="x")

    def status(self, text: str, colour: str = "dim") -> None:
        self.status_label.configure(text=text, fg=COLORS.get(colour, COLORS["dim"]))

    # -- device list -------------------------------------------------------

    def _fill_list(self) -> None:
        self.listbox.delete(0, "end")
        self.rows = []
        by_kind = self.result.by_kind()
        for kind in KIND_ORDER:
            devices = by_kind.get(kind)
            if not devices:
                continue
            self.listbox.insert("end", f"  {KIND_TITLE.get(kind, str(kind))}")
            self.listbox.itemconfig("end", foreground=COLORS["accent"])
            self.rows.append(None)
            for device in devices:
                text = f"    {_short(device.label, 28)}"
                self.listbox.insert("end", text)
                self.listbox.itemconfig(
                    "end", foreground=COLORS["fg"] if device.status == "online"
                    else COLORS["dim"])
                self.rows.append(device)
        self.status(f"{len(self.result.devices)} devices  ·  "
                    f"{self.result.duration:.1f}s")

    def _select_target(self) -> None:
        if not self.target:
            return
        matches = self.result.find(self.target)
        if not matches:
            self.status(f"{self.target!r} 에 맞는 장치가 없습니다", "warn")
            return
        wanted = matches[0]
        for index, device in enumerate(self.rows):
            if device is not None and device.uid == wanted.uid:
                self.listbox.selection_clear(0, "end")
                self.listbox.selection_set(index)
                self.listbox.see(index)
                self._open(device)
                return

    def _on_select(self, _event=None) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        device = self.rows[selection[0]]
        if device is None:                      # a kind header
            return
        self._open(device)

    # -- editors -----------------------------------------------------------

    def _open(self, device: Device) -> None:
        self.device = device
        self.panels = editors_for(device)
        self.title.configure(text=device.label)
        self.badge.configure(text=f"{device.kind}  ·  {device.address or device.uid}")
        for child in self.tabs.winfo_children():
            child.destroy()
        for index, panel in enumerate(self.panels):
            tk.Button(
                self.tabs, text=panel.TITLE, font=mono(9),
                command=lambda p=panel: self._mount(p),
                bg=COLORS["raised"] if index else COLORS["select"],
                fg=COLORS["danger"] if panel.WRITES else COLORS["fg"],
                activebackground=COLORS["select"], activeforeground=COLORS["fg"],
                relief="flat", padx=8, pady=2, highlightthickness=1,
                highlightbackground=COLORS["line"],
            ).pack(side="left", padx=(0, 4))
        self._mount(self.panels[0])

    def _mount(self, panel: type[Editor]) -> None:
        if self.editor is not None:
            self.editor.close()
            self.editor.destroy()
            self.editor = None
        self.subtitle.configure(text=panel.SUBTITLE)
        editor = panel(self.area, self.device, app=self)
        editor.pack(fill="both", expand=True)
        try:
            editor.build()
        except Exception as e:                   # a panel must never take the app down
            tk.Label(editor, text=f"이 패널을 열 수 없습니다: {e}",
                     bg=COLORS["panel"], fg=COLORS["danger"], font=mono(10),
                     wraplength=700, justify="left").pack(padx=16, pady=16)
            self.status(f"{panel.__name__}: {e}", "danger")
        self.editor = editor

    # -- rescan ------------------------------------------------------------

    def rescan(self) -> None:
        self.status("스캔 중…", "cyan")

        def work():
            scanner = build_scanner()
            result = scanner.scan(ProbeContext(deep=self.deep))
            self.root.after(0, lambda: self._rescanned(result))

        threading.Thread(target=work, daemon=True).start()

    def _rescanned(self, result: ScanResult) -> None:
        self.result = result
        self._fill_list()
        self.status(f"{len(result.devices)} devices  ·  {result.duration:.1f}s", "ok")

    # -- run ---------------------------------------------------------------

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            if self.editor is not None:
                self.editor.close()


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 1] + "…"
