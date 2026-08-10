"""Booting things in QEMU, and passing host USB through to the guest.

Two separate capabilities that happen to share a launcher:

  * **Boot an image.** Mostly the ASCII-art floppy, so you can see the boot
    sector run without finding a real machine. Headless by default, with a
    screenshot taken through the QEMU monitor.

  * **Attach a host USB device.** QEMU's `usb-host` detaches the device from
    the host kernel and hands it to the guest. That needs *write* access to
    `/dev/bus/usb/BBB/DDD`, which Debian ships as `crw-rw-r-- root:root` — so
    reading descriptors works unprivileged, but passthrough does not. We check
    and say so rather than failing halfway through.

Passthrough is genuinely dangerous in a way descriptor reading is not: the
host loses the device while the guest holds it. Handing over the disk your
root filesystem lives on, or the keyboard you are typing on, are both easy
mistakes, so both are checked for before QEMU is launched.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .core.util import read_text

__all__ = [
    "BootPlan",
    "PassthroughCheck",
    "check_passthrough",
    "find_qemu",
    "run_qemu",
    "udev_rule",
]

QEMU_CANDIDATES = ("qemu-system-i386", "qemu-system-x86_64")


def find_qemu() -> str:
    for name in QEMU_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return ""


# --------------------------------------------------------------------------
# passthrough safety
# --------------------------------------------------------------------------

@dataclass(slots=True)
class PassthroughCheck:
    address: str
    label: str = ""
    node: str = ""
    busnum: int = 0
    devnum: int = 0
    readable: bool = False
    writable: bool = False
    blockers: list[str] = field(default_factory=list)   # hard refusals
    warnings: list[str] = field(default_factory=list)   # need --force
    roles: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers and self.writable

    def as_dict(self) -> dict:
        return {
            "address": self.address, "label": self.label, "node": self.node,
            "busnum": self.busnum, "devnum": self.devnum,
            "readable": self.readable, "writable": self.writable,
            "blockers": self.blockers, "warnings": self.warnings,
            "roles": self.roles,
        }


def _mounted_sources() -> set[str]:
    """Block device names backing a mounted filesystem, e.g. {"sda1", "sda2"}."""
    out: set[str] = set()
    for line in read_text("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("/dev/"):
            continue
        out.add(Path(parts[0]).name)
    return out


def check_passthrough(address: str) -> PassthroughCheck:
    """Decide whether this device may be handed to a guest."""
    from .usbrole import _block_devices_for, gather_device_facts, identify

    base = Path("/sys/bus/usb/devices") / address
    check = PassthroughCheck(address=address)
    if not base.is_dir():
        check.blockers.append(f"no such USB device: {address}")
        return check

    busnum = read_text(base / "busnum")
    devnum = read_text(base / "devnum")
    try:
        check.busnum, check.devnum = int(busnum), int(devnum)
    except ValueError:
        check.blockers.append("could not read busnum/devnum")
        return check

    check.node = f"/dev/bus/usb/{check.busnum:03d}/{check.devnum:03d}"
    check.readable = os.access(check.node, os.R_OK)
    check.writable = os.access(check.node, os.W_OK)

    facts = gather_device_facts(address)
    check.label = " ".join(x for x in (facts.vendor, facts.product) if x) or address
    verdict = identify(facts)
    check.roles = [str(r) for r in verdict.roles]

    if any(i.cls == 0x09 for i in facts.interfaces):
        check.blockers.append(
            "this is a hub — passing it through would take every device behind it"
        )

    # The one that would actually ruin the afternoon.
    blocks = _block_devices_for(address)
    mounted = _mounted_sources()
    for name in blocks:
        touching = sorted(
            m for m in mounted if m == name or m.startswith(name)
        )
        if touching:
            check.blockers.append(
                f"/dev/{name} is backing a mounted filesystem ({', '.join(touching)}) — "
                "handing it to a guest would pull it out from under the host"
            )
    if blocks and not check.blockers:
        check.warnings.append(
            f"holds block device(s) {', '.join(blocks)} — unmount them first or the "
            "guest and host will both think they own the disk"
        )

    if {"keyboard", "mouse"} & set(check.roles):
        check.warnings.append(
            "this is an input device — the host loses it for as long as the guest "
            "holds it, so you may not be able to type"
        )

    if not check.writable:
        check.blockers.append(
            f"no write access to {check.node} — passthrough needs it "
            "(reading descriptors does not)"
        )
    return check


def udev_rule(vendor_id: str = "", product_id: str = "") -> str:
    """A udev rule granting the plugdev group write access to usbfs nodes."""
    match = 'SUBSYSTEM=="usb"'
    if vendor_id and product_id:
        match += f', ATTR{{idVendor}}=="{vendor_id}", ATTR{{idProduct}}=="{product_id}"'
    return (
        f'{match}, MODE="0660", GROUP="plugdev"'
    )


# --------------------------------------------------------------------------
# launching
# --------------------------------------------------------------------------

@dataclass(slots=True)
class BootPlan:
    qemu: str
    argv: list[str]
    monitor_path: str = ""
    screenshot: str = ""
    passthrough: list[PassthroughCheck] = field(default_factory=list)

    @property
    def command(self) -> str:
        return " ".join(self.argv)


def build_plan(
    floppy: str | None = None,
    disk: str | None = None,
    passthrough: list[PassthroughCheck] | None = None,
    memory: int = 128,
    display: str = "none",
    monitor_path: str = "",
    screenshot: str = "",
    extra: tuple[str, ...] = (),
) -> BootPlan:
    qemu = find_qemu()
    if not qemu:
        raise RuntimeError(
            "no QEMU found — install it with: sudo apt install qemu-system-x86"
        )

    argv = [qemu, "-m", str(memory), "-no-reboot"]
    if floppy:
        # Spell out format=raw: bare -fda makes QEMU probe, and it warns loudly
        # that guessing the format of a raw image is unsafe.
        argv += ["-drive", f"file={floppy},format=raw,if=floppy", "-boot", "a"]
    if disk:
        argv += ["-drive", f"file={disk},format=raw,if=ide"]
        if not floppy:
            argv += ["-boot", "c"]
    argv += ["-display", display] if display != "none" else ["-display", "none"]
    if monitor_path:
        argv += ["-monitor", f"unix:{monitor_path},server,nowait"]

    checks = passthrough or []
    if checks:
        # xHCI so SuperSpeed devices attach at their real speed.
        argv += ["-device", "qemu-xhci,id=xhci"]
        for check in checks:
            argv += [
                "-device",
                f"usb-host,bus=xhci.0,hostbus={check.busnum},"
                f"hostaddr={check.devnum},id=passthru{check.devnum}",
            ]
    argv += list(extra)
    return BootPlan(qemu=qemu, argv=argv, monitor_path=monitor_path,
                    screenshot=screenshot, passthrough=checks)


def run_qemu(plan: BootPlan, timeout: float = 12.0,
             screenshot_after: float = 6.0) -> tuple[int, str, str]:
    """Run QEMU, optionally grabbing a screenshot through the monitor.

    Returns (returncode, stdout+stderr, screenshot_path_or_empty).
    """
    proc = subprocess.Popen(
        plan.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        errors="replace",
    )
    shot = ""
    try:
        if plan.monitor_path and plan.screenshot:
            time.sleep(min(screenshot_after, timeout))
            shot = _screendump(plan.monitor_path, plan.screenshot)
        try:
            proc.wait(timeout=max(0.5, timeout - screenshot_after))
        except subprocess.TimeoutExpired:
            _quit(plan.monitor_path) or proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        if proc.poll() is None:
            proc.kill()
    output = proc.stdout.read() if proc.stdout else ""
    return proc.returncode or 0, output, shot


def _monitor_command(path: str, command: str, wait: float = 1.5) -> str:
    try:
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(5)
        sock.connect(path)
        time.sleep(0.3)
        try:
            sock.recv(65536)
        except OSError:
            pass
        sock.sendall(command.encode() + b"\n")
        time.sleep(wait)
        try:
            reply = sock.recv(65536).decode(errors="replace")
        except OSError:
            reply = ""
        sock.close()
        return reply
    except OSError:
        return ""


def _quit(monitor_path: str) -> bool:
    if not monitor_path:
        return False
    _monitor_command(monitor_path, "quit", wait=0.3)
    return True


def _screendump(monitor_path: str, output: str) -> str:
    """Ask QEMU for the framebuffer, then convert PPM to PNG if we can."""
    ppm = str(Path(output).with_suffix(".ppm"))
    _monitor_command(monitor_path, f"screendump {ppm}", wait=2.0)
    if not Path(ppm).exists():
        return ""
    if output.lower().endswith(".ppm"):
        return ppm
    try:
        from PIL import Image

        with Image.open(ppm) as im:
            im.convert("RGB").save(output)
        os.unlink(ppm)
        return output
    except Exception:
        return ppm


def screen_to_text(path: str, columns: int = 80, rows: int = 25) -> list[str]:
    """Very coarse OCR of a VGA text-mode framebuffer: is a cell lit or not?

    Not a character reader — it exists so a headless run can assert that
    *something* was drawn where the art should be, rather than a blank screen.
    """
    try:
        from PIL import Image
    except ImportError:
        return []
    try:
        with Image.open(path) as im:
            image = im.convert("RGB")
            width, height = image.size
            pixels = image.load()
    except Exception:
        return []

    cell_w, cell_h = max(1, width // columns), max(1, height // rows)
    lines: list[str] = []
    for row in range(rows):
        line = ""
        for col in range(columns):
            lit = 0
            for y in range(row * cell_h, min((row + 1) * cell_h, height), 2):
                for x in range(col * cell_w, min((col + 1) * cell_w, width)):
                    if sum(pixels[x, y]) > 90:
                        lit += 1
            line += "#" if lit > 6 else ("." if lit else " ")
        lines.append(line.rstrip())
    return lines
