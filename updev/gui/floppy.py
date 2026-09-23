"""The 꼬깔 editor: a FAT12 disk you can actually edit.

`updev floppy make` builds one fixed image from the art in `floppy.py`. This
is the other half — open an image (or the medium in the drive), change the
files, change the 16-bit boot sector's message, and write it back.

Two things make this more than a text editor with extra steps:

  * **It rebuilds rather than patches.** Editing a file in place would mean
    rewriting FAT chains and directory entries by hand for no gain, so the
    whole 1.44 MB is reassembled through the same `build_image()` the CLI
    uses. Whatever comes out is an image that passed the same construction.
  * **The boot sector has a hard budget.** Code plus message must fit in
    448 bytes, and the counter under the box is live, because finding out at
    write time that the message is nine bytes too long is a bad way to find
    out.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

from ..floppy import (
    FLOPPY_SIZE,
    boot_message,
    build_boot_code,
    build_image,
    gallery,
    inspect_image,
    read_file,
)
from .base import COLORS, Editor, block_node, confirm_write, mono, notice

#: Code + message have to fit between the BPB and the 0x55AA signature.
BOOT_BUDGET = 0x1FE - 0x3E

#: Raw block access is root:disk. The `disk` group would open every disk on the
#: machine — raw read past every file permission, raw write over the root
#: filesystem — so the panel offers this instead: one rule, matching only a USB
#: floppy drive, handing it to a group the user is already in.
FLOPPY_UDEV_RULE = (
    'SUBSYSTEM=="block", ENV{ID_BUS}=="usb", ENV{ID_TYPE}=="floppy", '
    'GROUP="plugdev", MODE="0660"'
)


class FloppyEditor(Editor):
    TITLE = "플로피 (FAT12) 에디터"
    SUBTITLE = "파일과 부트섹터 메시지를 고쳐서 이미지를 다시 굽는다"
    WRITES = True

    def __init__(self, parent, device, app=None) -> None:
        super().__init__(parent, device, app)
        self.files: dict[str, str] = {}
        self.label = "UPDEV ART"
        self.message = boot_message()
        self.current: str | None = None
        self.source = "기본 아트"
        #: The medium, not the sysfs directory. Empty when no disk path exists.
        self.node = block_node(device)

    # -- layout ------------------------------------------------------------

    def build(self) -> None:
        bar = self.row()
        tk.Label(bar, text="원본", bg=COLORS["panel"], fg=COLORS["dim"],
                 font=mono(10)).pack(side="left", padx=(0, 8))
        self.source_label = tk.Label(bar, text="", bg=COLORS["panel"],
                                     fg=COLORS["cyan"], font=mono(10, "bold"))
        self.source_label.pack(side="left")
        self.button(bar, "기본 아트", self.load_default).pack(side="right", padx=3)
        self.button(bar, "이미지 열기…", self.open_image).pack(side="right", padx=3)
        if self.node:
            self.button(bar, f"드라이브 읽기 ({self.node})",
                        self.read_medium).pack(side="right", padx=3)

        middle = tk.Frame(self, bg=COLORS["panel"])
        middle.pack(fill="both", expand=True, padx=12, pady=(6, 0))

        left = tk.Frame(middle, bg=COLORS["panel"])
        left.pack(side="left", fill="y")
        tk.Label(left, text="루트 디렉터리", bg=COLORS["panel"], fg=COLORS["fg"],
                 font=mono(10, "bold"), anchor="w").pack(fill="x")
        self.listbox = tk.Listbox(
            left, bg=COLORS["bg"], fg=COLORS["fg"], font=mono(10), width=18,
            height=12, relief="flat", highlightthickness=1,
            highlightbackground=COLORS["line"], selectbackground=COLORS["select"],
            activestyle="none", exportselection=False,
        )
        self.listbox.pack(fill="y", expand=True, pady=(4, 4))
        self.listbox.bind("<<ListboxSelect>>", self._select_file)

        file_buttons = tk.Frame(left, bg=COLORS["panel"])
        file_buttons.pack(fill="x")
        self.button(file_buttons, "추가", self.add_file).pack(side="left", padx=(0, 4))
        self.button(file_buttons, "삭제", self.remove_file, danger=True).pack(side="left")

        right = tk.Frame(middle, bg=COLORS["panel"])
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.file_title = tk.Label(right, text="파일 내용", bg=COLORS["panel"],
                                   fg=COLORS["fg"], font=mono(10, "bold"), anchor="w")
        self.file_title.pack(fill="x")
        self.editor = tk.Text(
            right, bg=COLORS["bg"], fg=COLORS["fg"], font=mono(10), height=14,
            relief="flat", padx=10, pady=8, wrap="none",
            insertbackground=COLORS["accent"], selectbackground=COLORS["select"],
        )
        self.editor.pack(fill="both", expand=True, pady=(4, 0))
        self.editor.bind("<KeyRelease>", self._stash_current)

        boot = tk.Frame(self, bg=COLORS["panel"])
        boot.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(boot, text="부트섹터 메시지 (16비트 x86가 INT 10h로 찍는 것)",
                 bg=COLORS["panel"], fg=COLORS["fg"], font=mono(10, "bold"),
                 anchor="w").pack(fill="x")
        self.boot_text = tk.Text(
            boot, bg=COLORS["bg"], fg=COLORS["ok"], font=mono(9), height=7,
            relief="flat", padx=10, pady=6, wrap="none",
            insertbackground=COLORS["accent"], selectbackground=COLORS["select"],
        )
        self.boot_text.pack(fill="x", pady=(4, 2))
        self.boot_text.bind("<KeyRelease>", lambda _e: self._update_budget())
        self.budget = tk.Label(boot, text="", bg=COLORS["panel"], fg=COLORS["dim"],
                               font=mono(9), anchor="w")
        self.budget.pack(fill="x")

        actions = self.row()
        self.button(actions, ".img 로 저장…", self.save_image).pack(side="left", padx=(0, 4))
        if self.node:
            self.button(actions, f"{self.node} 에 굽기", self.write_medium,
                        danger=True).pack(side="left", padx=4)
        self.button(actions, "되돌리기", self.reload).pack(side="left", padx=4)
        self.info = tk.Label(actions, text="", bg=COLORS["panel"], fg=COLORS["dim"],
                             font=mono(9))
        self.info.pack(side="right")

        notice(self, "쓰기는 전부 확인 창을 거칩니다. 드라이브에 굽는 건 매체를 "
                     "통째로 덮어씁니다 — 되돌릴 수 없습니다.", "warn")
        self.load_default()

    # -- sources -----------------------------------------------------------

    def load_default(self) -> None:
        self.files = {name: text for name, text in gallery().items()}
        self.message = boot_message()
        self.label = "UPDEV ART"
        self.source = "기본 아트 (updev floppy make 와 같은 것)"
        self._refresh()

    def open_image(self) -> None:
        path = filedialog.askopenfilename(
            title="플로피 이미지 열기",
            filetypes=[("디스크 이미지", "*.img *.ima *.flp"), ("전부", "*.*")],
            parent=self,
        )
        if path:
            self._load_bytes(Path(path).read_bytes(), f"파일 {path}")

    def read_medium(self) -> None:
        """Read the disk in the drive. 1.44 MB, so no streaming needed."""
        try:
            with open(self.node, "rb") as handle:
                data = handle.read(FLOPPY_SIZE)
        except PermissionError:
            messagebox.showerror(
                "드라이브 읽기",
                self.node + " 를 읽을 권한이 없습니다.\n\n"
                "이 드라이브만 열어주는 udev 규칙이 제일 깔끔합니다 — USB 플로피에만 "
                "붙고 다른 디스크에는 안 걸립니다:\n\n"
                "  echo '" + FLOPPY_UDEV_RULE + "' \\\n"
                "    | sudo tee /etc/udev/rules.d/99-updev-floppy.rules\n"
                "  sudo udevadm control --reload && sudo udevadm trigger\n\n"
                "disk 그룹은 권하지 않습니다: 모든 블록 장치를 원시로 읽고 쓰게 되므로 "
                "파일 권한을 통째로 우회하는, 사실상 root 권한입니다.",
                parent=self)
            return
        except OSError as e:
            messagebox.showerror("드라이브 읽기",
                                 f"{self.node}: {e.strerror or e}\n\n"
                                 f"디스켓이 안 들어있을 수도 있습니다.", parent=self)
            return
        self._load_bytes(data, f"드라이브 {self.node}")

    def _load_bytes(self, data: bytes, source: str) -> None:
        info = inspect_image(data)
        if "error" in info:
            messagebox.showerror("이미지 열기", info["error"], parent=self)
            return
        self.files = {}
        for entry in info.get("entries", []):
            if entry["volume_label"]:
                self.label = entry["name"]
                continue
            payload = read_file(data, entry["name"])
            self.files[entry["name"]] = payload.decode("ascii", "replace").replace(
                "\r\n", "\n")
        self.label = info.get("volume_label") or self.label
        self.source = source
        # The boot message lives in the sector as a NUL-terminated string after
        # the code; recovering it exactly is not worth guessing at, so the box
        # keeps whatever is loaded and only overwrites the image if edited.
        self._refresh()
        self.status(f"{len(self.files)} file(s) · label {self.label!r} · "
                    f"{info.get('size', 0):,} bytes", "ok")

    def reload(self) -> None:
        self.load_default()

    # -- files -------------------------------------------------------------

    def _refresh(self) -> None:
        self.listbox.delete(0, "end")
        for name in self.files:
            self.listbox.insert("end", name)
        self.source_label.configure(text=self.source)
        self.boot_text.delete("1.0", "end")
        self.boot_text.insert("1.0", self.message.replace("\r\n", "\n"))
        self._update_budget()
        if self.files:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(0)
            self._select_file()
        else:
            self.current = None
            self.editor.delete("1.0", "end")

    def _select_file(self, _event=None) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        name = self.listbox.get(selection[0])
        self.current = name
        self.file_title.configure(text=f"파일 내용   {name}")
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", self.files.get(name, ""))

    def _stash_current(self, _event=None) -> None:
        if self.current:
            self.files[self.current] = self.editor.get("1.0", "end-1c")

    def add_file(self) -> None:
        from tkinter import simpledialog

        name = simpledialog.askstring(
            "파일 추가", "8.3 이름 (FAT12는 긴 이름이 없습니다):",
            initialvalue="NEW.TXT", parent=self)
        if not name:
            return
        name = name.strip().upper()
        stem, _, ext = name.partition(".")
        if not stem or len(stem) > 8 or len(ext) > 3:
            messagebox.showerror("파일 추가",
                                 f"{name} 은 8.3 형식이 아닙니다", parent=self)
            return
        self.files[name] = ""
        self._refresh()

    def remove_file(self) -> None:
        if not self.current:
            return
        if messagebox.askyesno("파일 삭제", f"{self.current} 를 뺄까요?",
                               default="no", parent=self):
            self.files.pop(self.current, None)
            self.current = None
            self._refresh()

    # -- boot sector -------------------------------------------------------

    def _boot_message(self) -> str:
        """The box uses \\n; the BIOS teletype needs \\r\\n."""
        raw = self.boot_text.get("1.0", "end-1c")
        return raw.replace("\r\n", "\n").replace("\n", "\r\n")

    def _update_budget(self) -> None:
        try:
            used = len(build_boot_code(self._boot_message()))
            left = BOOT_BUDGET - used
            colour = "ok" if left > 40 else "warn"
            self.budget.configure(
                text=f"부트섹터 {used}/{BOOT_BUDGET} 바이트  ·  {left} 남음",
                fg=COLORS[colour])
        except ValueError as e:
            self.budget.configure(text=str(e), fg=COLORS["danger"])

    # -- output ------------------------------------------------------------

    def _assemble(self) -> bytes | None:
        self._stash_current()
        try:
            image = build_image(label=self.label, files=self.files,
                                message=self._boot_message())
        except ValueError as e:
            messagebox.showerror("이미지 조립", str(e), parent=self)
            return None
        self.info.configure(
            text=f"{image.size:,} bytes · {len(image.files)} files · "
                 f"{image.free_bytes:,} free")
        return image.data

    def save_image(self) -> None:
        data = self._assemble()
        if data is None:
            return
        path = filedialog.asksaveasfilename(
            title="이미지 저장", defaultextension=".img",
            initialfile="updev-art.img",
            filetypes=[("디스크 이미지", "*.img")], parent=self)
        if not path:
            return
        Path(path).write_bytes(data)
        self.status(f"wrote {path} ({len(data):,} bytes)", "ok")

    def write_medium(self) -> None:
        """Overwrite the disk in the drive. The one genuinely final action here."""
        data = self._assemble()
        if data is None:
            return
        if not confirm_write(
            self, "드라이브에 굽기",
            f"{self.node} 를 {len(data):,} 바이트로 통째로 덮어씁니다.",
            f"파일 {len(self.files)}개 · 볼륨 레이블 {self.label!r}\n"
            f"대상: {self.device.label}  ({self.device.address})",
            danger="디스켓에 들어있던 내용은 전부 사라집니다. 되돌릴 수 없습니다.",
        ):
            return
        try:
            with open(self.node, "wb") as handle:
                handle.write(data)
                handle.flush()
                import os
                os.fsync(handle.fileno())
        except PermissionError:
            messagebox.showinfo(
                "권한 없음",
                f"{self.node} 에 쓸 권한이 없습니다. 이미지를 저장한 뒤 "
                f"터미널에서:\n\n"
                f"  sudo dd if=updev-art.img of={self.node} bs=512 conv=fsync",
                parent=self)
            return
        except OSError as e:
            messagebox.showerror("굽기 실패", f"{self.node}: {e.strerror or e}",
                                 parent=self)
            return
        self.status(f"wrote {len(data):,} bytes to {self.node}", "ok")
        messagebox.showinfo(
            "완료",
            f"{self.node} 에 {len(data):,} 바이트를 썼습니다.\n\n"
            f"확인:  sudo updev floppy info {self.node}", parent=self)
