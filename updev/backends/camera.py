"""Cameras: libcamera/CSI sensors and plain V4L2 devices.

A Pi 5 exposes a *lot* of /dev/video* nodes that are not cameras — the PiSP
back-end alone claims sixteen of them, plus the HEVC decoder. Listing those as
"devices" is noise, so we classify by driver: capture hardware is promoted,
ISP and codec nodes are tagged `helper` and stay out of the way unless asked
for.

libcamera is the authority on CSI sensors, so we ask picamera2 for the global
camera list and merge it with the V4L2 view.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..core.model import Device, Kind, Severity, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import (
    glob,
    have,
    read_text,
    readable,
    run,
    run_ok,
    usb_address_from_path,
)

# Drivers that produce frames from an actual image sensor.
_CAPTURE_DRIVERS = {"uvcvideo", "rp1-cfe", "unicam", "bcm2835-unicam", "gspca_main"}
# Drivers that are pipeline plumbing, not cameras.
_HELPER_DRIVERS = {"pispbe", "rpi-hevc-dec", "bcm2835-codec", "rpivid", "v4l2loopback"}


class CameraBackend(Backend):
    name = "camera"
    title = "Camera"
    kinds = (Kind.CAMERA,)

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        devices: list[Device] = []
        libcam = self._libcamera()
        devices.extend(libcam)
        devices.extend(self._v4l2(ctx, claimed=len(libcam)))
        devices.append(self._csi_status(libcam))
        return devices

    # -- libcamera ---------------------------------------------------------

    def _libcamera(self) -> list[Device]:
        # libcamera logs to stderr on import; quiet it before picamera2 loads.
        os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:ERROR")
        try:
            from picamera2 import Picamera2
        except Exception:                        # not installed, or broken install
            return []

        try:
            infos = Picamera2.global_camera_info()
        except Exception as e:
            dev = Device(
                uid="camera:libcamera",
                kind=Kind.CAMERA,
                name="libcamera",
                status=Status.ERROR,
                summary=f"enumeration failed: {e}",
            )
            dev.issue(Severity.ERROR, f"libcamera enumeration failed: {e}")
            return [dev]

        out: list[Device] = []
        for info in infos:
            num = info.get("Num", len(out))
            model = str(info.get("Model", "camera"))
            dev = Device(
                uid=f"camera:{num}",
                kind=Kind.CAMERA,
                name=model,
                status=Status.ONLINE,
                bus="libcamera",
                address=str(num),
                model=model,
                node=str(info.get("Id", "")),
                parent="host:board",
            )
            dev.detail["id"] = info.get("Id", "")
            dev.detail["location"] = info.get("Location", "")
            dev.detail["rotation"] = info.get("Rotation", "")
            dev.tags.append("libcamera")
            self._sensor_modes(dev, num)
            dev.summary = self._camera_summary(dev, model)
            dev.act("still", "Capture a JPEG", f"updev cam capture {num} -o shot.jpg")
            dev.act("modes", "List sensor modes", f"updev cam modes {num}")
            out.append(dev)
        return out

    def _sensor_modes(self, dev: Device, num: int) -> None:
        """Sensor modes come from picamera2, but instantiating a camera is slow,
        so we parse rpicam-hello's listing instead when it's available."""
        if not have("rpicam-hello"):
            return
        rc, out, _ = run(["rpicam-hello", "--list-cameras"], timeout=12)
        if rc != 0:
            return
        block = _camera_block(out, num)
        if not block:
            return
        modes = re.findall(r"(\d+x\d+)\s*\[([\d.]+)\s*fps", block)
        if modes:
            dev.detail["modes"] = [f"{res} @ {fps}fps" for res, fps in modes]
            widths = [int(r.split("x")[0]) for r, _ in modes]
            heights = [int(r.split("x")[1]) for r, _ in modes]
            dev.detail["max_resolution"] = f"{max(widths)}x{max(heights)}"
            dev.metrics["max_pixels"] = float(max(widths) * max(heights))

    @staticmethod
    def _camera_summary(dev: Device, model: str) -> str:
        bits = [model]
        if dev.detail.get("max_resolution"):
            bits.append(dev.detail["max_resolution"])
        if dev.detail.get("modes"):
            bits.append(f"{len(dev.detail['modes'])} modes")
        return " · ".join(bits)

    # -- V4L2 --------------------------------------------------------------

    def _v4l2(self, ctx: ProbeContext, claimed: int) -> list[Device]:
        out: list[Device] = []
        for node in sorted(glob("/sys/class/video4linux/video*"), key=_video_sort):
            name = read_text(node / "name") or node.name
            driver = ""
            drv_link = node / "device/driver"
            if drv_link.exists():
                try:
                    driver = drv_link.resolve().name
                except OSError:
                    driver = ""
            dev_path = f"/dev/{node.name}"

            is_helper = driver in _HELPER_DRIVERS
            is_capture = driver in _CAPTURE_DRIVERS

            # Skip the pipeline plumbing unless the user asked for everything.
            if is_helper and not ctx.deep:
                continue

            dev = Device(
                uid=f"v4l2:{node.name}",
                kind=Kind.CAMERA,
                name=name,
                status=Status.ONLINE if is_capture else Status.IDLE,
                bus="v4l2",
                address=node.name,
                driver=driver,
                node=dev_path,
                parent=self._v4l2_parent(node, driver),
            )
            dev.detail["driver"] = driver
            dev.detail["readable"] = readable(dev_path)
            if is_helper:
                dev.tags.append("helper")
                dev.detail["role"] = "ISP / codec pipeline node, not a camera"
                dev.summary = f"{driver} pipeline node"
            elif is_capture:
                dev.tags.append("capture")
                self._v4l2_formats(dev, dev_path)
                dev.summary = self._v4l2_summary(dev, name, driver)
                dev.act("capture", "Grab a frame", f"updev cam capture {node.name} -o shot.jpg")
            else:
                dev.summary = f"{driver or 'unknown driver'}"

            if driver == "uvcvideo":
                dev.tags.append("usb")
                dev.vendor, dev.model = _uvc_names(node)
            out.append(dev)
        return out

    def _v4l2_formats(self, dev: Device, path: str) -> None:
        if not have("v4l2-ctl") or not readable(path):
            return
        out = run_ok(["v4l2-ctl", "--device", path, "--list-formats-ext"], timeout=6)
        if not out:
            return
        fourccs = re.findall(r"\[\d+\]:\s+'(\w+)'\s+\(([^)]+)\)", out)
        if fourccs:
            dev.detail["formats"] = [f"{cc} ({desc})" for cc, desc in fourccs]
        sizes = sorted(
            {tuple(map(int, m)) for m in re.findall(r"Size:\s+Discrete\s+(\d+)x(\d+)", out)},
            key=lambda wh: wh[0] * wh[1],
        )
        if sizes:
            dev.detail["max_resolution"] = f"{sizes[-1][0]}x{sizes[-1][1]}"
            dev.detail["resolutions"] = [f"{w}x{h}" for w, h in sizes[-8:]]
            dev.metrics["max_pixels"] = float(sizes[-1][0] * sizes[-1][1])

    @staticmethod
    def _v4l2_summary(dev: Device, name: str, driver: str) -> str:
        bits = [name]
        if dev.detail.get("max_resolution"):
            bits.append(dev.detail["max_resolution"])
        if driver:
            bits.append(driver)
        return " · ".join(bits)

    @staticmethod
    def _v4l2_parent(node: Path, driver: str) -> str:
        """USB webcams should hang off their USB device in the tree."""
        if driver != "uvcvideo":
            return "host:board"
        try:
            real = node.resolve()
        except OSError:
            return "host:board"
        address = usb_address_from_path(real)
        return f"usb:{address}" if address else "host:board"

    # -- CSI slot status ---------------------------------------------------

    def _csi_status(self, libcam: list[Device]) -> Device:
        """Answer 'why doesn't my camera show up' before it gets asked."""
        dev = Device(
            uid="camera:csi",
            kind=Kind.CAMERA,
            name="CSI camera ports",
            bus="csi",
            parent="host:board",
        )
        config = read_text("/boot/firmware/config.txt") or read_text("/boot/config.txt")
        auto = re.search(r"^\s*camera_auto_detect=(\d)", config, re.M)
        dev.detail["camera_auto_detect"] = auto.group(1) if auto else "unset"
        overlays = re.findall(r"^\s*dtoverlay=(imx\S+|ov\S+)", config, re.M)
        if overlays:
            dev.detail["sensor_overlays"] = overlays

        if libcam:
            dev.status = Status.ONLINE
            dev.summary = f"{len(libcam)} camera(s) detected by libcamera"
            return dev

        dev.status = Status.IDLE
        dev.summary = "no camera detected"
        if auto and auto.group(1) == "1":
            dev.issue(
                Severity.INFO,
                "auto-detect is on but no sensor was found",
                doc="Either nothing is plugged into CAM0/CAM1, or the ribbon is in "
                    "backwards. On the Pi 5 the contacts face the board on both ends, "
                    "and the port needs the narrow 22-pin cable.",
            )
        else:
            dev.issue(
                Severity.WARN,
                "camera_auto_detect is not enabled",
                fix="Add camera_auto_detect=1 to /boot/firmware/config.txt and reboot",
            )
        return dev


# --------------------------------------------------------------------------

def _video_sort(path: Path) -> tuple[int, str]:
    m = re.search(r"(\d+)$", path.name)
    return (int(m.group(1)) if m else 0, path.name)


def _camera_block(listing: str, num: int) -> str:
    """Pull one camera's stanza out of `rpicam-hello --list-cameras` output."""
    blocks = re.split(r"^\s*(?=\d+\s*:\s)", listing, flags=re.M)
    for block in blocks:
        if re.match(rf"\s*{num}\s*:", block):
            return block
    return ""


def _uvc_names(node: Path) -> tuple[str, str]:
    """Walk up to the USB device and borrow its descriptor strings."""
    try:
        base = node.resolve()
    except OSError:
        return "", ""
    for parent in list(base.parents)[:8]:
        if (parent / "idVendor").exists():
            return read_text(parent / "manufacturer"), read_text(parent / "product")
    return "", ""
