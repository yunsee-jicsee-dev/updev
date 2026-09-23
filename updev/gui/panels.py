"""The rest of the editors — one per kind of thing that has an "inside".

Sector viewer, register editor, pin editor, event log, and the info panel that
catches everything else so no device opens to a blank frame.

Where a panel can write, it writes the way the CLI does: one value at a time,
behind a dialog that names the target. Where it deliberately cannot — raw
sectors of a mounted disk — it says why instead of offering a switch that
should never be flipped.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from ..core.model import Kind
from .base import (
    COLORS,
    Editor,
    HexView,
    block_node,
    confirm_write,
    mono,
    notice,
)

SECTOR = 512


# ==========================================================================
# block devices
# ==========================================================================

class BlockEditor(Editor):
    TITLE = "섹터 뷰어"
    SUBTITLE = "블록 장치를 512바이트 단위로 — 읽기 전용"
    WRITES = False

    def build(self) -> None:
        self.node = block_node(self.device)
        bar = self.row()
        tk.Label(bar, text="섹터", bg=COLORS["panel"], fg=COLORS["dim"],
                 font=mono(10)).pack(side="left")
        self.entry = tk.Entry(bar, width=12, bg=COLORS["bg"], fg=COLORS["fg"],
                              font=mono(10), relief="flat",
                              insertbackground=COLORS["accent"],
                              highlightthickness=1, highlightbackground=COLORS["line"])
        self.entry.insert(0, "0")
        self.entry.pack(side="left", padx=(6, 10))
        self.entry.bind("<Return>", lambda _e: self.load())
        self.button(bar, "읽기", self.load).pack(side="left", padx=3)
        self.button(bar, "◀ 이전", lambda: self.step(-1)).pack(side="left", padx=3)
        self.button(bar, "다음 ▶", lambda: self.step(1)).pack(side="left", padx=3)
        self.size_label = tk.Label(bar, text="", bg=COLORS["panel"],
                                   fg=COLORS["dim"], font=mono(9))
        self.size_label.pack(side="right")

        self.hex = HexView(self, editable=False, height=20)
        self.hex.pack(fill="both", expand=True, padx=12, pady=(8, 4))

        notice(self, "읽기 전용입니다. 마운트된 파일시스템 밑의 섹터를 고치면 커널이 "
                     "들고 있는 것과 디스크가 어긋나고, 그건 권한 문제가 아니라 "
                     "고치는 방법이 없는 문제라서 여기서는 쓰기를 아예 안 만들었습니다.")
        self.load()

    def step(self, direction: int) -> None:
        try:
            current = int(self.entry.get(), 0)
        except ValueError:
            current = 0
        self.entry.delete(0, "end")
        self.entry.insert(0, str(max(0, current + direction)))
        self.load()

    def _blocked(self, reason: str) -> None:
        """A panel that cannot read says so in the panel, not just the status bar."""
        self.status(reason, "danger")
        self.hex.show(b"")
        self.hex.text.configure(state="normal")
        self.hex.text.insert("1.0", f"\n  {reason}\n", "note")
        self.hex.text.configure(state="disabled")

    def load(self) -> None:
        if not self.node:
            self._blocked("이 장치에는 /dev 노드가 없습니다")
            return
        try:
            sector = int(self.entry.get(), 0)
        except ValueError:
            return
        try:
            fd = os.open(self.node, os.O_RDONLY)
        except PermissionError:
            self._blocked(f"{self.node}: 읽기 권한이 없습니다 — 원시 섹터는 "
                          f"root:disk 입니다. sudo 로 실행하거나, 이 장치 하나만 "
                          f"여는 udev 규칙을 거세요. disk 그룹은 모든 디스크를 "
                          f"원시로 열어주는 사실상 root라 권하지 않습니다.")
            return
        except OSError as e:
            self._blocked(f"{self.node}: {e.strerror or e}")
            return
        try:
            size = os.lseek(fd, 0, os.SEEK_END)
            data = os.pread(fd, SECTOR, sector * SECTOR)
        except OSError as e:
            self._blocked(f"{self.node}: {e.strerror or e}")
            return
        finally:
            os.close(fd)

        notes = {}
        if sector == 0 and len(data) == SECTOR and data[510:512] == b"\x55\xAA":
            notes[496] = "0x55AA — MBR/boot signature"
            notes[448] = "partition table"
        self.hex.show(data, base=sector * SECTOR, notes=notes)
        self.size_label.configure(
            text=f"{self.node}   {size:,} bytes   ({size // SECTOR:,} sectors)")
        self.status(f"sector {sector} of {self.node}", "ok")


# ==========================================================================
# I2C registers
# ==========================================================================

class RegisterEditor(Editor):
    TITLE = "레지스터 에디터"
    SUBTITLE = "I2C 칩의 레지스터 공간을 읽고, 한 바이트씩 고친다"
    WRITES = True

    #: The addresses `updev i2c scan` treats as read-only for the same reason:
    #: a stray write to a HAT EEPROM is not recoverable from a dialog.
    EEPROM = set(range(0x50, 0x58)) | set(range(0x30, 0x38))

    def build(self) -> None:
        self.bus = _bus_number(self.device)
        self.address = _i2c_address(self.device)

        bar = self.row()
        tk.Label(bar, text=f"i2c-{self.bus}  0x{self.address:02x}",
                 bg=COLORS["panel"], fg=COLORS["cyan"],
                 font=mono(10, "bold")).pack(side="left")
        tk.Label(bar, text="  길이", bg=COLORS["panel"], fg=COLORS["dim"],
                 font=mono(10)).pack(side="left", padx=(14, 4))
        self.length = tk.Spinbox(bar, values=(16, 32, 64, 128, 256), width=5,
                                 bg=COLORS["bg"], fg=COLORS["fg"], font=mono(10),
                                 relief="flat", buttonbackground=COLORS["raised"],
                                 highlightthickness=1,
                                 highlightbackground=COLORS["line"])
        self.length.pack(side="left")
        self.button(bar, "읽기", self.load).pack(side="left", padx=8)
        self.button(bar, "바뀐 바이트 쓰기", self.write_changes,
                    danger=True).pack(side="left")

        self.hex = HexView(self, editable=True, height=17)
        self.hex.pack(fill="both", expand=True, padx=12, pady=(8, 4))
        self.original = b""

        if self.address in self.EEPROM:
            notice(self, f"0x{self.address:02x} 는 EEPROM 구간입니다. HAT EEPROM일 수 "
                         f"있어서 쓰기는 막아뒀습니다 — 읽기만 됩니다.", "warn")
        else:
            notice(self, "hex를 더블클릭해서 값을 고치고 [바뀐 바이트 쓰기]. 레지스터 "
                         "하나를 잘못 쓰면 칩 설정이 망가질 수 있어서, 나가는 바이트를 "
                         "전부 보여주고 한 번 더 묻습니다.")
        self.load()

    def _bus(self):
        try:
            from smbus2 import SMBus
        except ImportError:
            self.status("python3-smbus2 가 없습니다", "danger")
            return None
        return SMBus(self.bus)

    def load(self) -> None:
        bus = self._bus()
        if bus is None:
            return
        count = int(self.length.get())
        data = bytearray()
        try:
            with bus:
                for register in range(count):
                    try:
                        data.append(bus.read_byte_data(self.address, register))
                    except OSError:
                        data.append(0xFF)
        except OSError as e:
            self.status(f"i2c-{self.bus} 0x{self.address:02x}: {e.strerror or e}",
                        "danger")
            return
        self.original = bytes(data)
        self.hex.show(self.original)
        self.status(f"read {count} registers from 0x{self.address:02x}", "ok")

    def write_changes(self) -> None:
        if self.address in self.EEPROM:
            messagebox.showinfo("쓰기 막힘",
                                f"0x{self.address:02x} 는 EEPROM 구간이라 "
                                f"쓰기를 만들지 않았습니다.", parent=self)
            return
        edited = bytes(self.hex.data)
        changed = [(i, edited[i]) for i in range(min(len(edited), len(self.original)))
                   if edited[i] != self.original[i]]
        if not changed:
            messagebox.showinfo("쓰기", "바뀐 바이트가 없습니다", parent=self)
            return
        detail = "\n".join(
            f"  reg 0x{reg:02X}   0x{self.original[reg]:02X} → 0x{value:02X}"
            for reg, value in changed)
        if not confirm_write(
            self, "레지스터 쓰기",
            f"i2c-{self.bus} 0x{self.address:02x} 에 {len(changed)} 바이트를 씁니다.",
            detail,
            danger="레지스터를 잘못 쓰면 칩 설정이 망가지거나 벽돌이 됩니다.",
        ):
            return
        bus = self._bus()
        if bus is None:
            return
        failed = []
        with bus:
            for register, value in changed:
                try:
                    bus.write_byte_data(self.address, register, value)
                except OSError as e:
                    failed.append(f"0x{register:02X}: {e.strerror or e}")
        if failed:
            self.status(" · ".join(failed), "danger")
            messagebox.showerror("쓰기 실패", "\n".join(failed), parent=self)
        else:
            self.status(f"{len(changed)} register(s) written", "ok")
        self.load()


# ==========================================================================
# GPIO
# ==========================================================================

class PinEditor(Editor):
    TITLE = "핀 에디터"
    SUBTITLE = "40핀 헤더의 현재 상태 — 출력 제어는 따로 켠다"
    WRITES = True

    def build(self) -> None:
        from ..backends.gpio import PIN_FUNCTIONS, read_pin_state, _HEADER

        self.header = _HEADER
        self.functions = PIN_FUNCTIONS
        self._read_state = read_pin_state
        self.unlocked = False
        self.handle = None

        bar = self.row()
        self.button(bar, "새로고침", self.refresh).pack(side="left", padx=(0, 6))
        self.unlock_button = self.button(bar, "출력 제어 켜기", self.unlock, danger=True)
        self.unlock_button.pack(side="left")
        self.mode = tk.Label(bar, text="읽기 전용", bg=COLORS["panel"],
                             fg=COLORS["dim"], font=mono(10))
        self.mode.pack(side="right")

        self.grid_frame = tk.Frame(self, bg=COLORS["bg"])
        self.grid_frame.pack(fill="both", expand=True, padx=12, pady=(8, 4))

        notice(self, "핀을 클릭하면 그 핀의 현재 기능·레벨·풀 상태를 봅니다. 출력 제어를 "
                     "켜면 클릭이 레벨 토글이 됩니다 — 뭔가 물려 있는 핀을 출력으로 "
                     "몰면 그쪽 하드웨어가 다칠 수 있습니다.")
        self.refresh()

    def close(self) -> None:
        super().close()
        if self.handle is not None:
            try:
                import lgpio

                lgpio.gpiochip_close(self.handle)
            except Exception:
                pass
            self.handle = None

    def unlock(self) -> None:
        if not confirm_write(
            self, "출력 제어",
            "핀을 출력으로 잡고 레벨을 바꿀 수 있게 합니다.",
            "센서·모듈이 물려 있는 핀을 출력으로 몰면, 양쪽이 서로 다른 레벨을 "
            "밀면서 전류가 흐릅니다.",
            danger="어느 핀에 뭐가 물려 있는지 아는 상태에서만 켜세요.",
        ):
            return
        try:
            import lgpio

            self.handle = lgpio.gpiochip_open(0)
        except Exception as e:
            messagebox.showerror("출력 제어", f"lgpio를 열 수 없습니다: {e}",
                                 parent=self)
            return
        self.unlocked = True
        self.mode.configure(text="출력 제어 켜짐 — 클릭하면 토글", fg=COLORS["danger"])
        self.unlock_button.configure(state="disabled")

    def refresh(self) -> None:
        for child in self.grid_frame.winfo_children():
            child.destroy()
        try:
            state = self._read_state()
        except Exception:
            state = {}

        for row, (left, right) in enumerate(zip(range(1, 41, 2), range(2, 41, 2))):
            for column, pin in ((0, left), (3, right)):
                gpio = self.header.get(pin)
                label, function = self.functions.get(pin, ("", ""))
                info = state.get(gpio, {}) if gpio is not None else {}
                colour = self._colour(label, info)
                text = f"{pin:>2} {label}"
                cell = tk.Label(
                    self.grid_frame, text=text, bg=COLORS["bg"], fg=colour,
                    font=mono(9), anchor="w" if column == 0 else "e", padx=6,
                )
                cell.grid(row=row, column=column, sticky="ew")
                detail = tk.Label(
                    self.grid_frame,
                    text=(info.get("function", "") or function)[:18],
                    bg=COLORS["bg"], fg=COLORS["dim"], font=mono(8),
                    anchor="w" if column == 0 else "e", padx=6,
                )
                detail.grid(row=row, column=column + 1, sticky="ew")
                if gpio is not None:
                    for widget in (cell, detail):
                        widget.bind("<Button-1>",
                                    lambda _e, g=gpio, p=pin: self.click(g, p))
        for column in range(4):
            self.grid_frame.columnconfigure(column, weight=1)

    @staticmethod
    def _colour(label: str, info: dict) -> str:
        if label in ("3V3", "5V"):
            return COLORS["warn"]
        if label == "GND":
            return COLORS["dim"]
        if info.get("level") == "high":
            return COLORS["ok"]
        if info.get("function", "none") not in ("none", "", "-"):
            return COLORS["cyan"]
        return COLORS["fg"]

    def click(self, gpio: int, pin: int) -> None:
        state = {}
        try:
            state = self._read_state().get(gpio, {})
        except Exception:
            pass
        if not self.unlocked:
            self.status(
                f"pin {pin} · GPIO{gpio} · {state.get('function', '?')} "
                f"· level {state.get('level', '?')} · pull {state.get('pull', '?')}",
                "ok")
            return

        import lgpio

        level = 0 if state.get("level") == "high" else 1
        if not confirm_write(
            self, "핀 토글",
            f"GPIO{gpio} (pin {pin}) 를 출력으로 잡고 {level} 로 밉니다.",
            f"지금: {state.get('function', '?')} · level {state.get('level', '?')}",
        ):
            return
        try:
            lgpio.gpio_claim_output(self.handle, gpio, level)
            self.status(f"GPIO{gpio} → {level}", "ok")
        except Exception as e:
            self.status(f"GPIO{gpio}: {e}", "danger")
        self.refresh()


# ==========================================================================
# HID events
# ==========================================================================

class EventEditor(Editor):
    TITLE = "입력 이벤트"
    SUBTITLE = "이 장치가 커널에 보내는 evdev 이벤트 그대로"
    WRITES = False

    def build(self) -> None:
        from ..hid import resolve

        self.nodes = [n for n in resolve(self.device.address) if n.readable]
        self.queue: queue.Queue = queue.Queue()
        self.thread = None
        self.stop = threading.Event()

        bar = self.row()
        self.toggle = self.button(bar, "시작", self.start)
        self.toggle.pack(side="left")
        self.button(bar, "지우기", self.clear).pack(side="left", padx=6)
        tk.Label(bar, text=", ".join(n.path for n in self.nodes) or "읽을 수 있는 노드 없음",
                 bg=COLORS["panel"], fg=COLORS["dim"], font=mono(9)).pack(side="right")

        self.log = tk.Text(self, bg=COLORS["bg"], fg=COLORS["fg"], font=mono(10),
                           relief="flat", padx=10, pady=8, height=20,
                           state="disabled", wrap="none")
        self.log.pack(fill="both", expand=True, padx=12, pady=(8, 4))
        self.log.tag_configure("key", foreground=COLORS["ok"])
        self.log.tag_configure("dim", foreground=COLORS["dim"])

        if any("keys" in n.capabilities for n in self.nodes):
            notice(self, "이 장치는 키 입력을 보냅니다 — 여기 찍히는 건 실제로 "
                         "입력되는 내용입니다.", "warn")
        else:
            notice(self, "커널이 이미 디코드해둔 이벤트를 그대로 읽습니다.")

    def close(self) -> None:
        super().close()
        self.stop.set()

    def start(self) -> None:
        if self.thread is not None:
            self.stop.set()
            self.thread = None
            self.toggle.configure(text="시작")
            return
        if not self.nodes:
            self.status("읽을 수 있는 이벤트 노드가 없습니다 — input 그룹에 "
                        "들어가야 할 수 있습니다", "warn")
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()
        self.toggle.configure(text="정지")
        self.every(120, self._drain)

    def _pump(self) -> None:
        """Blocking reads live in a thread; the UI only ever sees the queue."""
        from ..hid import describe, watch

        for node, event in watch(self.nodes, duration=0.0, quiet=True):
            if self.stop.is_set():
                return
            self.queue.put((Path(node.path).name, describe(event), event.type))

    def _drain(self) -> None:
        wrote = False
        while True:
            try:
                name, text, etype = self.queue.get_nowait()
            except queue.Empty:
                break
            self.log.configure(state="normal")
            self.log.insert("end", f"{name:<9}", "dim")
            self.log.insert("end", f"{text}\n", "key" if etype == 0x01 else "")
            self.log.configure(state="disabled")
            wrote = True
        if wrote:
            self.log.see("end")

    def clear(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


# ==========================================================================
# the catch-all
# ==========================================================================

class InfoEditor(Editor):
    TITLE = "장치 정보"
    SUBTITLE = "이 장치에 대해 아는 것 전부, 그리고 실행할 수 있는 명령"
    WRITES = False

    def build(self) -> None:
        text = tk.Text(self, bg=COLORS["bg"], fg=COLORS["fg"], font=mono(10),
                       relief="flat", padx=12, pady=10, wrap="word", height=22,
                       state="disabled")
        text.pack(fill="both", expand=True, padx=12, pady=(8, 4))
        text.tag_configure("key", foreground=COLORS["dim"])
        text.tag_configure("head", foreground=COLORS["accent"], font=mono(10, "bold"))
        text.tag_configure("cmd", foreground=COLORS["cyan"])
        text.tag_configure("warn", foreground=COLORS["warn"])

        dev = self.device
        text.configure(state="normal")
        text.insert("end", f"{dev.label}\n", "head")
        for key, value in (
            ("uid", dev.uid), ("kind", str(dev.kind)), ("status", str(dev.status)),
            ("bus", dev.bus), ("address", dev.address), ("node", dev.node),
            ("vendor", dev.vendor), ("model", dev.model), ("serial", dev.serial),
            ("driver", dev.driver), ("tags", ", ".join(dev.tags)),
            ("summary", dev.summary),
        ):
            if value:
                text.insert("end", f"{key:>10}  ", "key")
                text.insert("end", f"{value}\n")

        if dev.detail:
            text.insert("end", "\n상세\n", "head")
            for key, value in dev.detail.items():
                text.insert("end", f"{key:>10}  ", "key")
                text.insert("end", f"{value}\n")

        if dev.issues:
            text.insert("end", "\n문제\n", "head")
            for issue in dev.issues:
                text.insert("end", f"  {issue.severity}  ", "warn")
                text.insert("end", f"{issue.message}\n")
                if issue.fix:
                    text.insert("end", f"      $ {issue.fix}\n", "cmd")

        from ..toolkit import annotate, recognize

        recognition = recognize(dev)
        annotate(recognition.tools)
        if recognition.tools:
            text.insert("end", "\n할 수 있는 것\n", "head")
            for tool in recognition.tools:
                mark = "★ " if tool.lead else "  "
                text.insert("end", f"{mark}{tool.title}\n")
                text.insert("end", f"    {tool.command}\n", "cmd")
        text.configure(state="disabled")


def _bus_number(device) -> int:
    """`i2c:1:0x3c` → 1, `i2c-1` → 1."""
    parts = device.uid.split(":")
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    digits = "".join(c for c in device.bus if c.isdigit())
    return int(digits) if digits else 1


def _i2c_address(device) -> int:
    for candidate in (device.address, device.uid.split(":")[-1]):
        try:
            return int(str(candidate), 16)
        except (TypeError, ValueError):
            continue
    return 0
