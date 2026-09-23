"""USB panels — one per kind of USB device, plus two that fit them all.

The two universal ones come first because they are the ones you reach for when
a device misbehaves:

  * **정체** — the role verdict and the storage classification with their full
    evidence tables. The same reasoning `updev usb zone` prints, in a window,
    so a surprising verdict can be argued with here too.
  * **디스크립터** — the descriptor tree. sysfs summarises a device; the
    descriptor *is* the device, including the alternate settings sysfs hides
    and the isochronous endpoints that prove real streaming hardware.

Then the per-role ones: a camera you can take a picture with, a serial port
you can watch, a network adapter you can sweep a subnet from, a hub whose
ports you can see.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

from ..core.util import have
from .base import COLORS, Editor, mono, notice


class _TextPanel(Editor):
    """Shared scaffolding for the panels that mostly render text."""

    def text_area(self, height: int = 20) -> tk.Text:
        frame = tk.Frame(self, bg=COLORS["panel"])
        frame.pack(fill="both", expand=True, padx=12, pady=(8, 4))
        widget = tk.Text(frame, bg=COLORS["bg"], fg=COLORS["fg"], font=mono(10),
                         relief="flat", padx=12, pady=10, wrap="none",
                         height=height, state="disabled")
        scroll = tk.Scrollbar(frame, command=widget.yview, bg=COLORS["panel"],
                              troughcolor=COLORS["bg"], relief="flat",
                              highlightthickness=0, width=10)
        widget.configure(yscrollcommand=scroll.set)
        widget.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for name, colour in (("head", "accent"), ("key", "dim"), ("ok", "ok"),
                             ("warn", "warn"), ("bad", "danger"), ("cyan", "cyan")):
            widget.tag_configure(name, foreground=COLORS[colour])
        widget.tag_configure("bold", font=mono(10, "bold"))
        return widget

    @staticmethod
    def write(widget: tk.Text, chunks) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for tag, text in chunks:
            widget.insert("end", text, tag)
        widget.configure(state="disabled")


# ==========================================================================
# universal
# ==========================================================================

class IdentityPanel(_TextPanel):
    TITLE = "정체"
    SUBTITLE = "무슨 장치인지, 그리고 왜 그렇게 판단했는지"
    WRITES = False

    def build(self) -> None:
        bar = self.row()
        self.button(bar, "다시 판정", self.refresh).pack(side="left")
        self.badge = tk.Label(bar, text="", bg=COLORS["panel"], fg=COLORS["cyan"],
                              font=mono(10, "bold"))
        self.badge.pack(side="right")
        self.body = self.text_area(height=22)
        notice(self, "커널이 실제로 바인딩한 것이 장치가 스스로 주장하는 디스크립터를 "
                     "이깁니다. 두 근거를 출처와 함께 나눠서 보여줍니다.")
        self.refresh()

    def refresh(self) -> None:
        from ..toolkit import annotate, recognize
        from ..usbclass import CLASS_LABEL

        recognition = recognize(self.device)
        annotate(recognition.tools)
        self.badge.configure(text=recognition.badge)

        chunks: list[tuple[str, str]] = [
            ("head", f"{self.device.label}\n"),
            ("key", f"{self.device.address}   {self.device.summary}\n\n"),
        ]

        roles = recognition.roles
        if roles and roles.roles:
            chunks.append(("bold", f"역할   {roles.label}\n"))
            for item in roles.evidence:
                mark = "!! " if item.definitive else "   "
                chunks.append(("ok" if item.definitive else "key",
                               f"{mark}[{item.source}] {item.observed} → {item.role}\n"))
                chunks.append(("key", f"      {item.reason}\n"))
            chunks.append(("", "\n"))

        storage = recognition.storage
        if storage is not None:
            chunks.append(("bold", f"저장장치 분류   {storage.usb_class}  "
                                   f"{CLASS_LABEL.get(storage.usb_class, '')}\n"))
            chunks.append(("key", f"신뢰도 {storage.confidence}   margin "
                                  f"{storage.margin}\n"))
            for item in storage.evidence:
                mark = "!! " if item.decisive else "   "
                scores = "  ".join(f"{cls}{score:+d}"
                                   for cls, score in sorted(item.scores.items(),
                                                            key=lambda kv: -kv[1])
                                   if score)
                chunks.append(("ok" if item.decisive else "key",
                               f"{mark}{item.signature}: {item.observed}   {scores}\n"))
                chunks.append(("key", f"      {item.reason}\n"))
            chunks.append(("", "\n"))

        if recognition.nodes:
            chunks.append(("bold", "/dev 노드\n"))
            for subsystem, names in sorted(recognition.nodes.items()):
                chunks.append(("key", f"  {subsystem:>12}  "))
                chunks.append(("", f"{', '.join(names)}\n"))
            chunks.append(("", "\n"))

        if recognition.tools:
            chunks.append(("bold", "할 수 있는 것\n"))
            for tool in recognition.tools:
                chunks.append(("ok" if tool.lead else "", 
                               f"  {'★' if tool.lead else ' '} {tool.title}\n"))
                chunks.append(("cyan", f"      {tool.command}\n"))
        self.write(self.body, chunks)
        self.status(f"{recognition.badge} — {recognition.headline}", "ok")


class DescriptorPanel(_TextPanel):
    TITLE = "디스크립터"
    SUBTITLE = "설정 · 인터페이스 · alt setting · 엔드포인트 — 장치가 말하는 그대로"
    WRITES = False

    def build(self) -> None:
        bar = self.row()
        self.button(bar, "다시 읽기", self.refresh).pack(side="left")
        self.button(bar, "원시 바이트 저장…", self.save_raw).pack(side="left", padx=6)
        self.source_label = tk.Label(bar, text="", bg=COLORS["panel"],
                                     fg=COLORS["dim"], font=mono(9))
        self.source_label.pack(side="right")
        self.body = self.text_area(height=22)
        notice(self, "sysfs는 활성 alt setting 하나만 보여줍니다. 여기에는 전부 "
                     "나옵니다 — 외장SSD의 alt 0 = BOT, alt 1 = UAS 같은 게 "
                     "그렇게 드러납니다.")
        self.blob = b""
        self.refresh()

    def refresh(self) -> None:
        from ..usbdesc import descriptor_hints, parse_descriptors, read_raw_descriptors

        try:
            blob, source, _ = read_raw_descriptors(self.device.address)
        except OSError as e:
            self.write(self.body, [("bad", f"디스크립터를 읽을 수 없습니다: {e}\n")])
            return
        self.blob = blob
        tree = parse_descriptors(blob, source)
        self.source_label.configure(text=f"{source}  ·  {len(blob)} bytes")

        chunks: list[tuple[str, str]] = []
        if tree.error:
            chunks.append(("bad", f"{tree.error}\n\n"))
        device = tree.device
        if device is not None:
            chunks.append(("head", "device\n"))
            for key, value in device.as_dict().items():
                chunks.append(("key", f"  {key:>18}  "))
                chunks.append(("", f"{value}\n"))
            chunks.append(("", "\n"))

        for config in tree.configurations:
            chunks.append(("head", f"configuration {config.value}\n"))
            chunks.append(("key", f"  {config.num_interfaces} interfaces · "
                                  f"{config.max_power_ma} mA · "
                                  f"{'self' if config.self_powered else 'bus'}-powered"
                                  f"{' · remote wakeup' if config.remote_wakeup else ''}\n"))
            for assoc in config.associations:
                chunks.append(("cyan", f"  ⇥ association: interfaces "
                                       f"{assoc.first_interface}.."
                                       f"{assoc.first_interface + assoc.interface_count - 1}"
                                       f" are one function\n"))
            for iface in config.interfaces:
                chunks.append(("bold", f"  interface {iface.number}.{iface.alternate}"))
                chunks.append(("", f"   {iface.triple}\n"))
                for endpoint in iface.endpoints:
                    line = (f"      EP{endpoint.number} {endpoint.direction} "
                            f"{endpoint.transfer_type} {endpoint.packet_size}B "
                            f"interval {endpoint.interval}")
                    if endpoint.transactions_per_microframe > 1:
                        line += f" ×{endpoint.transactions_per_microframe}"
                    if endpoint.burst is not None:
                        line += f" burst {endpoint.burst}"
                    chunks.append(("ok" if endpoint.transfer_type == "isochronous"
                                   else "", line + "\n"))
                for extra in iface.class_specific:
                    chunks.append(("cyan", f"      · {extra}\n"))
            chunks.append(("", "\n"))

        hints = descriptor_hints(tree)
        if hints:
            chunks.append(("head", "디스크립터가 말해주는 것\n"))
            for hint in hints:
                chunks.append(("warn", f"  • {hint}\n"))
        self.write(self.body, chunks)
        self.status(f"{len(blob)} bytes of descriptor from {source}", "ok")

    def save_raw(self) -> None:
        if not self.blob:
            return
        path = filedialog.asksaveasfilename(
            title="원시 디스크립터 저장", defaultextension=".bin",
            initialfile=f"descriptors-{self.device.address}.bin", parent=self)
        if path:
            Path(path).write_bytes(self.blob)
            self.status(f"wrote {path} ({len(self.blob)} bytes)", "ok")


class PathPanel(_TextPanel):
    TITLE = "경로 · 병목"
    SUBTITLE = "루트허브부터 이 장치까지, 그리고 어디서 느려졌는지"
    WRITES = False

    def build(self) -> None:
        self.body = self.text_area(height=14)
        notice(self, "판정 기준은 선언한 규격(bcdUSB) 대비 실제 협상 속도입니다. "
                     "원래 느린 장치는 경고하지 않습니다.")
        self.refresh()

    def refresh(self) -> None:
        from ..usbrole import build_path, device_nodes, path_bottleneck

        hops = build_path(self.device.address)
        bottleneck = path_bottleneck(hops)
        slow_at = bottleneck[0] if bottleneck else None

        chunks: list[tuple[str, str]] = [("head", "Raspberry Pi (host)\n")]
        for index, hop in enumerate(hops):
            indent = "  " * (index + 1)
            arrow = "└─ "
            line = f"{indent}{arrow}{hop.label or hop.address}"
            chunks.append(("bold" if hop.is_target else "", line))
            detail = f"   {hop.address}  {hop.speed_label}"
            if hop.is_root_hub:
                detail += "  root hub"
            elif hop.port is not None:
                detail += f"  port {hop.port}"
            chunks.append(("key", detail))
            chunks.append(("bad", "  ◀ 병목\n") if index == slow_at else ("", "\n"))
        if bottleneck:
            chunks.append(("warn", f"\n{bottleneck[1]}\n"))
        else:
            chunks.append(("ok", "\n이 경로에서 속도를 깎아먹는 홉은 없습니다.\n"))

        nodes = device_nodes(self.device.address)
        if nodes:
            chunks.append(("head", "\n/dev 노드\n"))
            for subsystem, names in sorted(nodes.items()):
                chunks.append(("key", f"  {subsystem:>12}  "))
                chunks.append(("", f"{', '.join(names)}\n"))
        self.write(self.body, chunks)


# ==========================================================================
# per role
# ==========================================================================

class CameraPanel(Editor):
    TITLE = "카메라"
    SUBTITLE = "지원 포맷 · 한 장 찍어서 바로 보기"
    WRITES = False

    def build(self) -> None:
        from ..usbrole import device_nodes

        nodes = device_nodes(self.device.address).get("video4linux") or []
        self.node = nodes[0] if nodes else ""
        self.image = None

        bar = self.row()
        tk.Label(bar, text=self.node or "video 노드 없음", bg=COLORS["panel"],
                 fg=COLORS["cyan"], font=mono(10, "bold")).pack(side="left")
        self.button(bar, "포맷 목록", self.list_formats).pack(side="right", padx=3)
        self.button(bar, "한 장 찍기", self.capture).pack(side="right", padx=3)

        self.preview = tk.Label(self, bg=COLORS["bg"], fg=COLORS["dim"],
                                font=mono(10), text="[한 장 찍기] 를 누르면 여기에 "
                                                    "나옵니다")
        self.preview.pack(fill="both", expand=True, padx=12, pady=(8, 4))
        self.formats = tk.Text(self, bg=COLORS["bg"], fg=COLORS["fg"], font=mono(9),
                               height=8, relief="flat", padx=10, pady=6, wrap="none",
                               state="disabled")
        self.formats.pack(fill="x", padx=12, pady=(0, 4))
        notice(self, "ffmpeg으로 한 프레임만 받아옵니다 — 스트리밍은 camtoy 쪽이 "
                     "합니다 (python3 -m camtoy live).")

    def list_formats(self) -> None:
        if not have("v4l2-ctl"):
            self.status("v4l-utils 가 없습니다: sudo apt install v4l-utils", "warn")
            return
        try:
            out = subprocess.run(
                ["v4l2-ctl", f"--device={self.node}", "--list-formats-ext"],
                capture_output=True, text=True, timeout=6).stdout
        except (OSError, subprocess.SubprocessError) as e:
            self.status(str(e), "danger")
            return
        self.formats.configure(state="normal")
        self.formats.delete("1.0", "end")
        self.formats.insert("1.0", out.strip() or "포맷을 못 읽었습니다")
        self.formats.configure(state="disabled")

    def capture(self) -> None:
        """One frame through ffmpeg, which is the same path camtoy uses."""
        if not self.node:
            return
        if not have("ffmpeg"):
            self.status("ffmpeg 이 없습니다: sudo apt install ffmpeg", "warn")
            return
        target = Path("/tmp") / f"updev-capture-{self.device.address}.jpg"
        self.status("찍는 중…", "cyan")
        self.update_idletasks()
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "v4l2",
                 "-i", self.node, "-frames:v", "1", str(target)],
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            self.status(f"ffmpeg: {e}", "danger")
            return
        if result.returncode != 0 or not target.exists():
            self.status(f"ffmpeg: {result.stderr.strip()[:120]}", "danger")
            return
        self._show(target)

    def _show(self, path: Path) -> None:
        try:
            from PIL import Image, ImageTk
        except ImportError:
            self.preview.configure(text=f"저장됨: {path}\n(Pillow가 있으면 여기 "
                                        f"바로 보입니다)")
            self.status(f"wrote {path}", "ok")
            return
        picture = Image.open(path)
        picture.thumbnail((720, 420))
        self.image = ImageTk.PhotoImage(picture)      # kept, or Tk drops it
        self.preview.configure(image=self.image, text="")
        self.status(f"{path}  ·  {picture.width}×{picture.height}", "ok")


class SerialPanel(Editor):
    TITLE = "시리얼 터미널"
    SUBTITLE = "포트를 열어서 오는 것을 보고, 한 줄 보낸다"
    WRITES = True

    BAUDS = (300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400,
             460800, 921600)

    def build(self) -> None:
        from ..usbrole import device_nodes

        nodes = device_nodes(self.device.address).get("tty") or []
        self.node = nodes[0] if nodes else (self.device.node or "")
        self.conn = None
        self.queue: queue.Queue = queue.Queue()
        self.stop = threading.Event()

        bar = self.row()
        tk.Label(bar, text=self.node or "tty 없음", bg=COLORS["panel"],
                 fg=COLORS["cyan"], font=mono(10, "bold")).pack(side="left")
        tk.Label(bar, text="  baud", bg=COLORS["panel"], fg=COLORS["dim"],
                 font=mono(10)).pack(side="left", padx=(12, 4))
        self.baud = tk.Spinbox(bar, values=self.BAUDS, width=8, bg=COLORS["bg"],
                               fg=COLORS["fg"], font=mono(10), relief="flat",
                               buttonbackground=COLORS["raised"],
                               highlightthickness=1,
                               highlightbackground=COLORS["line"])
        while self.baud.get() != "115200":
            self.baud.invoke("buttonup")
            if self.baud.get() == str(self.BAUDS[-1]):
                break
        self.baud.pack(side="left")
        self.hex_mode = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="hex", variable=self.hex_mode, bg=COLORS["panel"],
                       fg=COLORS["fg"], selectcolor=COLORS["bg"], font=mono(9),
                       activebackground=COLORS["panel"],
                       activeforeground=COLORS["fg"]).pack(side="left", padx=8)
        self.toggle = self.button(bar, "열기", self.open_port)
        self.toggle.pack(side="left", padx=4)

        self.log = tk.Text(self, bg=COLORS["bg"], fg=COLORS["fg"], font=mono(10),
                           relief="flat", padx=10, pady=8, height=16,
                           state="disabled", wrap="char")
        self.log.pack(fill="both", expand=True, padx=12, pady=(8, 4))

        send = self.row()
        self.entry = tk.Entry(send, bg=COLORS["bg"], fg=COLORS["fg"], font=mono(10),
                              relief="flat", insertbackground=COLORS["accent"],
                              highlightthickness=1, highlightbackground=COLORS["line"])
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _e: self.send())
        self.button(send, "보내기", self.send, danger=True).pack(side="left", padx=(6, 0))
        notice(self, "보내기는 실제로 버스에 바이트를 씁니다 — 반대편이 뭘 하는지 "
                     "아는 상태에서만 쓰세요. 줄 끝에 \\r\\n 이 붙습니다.")

    def close(self) -> None:
        super().close()
        self.stop.set()
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def open_port(self) -> None:
        if self.conn is not None:
            self.close()
            self.toggle.configure(text="열기")
            self.status("닫힘", "dim")
            return
        try:
            import serial as pyserial
        except ImportError:
            self.status("python3-serial 이 없습니다", "danger")
            return
        try:
            self.conn = pyserial.Serial(self.node, int(self.baud.get()), timeout=0.3)
        except Exception as e:
            self.status(f"{self.node}: {e}", "danger")
            return
        self.stop.clear()
        threading.Thread(target=self._pump, daemon=True).start()
        self.every(120, self._drain)
        self.toggle.configure(text="닫기")
        self.status(f"{self.node} @ {self.baud.get()} baud", "ok")

    def _pump(self) -> None:
        while not self.stop.is_set() and self.conn is not None:
            try:
                data = self.conn.read(256)
            except Exception:
                return
            if data:
                self.queue.put(data)

    def _drain(self) -> None:
        wrote = False
        while True:
            try:
                data = self.queue.get_nowait()
            except queue.Empty:
                break
            text = (" ".join(f"{b:02x}" for b in data) + " "
                    if self.hex_mode.get() else data.decode("utf-8", "replace"))
            self.log.configure(state="normal")
            self.log.insert("end", text)
            self.log.configure(state="disabled")
            wrote = True
        if wrote:
            self.log.see("end")

    def send(self) -> None:
        if self.conn is None:
            self.status("포트가 안 열려 있습니다", "warn")
            return
        line = self.entry.get()
        try:
            self.conn.write(line.encode("utf-8", "replace") + b"\r\n")
        except Exception as e:
            self.status(f"보내기 실패: {e}", "danger")
            return
        self.entry.delete(0, "end")
        self.status(f"sent {len(line) + 2} bytes", "ok")


class NetworkPanel(_TextPanel):
    TITLE = "네트워크"
    SUBTITLE = "이 어댑터의 링크 상태, 그리고 닿는 곳"
    WRITES = False

    def build(self) -> None:
        from ..usbrole import device_nodes

        nodes = device_nodes(self.device.address).get("net") or []
        self.iface = nodes[0] if nodes else ""
        bar = self.row()
        tk.Label(bar, text=self.iface or "net 노드 없음", bg=COLORS["panel"],
                 fg=COLORS["cyan"], font=mono(10, "bold")).pack(side="left")
        self.button(bar, "서브넷 훑기", self.sweep).pack(side="right", padx=3)
        self.button(bar, "링크 정보", self.refresh).pack(side="right", padx=3)
        self.body = self.text_area(height=20)
        notice(self, "훑기는 능동적으로 패킷을 보냅니다 — 본인이 책임지는 "
                     "네트워크에서만 쓰세요. root 없이 ping + ARP 테이블만 씁니다.")
        self.refresh()

    def refresh(self) -> None:
        chunks: list[tuple[str, str]] = [("head", f"{self.device.label}\n")]
        for key, value in self.device.detail.items():
            chunks.append(("key", f"  {key:>18}  "))
            chunks.append(("", f"{value}\n"))
        for metric, value in self.device.metrics.items():
            chunks.append(("key", f"  {metric:>18}  "))
            chunks.append(("cyan", f"{value}\n"))
        self.write(self.body, chunks)

    def sweep(self) -> None:
        """Reuses the LAN backend rather than reimplementing a scanner."""
        self.status("서브넷 훑는 중…", "cyan")
        self.update_idletasks()

        def work():
            from ..core.registry import ProbeContext, build_scanner

            ctx = ProbeContext(deep=True, include=frozenset({"lan"}))
            result = build_scanner().scan(ctx)
            self.after(0, lambda: self._swept(result))

        threading.Thread(target=work, daemon=True).start()

    def _swept(self, result) -> None:
        from ..core.model import Kind

        hosts = [d for d in result.devices if d.kind == Kind.NET_HOST]
        chunks: list[tuple[str, str]] = [
            ("head", f"이웃 {len(hosts)}대  ·  {result.duration:.1f}s\n\n")]
        for host in hosts:
            chunks.append(("ok", f"  {host.address:<16}"))
            chunks.append(("", f"{host.label}\n"))
            if host.summary:
                chunks.append(("key", f"      {host.summary}\n"))
        self.write(self.body, chunks)
        self.status(f"{len(hosts)} neighbours", "ok")


class HubPanel(_TextPanel):
    TITLE = "허브 포트"
    SUBTITLE = "이 허브에 뭐가 걸려 있고, 각 포트가 뭘 협상했는지"
    WRITES = False

    def build(self) -> None:
        self.body = self.text_area(height=18)
        notice(self, "허브는 아래쪽 전부의 속도 상한을 정합니다. 느린 장치의 범인이 "
                     "잎이 아니라 허브인 경우가 대부분입니다.")
        self.refresh()

    def refresh(self) -> None:
        from ..core.registry import ProbeContext, build_scanner
        from ..usbrole import build_path

        result = build_scanner().scan(ProbeContext(include=frozenset({"usb"})))
        address = self.device.address
        children = [d for d in result.devices
                    if d.address.startswith(f"{address}.") and d.address != address]

        chunks: list[tuple[str, str]] = [
            ("head", f"{self.device.label}   {address}\n"),
            ("key", f"{self.device.summary}\n\n"),
        ]
        if not children:
            chunks.append(("key", "이 허브 아래에 아무것도 없습니다.\n"))
        for child in sorted(children, key=lambda d: d.address):
            port = child.address.rsplit(".", 1)[-1]
            chunks.append(("bold", f"  port {port}  "))
            chunks.append(("", f"{child.label}\n"))
            chunks.append(("key", f"          {child.address}  {child.summary}\n"))
            hops = build_path(child.address)
            target = next((h for h in hops if h.is_target), None)
            if target and target.declared_generation > target.generation:
                chunks.append(("warn", f"          declares USB {target.declared} "
                                       f"but negotiated {target.speed_label}\n"))
        self.write(self.body, chunks)
        self.status(f"{len(children)} device(s) behind this hub", "ok")
