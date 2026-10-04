"""The tag editor — the same shape as the floppy editor, for a card.

The parallel is deliberate and it goes all the way down. A floppy is an image
file you can open, edit and write back to the medium; a MIFARE card is a dump
file you can open, edit and write back to the medium. Both editors put the
container's structure on the left, the bytes on the right, and the rules that
govern writing underneath.

What a card has that a disk doesn't is *permission encoded in the medium
itself*. Every sector's trailer carries nine bits saying who may read, write,
increment and decrement each block, and with which of the two keys. So the
panel under the hex is not decoration: it is the difference between "the write
failed" and "that block has been read-only since the day it was formatted, and
here is the byte that says so".

Reading is free. Writing goes through the same guards as the CLI — block 0 and
sector trailers are refused by `nfc.check_writable()` before a byte moves, and
the trailer override needs its own confirmation because a wrong access-bit
combination locks that sector for good.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

from ..mifare import (
    DEFAULT_KEYS,
    CardLayout,
    decode_access_bits,
    describe_permissions,
    group_of,
    layout_for_sak,
    parse_ndef,
)
from ..nfc import (
    DEFAULT_KEY,
    Mfrc522,
    NfcError,
    ascii_dump,
    is_trailer,
)
from .base import COLORS, Editor, HexView, confirm_write, mono, notice


class TagEditor(Editor):
    TITLE = "NFC 태그 에디터"
    SUBTITLE = "카드 전체 덤프 · 섹터별 접근 조건 · 블록 편집 · NDEF"
    WRITES = True

    def __init__(self, parent, device, app=None) -> None:
        super().__init__(parent, device, app)
        self.chip = None
        self._close = None
        self.reader = None
        self.tag = None
        self.layout: CardLayout | None = None
        self.blocks: dict[int, bytes] = {}
        self.original: dict[int, bytes] = {}
        self.sector_keys: dict[int, bytes] = {}
        self.sector_error: dict[int, str] = {}
        self.sector = 0
        self.source = "카드 없음"

    # ==================================================================
    # layout
    # ==================================================================

    def build(self) -> None:
        self._build_source_bar()
        self._build_middle()
        self._build_access()
        self._build_actions()
        notice(self, "hex를 더블클릭하면 그 한 바이트를 고칩니다. 바뀐 바이트는 "
                     "노란색이고, 쓰기는 바뀐 블록만 나갑니다. block 0과 섹터 "
                     "트레일러는 따로 막혀 있습니다.")
        self._open_reader()

    def _build_source_bar(self) -> None:
        bar = self.row()
        tk.Label(bar, text="원본", bg=COLORS["panel"], fg=COLORS["dim"],
                 font=mono(10)).pack(side="left", padx=(0, 8))
        self.source_label = tk.Label(bar, text=self.source, bg=COLORS["panel"],
                                     fg=COLORS["cyan"], font=mono(10, "bold"))
        self.source_label.pack(side="left")
        self.button(bar, "덤프 저장…", self.save_dump).pack(side="right", padx=3)
        self.button(bar, "덤프 열기…", self.open_dump).pack(side="right", padx=3)
        self.button(bar, "카드 전체 읽기", self.read_card).pack(side="right", padx=3)

        self.reader_label = tk.Label(self, text="리더 여는 중…", bg=COLORS["panel"],
                                     fg=COLORS["dim"], font=mono(9), anchor="w")
        self.reader_label.pack(fill="x", padx=12)
        self.tag_label = tk.Label(self, text="카드를 리더에 올리고 [카드 전체 읽기]",
                                  bg=COLORS["panel"], fg=COLORS["fg"],
                                  font=mono(11, "bold"), anchor="w")
        self.tag_label.pack(fill="x", padx=12, pady=(2, 0))

    def _build_middle(self) -> None:
        middle = tk.Frame(self, bg=COLORS["panel"])
        middle.pack(fill="both", expand=True, padx=12, pady=(6, 0))

        left = tk.Frame(middle, bg=COLORS["panel"])
        left.pack(side="left", fill="y")
        tk.Label(left, text="섹터", bg=COLORS["panel"], fg=COLORS["fg"],
                 font=mono(10, "bold"), anchor="w").pack(fill="x")
        self.sector_list = tk.Listbox(
            left, bg=COLORS["bg"], fg=COLORS["fg"], font=mono(10), width=20,
            height=13, relief="flat", highlightthickness=1,
            highlightbackground=COLORS["line"], selectbackground=COLORS["select"],
            activestyle="none", exportselection=False,
        )
        self.sector_list.pack(fill="y", expand=True, pady=(4, 4))
        self.sector_list.bind("<<ListboxSelect>>", self._select_sector)

        keys = tk.Frame(left, bg=COLORS["panel"])
        keys.pack(fill="x")
        tk.Label(keys, text="key A", bg=COLORS["panel"], fg=COLORS["dim"],
                 font=mono(9)).pack(side="left")
        self.key_entry = tk.Entry(keys, width=13, bg=COLORS["bg"], fg=COLORS["fg"],
                                  font=mono(9), relief="flat",
                                  insertbackground=COLORS["accent"],
                                  highlightthickness=1,
                                  highlightbackground=COLORS["line"])
        self.key_entry.insert(0, DEFAULT_KEY.hex().upper())
        self.key_entry.pack(side="left", padx=(4, 0))
        self.button(left, "기본 키로 찾기", self.find_keys).pack(fill="x", pady=(4, 0))

        right = tk.Frame(middle, bg=COLORS["panel"])
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.block_title = tk.Label(right, text="블록", bg=COLORS["panel"],
                                    fg=COLORS["fg"], font=mono(10, "bold"), anchor="w")
        self.block_title.pack(fill="x")
        self.hex = HexView(right, editable=True, height=11)
        self.hex.pack(fill="both", expand=True, pady=(4, 0))

    def _build_access(self) -> None:
        panel = tk.Frame(self, bg=COLORS["panel"])
        panel.pack(fill="x", padx=12, pady=(8, 0))

        left = tk.Frame(panel, bg=COLORS["panel"])
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="접근 조건 (트레일러가 정하는 것)", bg=COLORS["panel"],
                 fg=COLORS["fg"], font=mono(10, "bold"), anchor="w").pack(fill="x")
        self.access = tk.Text(left, bg=COLORS["bg"], fg=COLORS["fg"], font=mono(9),
                              height=6, relief="flat", padx=10, pady=6, wrap="none",
                              state="disabled")
        self.access.pack(fill="both", expand=True, pady=(4, 0))
        self.access.tag_configure("head", foreground=COLORS["dim"])
        self.access.tag_configure("never", foreground=COLORS["danger"])
        self.access.tag_configure("open", foreground=COLORS["ok"])
        self.access.tag_configure("warn", foreground=COLORS["warn"])

        right = tk.Frame(panel, bg=COLORS["panel"])
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        tk.Label(right, text="NDEF", bg=COLORS["panel"], fg=COLORS["fg"],
                 font=mono(10, "bold"), anchor="w").pack(fill="x")
        self.ndef = tk.Text(right, bg=COLORS["bg"], fg=COLORS["cyan"], font=mono(9),
                            height=6, relief="flat", padx=10, pady=6, wrap="word",
                            state="disabled")
        self.ndef.pack(fill="both", expand=True, pady=(4, 0))

    def _build_actions(self) -> None:
        actions = self.row()
        self.button(actions, "바뀐 블록 쓰기", self.write_changes,
                    danger=True).pack(side="left", padx=(0, 4))
        self.button(actions, "다시 읽기", self.reload_sector).pack(side="left", padx=4)
        self.info = tk.Label(actions, text="", bg=COLORS["panel"], fg=COLORS["dim"],
                             font=mono(9))
        self.info.pack(side="right")

    # ==================================================================
    # reader
    # ==================================================================

    def _open_reader(self) -> None:
        from ..backends.nfc import open_reader, probe_all

        found = [r for r in probe_all(spi=True, i2c=True, uart=False) if r.found]
        reader = next((r for r in found if r.where == self.device.address), None)
        reader = reader or (found[0] if found else None)
        if reader is None:
            self.reader_label.configure(
                text="리더가 대답하지 않습니다 — updev nfc wiring 로 배선을 확인하세요. "
                     "덤프 파일은 리더 없이도 열 수 있습니다.",
                fg=COLORS["danger"])
            return
        try:
            self.chip, self._close = open_reader(reader)
            self.reader = reader
        except OSError as e:
            self.reader_label.configure(text=f"{reader.where}: {e.strerror or e}",
                                        fg=COLORS["danger"])
            return
        self.reader_label.configure(
            text=f"{reader.module} · {reader.where} · {reader.detail}", fg=COLORS["ok"])

    def close(self) -> None:
        super().close()
        if isinstance(self.chip, Mfrc522):
            try:
                self.chip.stop_crypto()
                self.chip.halt()
            except Exception:
                pass
        if self._close:
            try:
                self._close()
            except Exception:
                pass
        self.chip = None

    # ==================================================================
    # reading the card
    # ==================================================================

    def _poll(self) -> bool:
        if self.chip is None:
            messagebox.showinfo("리더 없음",
                                "리더가 없습니다. 덤프 파일은 열 수 있습니다.",
                                parent=self)
            return False
        try:
            self.tag = self.chip.poll()
        except NfcError as e:
            self.tag_label.configure(text=f"카드 없음 — {e}", fg=COLORS["warn"])
            return False
        self.layout = layout_for_sak(self.tag.sak)
        label = f"UID {self.tag.uid_hex}   ·   {self.tag.kind}"
        if self.layout is None:
            self.tag_label.configure(
                text=label + "   — 블록 구조가 없는 카드라 편집할 수 없습니다",
                fg=COLORS["warn"])
            return False
        self.tag_label.configure(text=label, fg=COLORS["ok"])
        return True

    def read_card(self) -> None:
        """Every sector, with whichever key opens it. This is the slow one, so
        it reports progress rather than freezing with no explanation."""
        if not self._poll():
            return
        self.blocks, self.original = {}, {}
        self.sector_keys, self.sector_error = {}, {}

        if not self.layout.classic:
            self._read_ultralight()
        else:
            self._read_classic()

        self.source = f"카드 {self.tag.uid_hex}"
        self._refresh_sectors()
        self._update_ndef()
        opened = len(self.sector_keys)
        total = self.layout.sectors or 1
        self.status(f"{self.source} — {opened}/{total} 섹터 열림, "
                    f"{len(self.blocks)} 블록", "ok" if opened else "warn")

    def _read_classic(self) -> None:
        key = self._key_bytes() or DEFAULT_KEY
        for sector in range(self.layout.sectors):
            self.status(f"섹터 {sector}/{self.layout.sectors} 읽는 중…", "cyan")
            self.update_idletasks()
            used, error = None, ""
            for block in self.layout.blocks_in(sector):
                try:
                    data = self._read_block(block, used or key)
                    used = used or key
                except NfcError as e:
                    error = str(e)
                    # A failed authentication kills the crypto session, so the
                    # card has to be re-selected before the next attempt.
                    self._reselect()
                    break
                self.blocks[block] = data
                self.original[block] = data
            if used and not error:
                self.sector_keys[sector] = used
            elif error:
                self.sector_error[sector] = error

    def _read_ultralight(self) -> None:
        """Ultralight and NTAG have no sectors and no keys — just pages.

        READ hands back four pages at a time and wraps at the end of memory, so
        the loop stops when it sees the wrap rather than trusting a page count
        that varies by product.
        """
        seen: set[bytes] = set()
        for page in range(0, 64, 4):
            try:
                chunk = self.chip.read_page(page)
            except (NfcError, AttributeError):
                break
            if chunk in seen and page > 8:
                break                              # memory wrapped
            seen.add(chunk)
            self.blocks[page] = chunk
            self.original[page] = chunk
        self.sector_keys = {0: b""}

    def _reselect(self) -> None:
        if isinstance(self.chip, Mfrc522):
            try:
                self.chip.stop_crypto()
                self.chip.poll(wake=True)
            except NfcError:
                pass

    def _read_block(self, block: int, key: bytes) -> bytes:
        if isinstance(self.chip, Mfrc522):
            self.chip.authenticate(block, self.tag.uid, key)
            return self.chip.read_block(block)
        return self.chip.read_block(block, self.tag.uid, key)

    def reload_sector(self) -> None:
        if self.tag is None or self.layout is None or not self.layout.classic:
            self.read_card()
            return
        key = self.sector_keys.get(self.sector) or self._key_bytes() or DEFAULT_KEY
        for block in self.layout.blocks_in(self.sector):
            try:
                data = self._read_block(block, key)
            except NfcError as e:
                self.sector_error[self.sector] = str(e)
                self._reselect()
                break
            self.blocks[block] = data
            self.original[block] = data
        self._show_sector(self.sector)

    def find_keys(self) -> None:
        """Try the published default keys, sector by sector.

        This finds cards that were never keyed — which is most of them. It is
        not a recovery tool: nothing here searches beyond the list of keys the
        specs and vendors publish.
        """
        if not self._poll() or not self.layout.classic:
            return
        found = {}
        for sector in range(self.layout.sectors):
            first = self.layout.blocks_in(sector)[0]
            for name, key in DEFAULT_KEYS:
                self.status(f"섹터 {sector} — {name} 시도 중…", "cyan")
                self.update_idletasks()
                try:
                    self._read_block(first, key)
                except NfcError:
                    self._reselect()
                    continue
                found[sector] = (name, key)
                self.sector_keys[sector] = key
                break
        self._refresh_sectors()
        if found:
            names = {name for name, _ in found.values()}
            self.status(f"{len(found)}/{self.layout.sectors} 섹터가 기본 키로 열림 "
                        f"({', '.join(sorted(names))})", "ok")
        else:
            self.status("기본 키로는 아무 섹터도 열리지 않았습니다 — 키가 바뀐 "
                        "카드입니다", "warn")

    # ==================================================================
    # dump files
    # ==================================================================

    def open_dump(self) -> None:
        """A .mfd is a flat dump: every block in order, no header.

        Opening one needs no reader and no card, which is the point — the same
        way the floppy editor opens an .img without a drive attached.
        """
        path = filedialog.askopenfilename(
            title="덤프 열기", filetypes=[("MIFARE 덤프", "*.mfd *.dump *.bin"),
                                          ("전부", "*.*")], parent=self)
        if not path:
            return
        data = Path(path).read_bytes()
        layout = _layout_for_size(len(data))
        if layout is None:
            messagebox.showerror(
                "덤프 열기",
                f"{len(data):,} 바이트는 아는 카드 크기가 아닙니다.\n\n"
                f"1K=1024, 2K=2048, 4K=4096 바이트입니다.", parent=self)
            return
        self.layout = layout
        self.blocks, self.original = {}, {}
        for index in range(layout.blocks):
            chunk = data[index * 16:(index + 1) * 16]
            if len(chunk) < 16:
                break
            self.blocks[index] = chunk
            self.original[index] = chunk
        self.sector_keys = {s: DEFAULT_KEY for s in range(layout.sectors)}
        self.sector_error = {}
        self.source = f"덤프 {Path(path).name}"
        self._refresh_sectors()
        self._update_ndef()
        self.status(f"{path} — {layout.name}, {len(self.blocks)} 블록", "ok")

    def save_dump(self) -> None:
        if not self.blocks:
            messagebox.showinfo("덤프 저장", "먼저 카드를 읽으세요", parent=self)
            return
        path = filedialog.asksaveasfilename(
            title="덤프 저장", defaultextension=".mfd",
            initialfile=f"{(self.tag.uid_hex.replace(':', '') if self.tag else 'dump')}.mfd",
            filetypes=[("MIFARE 덤프", "*.mfd")], parent=self)
        if not path:
            return
        size = (self.layout.blocks if self.layout else max(self.blocks) + 1)
        blob = bytearray(size * 16)
        for block, payload in self.blocks.items():
            blob[block * 16:block * 16 + len(payload)] = payload
        Path(path).write_bytes(bytes(blob))
        self.status(f"wrote {path} ({len(blob):,} bytes)", "ok")

    # ==================================================================
    # views
    # ==================================================================

    def _refresh_sectors(self) -> None:
        self.sector_list.delete(0, "end")
        self.source_label.configure(text=self.source)
        if self.layout is None:
            return
        if not self.layout.classic:
            self.sector_list.insert("end", "  pages 0-…")
            self.sector_list.itemconfig("end", foreground=COLORS["ok"])
            self.sector_list.selection_set(0)
            self._show_pages()
            return
        for sector in range(self.layout.sectors):
            if sector in self.sector_keys:
                mark, colour = "●", COLORS["ok"]
            elif sector in self.sector_error:
                mark, colour = "✕", COLORS["danger"]
            else:
                mark, colour = "·", COLORS["dim"]
            self.sector_list.insert("end", f" {mark} 섹터 {sector:>2}")
            self.sector_list.itemconfig("end", foreground=colour)
        if self.sector < self.sector_list.size():
            self.sector_list.selection_clear(0, "end")
            self.sector_list.selection_set(self.sector)
            self._show_sector(self.sector)

    def _select_sector(self, _event=None) -> None:
        selection = self.sector_list.curselection()
        if not selection or self.layout is None:
            return
        if not self.layout.classic:
            self._show_pages()
            return
        self.sector = selection[0]
        self._show_sector(self.sector)

    def _show_sector(self, sector: int) -> None:
        blocks = self.layout.blocks_in(sector)
        present = [b for b in blocks if b in self.blocks]
        if not present:
            self.hex.show(b"")
            self.block_title.configure(
                text=f"섹터 {sector} — {self.sector_error.get(sector, '아직 안 읽음')}")
            self._update_access(sector)
            return
        first = present[0]
        blob = b"".join(self.blocks[b] for b in present)
        notes = {}
        for index, block in enumerate(present):
            role = "트레일러" if self.layout.is_trailer(block) else (
                "제조사 블록" if block == 0 else f"그룹 {group_of(self.layout, block)}")
            notes[first * 16 + index * 16] = f"blk {block}  {role}"
        self.hex.show(blob, base=first * 16, notes=notes)
        key = self.sector_keys.get(sector)
        self.block_title.configure(
            text=f"섹터 {sector}   블록 {present[0]}-{present[-1]}" +
                 (f"   key {key.hex().upper()}" if key else ""))
        self._update_access(sector)

    def _show_pages(self) -> None:
        blob = b"".join(self.blocks[p] for p in sorted(self.blocks))
        notes = {p * 4: f"page {p}-{p + 3}" for p in sorted(self.blocks)}
        self.hex.show(blob, base=0, notes=notes)
        self.block_title.configure(text=f"Ultralight / NTAG — {len(blob)} 바이트")
        self._write_access([
            ("head", "Ultralight/NTAG에는 섹터도 키도 없습니다.\n"),
            ("", "페이지 4부터가 사용자 영역입니다.\n"),
            ("never", "0-1 UID · 2 lock bits · 3 OTP — 전부 쓰기 금지\n"),
        ])

    def _update_access(self, sector: int) -> None:
        trailer_block = self.layout.trailer_of(sector)
        trailer = self.blocks.get(trailer_block)
        if trailer is None:
            self._write_access([("head", "트레일러를 아직 못 읽었습니다.\n")])
            return
        bits = decode_access_bits(trailer)
        lines: list[tuple[str, str]] = []
        if not bits.valid:
            lines.append(("never", "⚠ 접근 비트가 반전 사본과 안 맞습니다 — 카드가 "
                                   "이 섹터를 거부할 수 있습니다\n"))
        lines.append(("head", f"C1C2C3  {[bits.triple(g) for g in range(4)]}   "
                              f"raw {bits.raw.hex().upper()}\n"))
        for block in self.layout.blocks_in(sector):
            perms = describe_permissions(self.layout, block, trailer)
            if self.layout.is_trailer(block):
                text = (f"blk {block:>3} 트레일러  keyA쓰기 {perms['key_a_write']}  "
                        f"접근비트 읽기 {perms['access_read']}/쓰기 "
                        f"{perms['access_write']}  keyB쓰기 {perms['key_b_write']}\n")
            else:
                text = (f"blk {block:>3}          읽기 {perms['read']:<4} "
                        f"쓰기 {perms['write']:<4} 증가 {perms['increment']:<4} "
                        f"감소 {perms['decrement']}\n")
            tag = "never" if "—" == perms.get("write", perms.get("access_write")) else "open"
            lines.append((tag, text))
        self._write_access(lines)

    def _write_access(self, lines: list[tuple[str, str]]) -> None:
        self.access.configure(state="normal")
        self.access.delete("1.0", "end")
        for tag, text in lines:
            self.access.insert("end", text, tag)
        self.access.configure(state="disabled")

    def _update_ndef(self) -> None:
        blob = b"".join(self.blocks[b] for b in sorted(self.blocks))
        records = parse_ndef(blob)
        self.ndef.configure(state="normal")
        self.ndef.delete("1.0", "end")
        if not records:
            self.ndef.insert("end", "NDEF 메시지가 없습니다 (또는 아직 안 읽은 "
                                    "섹터에 있습니다).")
        for record in records:
            self.ndef.insert("end", f"{record.label}\n  {record.text}\n\n")
        self.ndef.configure(state="disabled")

    # ==================================================================
    # writing
    # ==================================================================

    def _key_bytes(self) -> bytes | None:
        raw = self.key_entry.get().replace(":", "").replace(" ", "").strip()
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            return None
        return key if len(key) == 6 else None

    def write_changes(self) -> None:
        if self.layout is None or not self.blocks:
            messagebox.showinfo("쓰기", "먼저 카드를 읽으세요", parent=self)
            return
        if self.chip is None or self.tag is None:
            messagebox.showinfo("쓰기", "쓰려면 카드가 리더에 올라와 있어야 합니다 — "
                                       "[카드 전체 읽기] 를 먼저 누르세요", parent=self)
            return

        changed = self._changed_blocks()
        if not changed:
            messagebox.showinfo("쓰기", "바뀐 블록이 없습니다", parent=self)
            return

        trailers = [b for b, _ in changed if self.layout.classic and is_trailer(b)]
        detail = "\n".join(
            f"  blk {b:>3}   {' '.join(f'{x:02X}' for x in payload[:8])} …   "
            f"|{ascii_dump(payload)}|" for b, payload in changed)
        if not confirm_write(
            self, "카드에 쓰기",
            f"블록 {len(changed)}개를 UID {self.tag.uid_hex} 카드에 씁니다.",
            detail,
            danger=("섹터 트레일러가 포함돼 있습니다 — 접근 비트를 잘못 쓰면 그 섹터는 "
                    "영구히 잠깁니다." if trailers else ""),
        ):
            return
        if trailers and not messagebox.askyesno(
            "트레일러 확인",
            f"블록 {trailers} 은 키와 접근 비트가 들어있는 섹터 트레일러입니다.\n\n"
            f"잘못된 접근 비트 조합은 되돌릴 방법이 없습니다. 정말 쓸까요?",
            default="no", icon="warning", parent=self):
            return

        written, failed = [], []
        for block, payload in changed:
            try:
                self._write_block(block, payload, allow_trailer=block in trailers)
                self.blocks[block] = payload
                self.original[block] = payload
                written.append(block)
            except (NfcError, ValueError) as e:
                failed.append(f"blk {block}: {e}")
                self._reselect()

        if written:
            self.hex.mark_clean()
        message = f"{len(written)} block(s) written"
        if failed:
            self.status(message + " · " + " · ".join(failed), "danger")
            messagebox.showerror("쓰기 실패", "\n".join(failed), parent=self)
        else:
            self.status(message, "ok")
        self._update_ndef()

    def _changed_blocks(self) -> list[tuple[int, bytes]]:
        """Diff the hex view against what was read, in block-sized pieces."""
        edited = bytes(self.hex.data)
        if not self.layout.classic:
            present = sorted(self.blocks)
            step = 16
        else:
            present = [b for b in self.layout.blocks_in(self.sector) if b in self.blocks]
            step = 16
        out = []
        for index, block in enumerate(present):
            payload = edited[index * step:(index + 1) * step]
            if len(payload) == step and payload != self.original.get(block):
                out.append((block, payload))
        return out

    def _write_block(self, block: int, payload: bytes, allow_trailer: bool) -> None:
        if not self.layout.classic:
            # The hex view holds four pages per row; write them one page at a
            # time, because that is the unit the card accepts.
            for offset in range(0, 16, 4):
                self.chip.write_page(block + offset // 4, payload[offset:offset + 4])
            return
        key = self.sector_keys.get(self.layout.sector_of(block)) or \
            self._key_bytes() or DEFAULT_KEY
        if isinstance(self.chip, Mfrc522):
            self.chip.authenticate(block, self.tag.uid, key)
            self.chip.write_block(block, payload, allow_trailer=allow_trailer)
            return
        self.chip.write_block(block, self.tag.uid, payload, key,
                              allow_trailer=allow_trailer)


def _layout_for_size(size: int) -> CardLayout | None:
    """A dump file has no SAK, so the size is the only clue to the geometry."""
    return {1024: layout_for_sak(0x08), 2048: layout_for_sak(0x19),
            4096: layout_for_sak(0x18), 320: layout_for_sak(0x09)}.get(size)
