"""USB tree straight out of sysfs.

Deliberately no pyusb: sysfs needs no root, never claims an interface away
from a running driver, and already knows the topology. We reconstruct the
hub tree, name things from usb.ids, and flag the classic Pi failure modes
(a 3.0-capable disk negotiating 480Mbps, ports over their power budget).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..core.model import Device, Kind, Severity, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import read_hex, read_int, read_text, usb_names

USB_ROOT = Path("/sys/bus/usb/devices")

_CLASSES = {
    0x00: "per-interface", 0x01: "audio", 0x02: "communications", 0x03: "HID",
    0x05: "physical", 0x06: "image", 0x07: "printer", 0x08: "mass storage",
    0x09: "hub", 0x0A: "CDC data", 0x0B: "smart card", 0x0D: "content security",
    0x0E: "video", 0x0F: "personal healthcare", 0x10: "audio/video",
    0x11: "billboard", 0x12: "USB-C bridge", 0xDC: "diagnostic",
    0xE0: "wireless", 0xEF: "miscellaneous", 0xFE: "application specific",
    0xFF: "vendor specific",
}

# sysfs `speed` is in Mbps; map to the marketing name people actually recognise.
_SPEEDS = {
    "1.5": "Low-Speed (1.5 Mbps)",
    "12": "Full-Speed (12 Mbps)",
    "480": "High-Speed (480 Mbps)",
    "5000": "SuperSpeed (5 Gbps)",
    "10000": "SuperSpeed+ (10 Gbps)",
    "20000": "SuperSpeed+ x2 (20 Gbps)",
}


def _speed_shortfall(declared: str, speed: str) -> str:
    """Negotiated speed, when it falls short of what bcdUSB actually promises.

    Only bcdUSB 3.x is usable here. A device reporting 2.00 is stating which
    *specification* it complies with, not which speeds it supports — plenty of
    Full-Speed-only hardware legitimately says 2.00, so treating that as a
    shortfall flags half the devices on a healthy bus. 3.x is different: there
    is no such thing as a USB 3 device that cannot do SuperSpeed, so a 3.x
    device on a 480 Mbps link really has lost its SuperSpeed pairs.
    """
    if declared[:1] != "3":
        return ""
    actual = {"1.5": 1, "12": 1, "480": 2, "5000": 3, "10000": 3, "20000": 3}.get(speed, 0)
    if actual and actual < 3:
        return _SPEEDS.get(speed, f"{speed} Mbps")
    return ""


class UsbBackend(Backend):
    name = "usb"
    title = "USB"
    kinds = (Kind.USB,)

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        if not USB_ROOT.is_dir():
            return False, "no USB subsystem in sysfs"
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        devices: list[Device] = []
        try:
            entries = sorted(os.listdir(USB_ROOT))
        except OSError as e:
            raise RuntimeError(f"cannot list {USB_ROOT}: {e}") from e

        for entry in entries:
            if ":" in entry:            # "1-2:1.0" is an interface, not a device
                continue
            path = USB_ROOT / entry
            if not (path / "idVendor").exists():
                continue
            devices.append(self._device(entry, path))

        self._link_parents(devices)
        self._check_power_budget(devices)
        self._check_speed_caps(devices)
        return devices

    # ----------------------------------------------------------------------

    def _device(self, entry: str, path: Path) -> Device:
        vid = read_text(path / "idVendor")
        pid = read_text(path / "idProduct")
        db_vendor, db_product = usb_names(vid, pid)

        # The descriptor strings the device reports are usually nicer than
        # usb.ids, but plenty of cheap hardware leaves them blank.
        vendor = read_text(path / "manufacturer") or db_vendor
        product = read_text(path / "product") or db_product
        is_root_hub = entry.startswith("usb")

        dev = Device(
            uid=f"usb:{entry}",
            kind=Kind.USB,
            name=product or db_product or f"USB {vid}:{pid}",
            status=Status.ONLINE,
            bus="usb",
            address=entry,
            vendor=vendor,
            model=product,
            serial=read_text(path / "serial"),
            node=str(path),
        )
        dev.detail["id"] = f"{vid}:{pid}"
        dev.detail["usb_version"] = read_text(path / "version")

        speed = read_text(path / "speed")
        if speed:
            dev.detail["speed"] = _SPEEDS.get(speed, f"{speed} Mbps")
            try:
                dev.metrics["speed_mbps"] = float(speed)
            except ValueError:
                pass

        cls = read_hex(path / "bDeviceClass")
        if cls is not None:
            dev.detail["class"] = _CLASSES.get(cls, f"0x{cls:02X}")

        raw_power = read_text(path / "bMaxPower")
        if raw_power:
            dev.detail["max_power"] = raw_power
            try:
                dev.metrics["max_power_ma"] = float(raw_power.rstrip("mA"))
            except ValueError:
                pass

        maxchild = read_int(path / "maxchild")
        if maxchild:
            dev.detail["ports"] = maxchild

        interfaces = self._interfaces(entry, path)
        if interfaces:
            dev.detail["interfaces"] = interfaces
            drivers = sorted({i["driver"] for i in interfaces if i.get("driver")})
            dev.driver = ", ".join(drivers)

        if is_root_hub:
            dev.tags.append("root-hub")
            # Root hubs describe themselves as "Linux <kernel> xhci-hcd xHCI Host
            # Controller", which tells you nothing and eats the whole column.
            dev.detail["controller"] = f"{vendor} {product}".strip()
            gen = "USB 3" if speed in ("5000", "10000", "20000") else "USB 2"
            dev.name = f"{entry} root hub ({gen})"
            dev.vendor = ""
            dev.model = product
        if cls == 0x09:
            dev.tags.append("hub")

        # Nothing bound to any interface means the kernel has no driver for it.
        if not is_root_hub and interfaces and not any(i.get("driver") for i in interfaces):
            dev.status = Status.DEGRADED
            dev.issue(
                Severity.WARN,
                "no kernel driver bound to any interface",
                doc="The device enumerated but nothing is driving it — a missing "
                    "module, or it needs a userspace driver.",
            )

        # A device that negotiated below its own declared bcdUSB is nearly
        # always a cable or connector fault, and it silently caps everything
        # plugged in below it.
        declared = read_text(path / "version").strip()
        if not is_root_hub:
            gap = _speed_shortfall(declared, speed)
            if gap:
                dev.status = Status.DEGRADED
                dev.issue(
                    Severity.WARN,
                    f"declares USB {declared} but negotiated only {gap}",
                    fix=f"Try another cable and port, then: updev usb path {entry}",
                    doc="Anything downstream of this device inherits the slower link.",
                )

        if not is_root_hub:
            self._identify_role(dev)
        dev.summary = self._summary(dev, interfaces, is_root_hub)
        if any(i.get("class_code") == 0x08 for i in interfaces):
            dev.tags.append("storage")
            self._classify_storage(dev)
        if any(i.get("class_code") == 0x03 for i in interfaces):
            dev.tags.append("input")
        if any(i.get("class_code") == 0x0E for i in interfaces):
            dev.tags.append("camera")
            dev.act("camera", "Inspect as a camera", "updev cam list")
        return dev

    @staticmethod
    def _identify_role(dev: Device) -> None:
        """Keyboard, camera, Wi-Fi, phone, audio dongle — see `updev/usbrole.py`."""
        from ..usbrole import UsbRole, gather_device_facts, identify

        verdict = identify(gather_device_facts(dev.address))
        if not verdict.roles or verdict.primary is UsbRole.UNKNOWN:
            return
        dev.detail["role"] = verdict.label
        dev.detail["roles"] = [str(r) for r in verdict.roles]
        dev.tags.extend(str(r) for r in verdict.roles)
        if verdict.evidence:
            dev.detail["role_evidence"] = [
                f"[{e.source}] {e.observed}" for e in verdict.evidence
            ]
        if UsbRole.BILLBOARD in verdict.roles:
            dev.issue(
                Severity.INFO,
                "USB-C alternate mode was requested but not established",
                doc="This device wanted DisplayPort (or similar) over USB-C. The "
                    "Pi 5's USB-C port is power-only, so alt mode is never "
                    "available — connect the display over HDMI instead.",
            )
        dev.act("path", "Trace the physical path", f"updev usb path {dev.address}")

    @staticmethod
    def _classify_storage(dev: Device) -> None:
        """Work out what kind of storage this is — see `updev/usbclass.py`.
        Pure sysfs reads, about a millisecond, so it runs on every scan."""
        from ..usbclass import UsbClass, classify, gather_facts

        verdict = classify(gather_facts(usb_address=dev.address))
        if verdict.usb_class is UsbClass.UNKNOWN:
            return
        dev.tags.append(str(verdict.usb_class))
        dev.detail["storage_class"] = f"{verdict.usb_class} — {verdict.label}"
        dev.detail["storage_class_confidence"] = str(verdict.confidence)
        dev.detail["storage_class_evidence"] = [
            f"{e.signature}: {e.observed}" for e in verdict.evidence
        ]
        dev.summary = f"{verdict.usb_class} · {dev.summary}"
        dev.act("classify", "Signature breakdown",
                f"updev usb classify {dev.address}")

    def _interfaces(self, entry: str, path: Path) -> list[dict]:
        out = []
        try:
            names = sorted(p for p in os.listdir(USB_ROOT) if p.startswith(entry + ":"))
        except OSError:
            return out
        for iface in names:
            ipath = USB_ROOT / iface
            code = read_hex(ipath / "bInterfaceClass")
            driver = ""
            link = ipath / "driver"
            if link.is_symlink() or link.exists():
                try:
                    driver = os.path.basename(os.path.realpath(link))
                except OSError:
                    driver = ""
            out.append(
                {
                    "id": iface.split(":", 1)[1],
                    "class": _CLASSES.get(code, f"0x{code:02X}" if code is not None else "?"),
                    "class_code": code,
                    "driver": driver,
                    "endpoints": read_hex(ipath / "bNumEndpoints"),
                }
            )
        return out

    @staticmethod
    def _summary(dev: Device, interfaces: list[dict], is_root_hub: bool) -> str:
        bits = []
        if is_root_hub:
            bits.append("root hub")
            ports = dev.detail.get("ports")
            if ports:
                bits.append(f"{ports} ports")
        elif dev.detail.get("role"):
            # The identified role reads better than a list of interface classes.
            bits.append(dev.detail["role"])
        else:
            classes = sorted({i["class"] for i in interfaces if i.get("class")})
            if classes:
                bits.append("/".join(classes))
            elif dev.detail.get("class"):
                bits.append(dev.detail["class"])
        if dev.detail.get("speed"):
            bits.append(dev.detail["speed"])
        if dev.driver:
            bits.append(f"driver {dev.driver}")
        return " · ".join(bits)

    @staticmethod
    def _link_parents(devices: list[Device]) -> None:
        """`3-2.1` hangs off `3-2`, `3-2` off root hub `usb3`, roots off the board."""
        known = {d.address for d in devices}
        for dev in devices:
            addr = dev.address
            if addr.startswith("usb"):
                dev.parent = "host:board"
                continue
            if "." in addr:
                candidate = addr.rsplit(".", 1)[0]
            elif "-" in addr:
                candidate = "usb" + addr.split("-", 1)[0]
            else:
                candidate = ""
            dev.parent = f"usb:{candidate}" if candidate in known else "host:board"

    @staticmethod
    def _check_speed_caps(devices: list[Device]) -> None:
        """Report a hub that caps everything behind it.

        Stated as an observation rather than a fault: we cannot tell from the
        descriptors whether a hub running at Full Speed is broken or simply is
        a Full-Speed hub. What is certain either way is that its children can
        never exceed it, and that is the part worth knowing.
        """
        by_uid = {d.uid: d for d in devices}
        for dev in devices:
            if "hub" not in dev.tags and "root-hub" not in dev.tags:
                continue
            children = [d for d in devices if d.parent == dev.uid]
            if not children:
                continue
            own = dev.metrics.get("speed_mbps")
            parent = by_uid.get(dev.parent or "")
            upstream = parent.metrics.get("speed_mbps") if parent else None
            if not own or not upstream or own >= upstream:
                continue
            dev.detail["downstream_speed_cap"] = dev.detail.get("speed", f"{own:.0f} Mbps")
            dev.issue(
                Severity.WARN,
                f"running at {dev.detail.get('speed', own)} while its upstream port "
                f"offers {parent.detail.get('speed', upstream)}",
                fix=f"Try another cable and port, then: updev usb path {dev.address}",
                doc=f"{len(children)} device(s) behind this hub inherit the slower "
                    "link and cannot go faster than it.",
            )

    @staticmethod
    def _check_power_budget(devices: list[Device]) -> None:
        """Pi 5 gives 1.6A across the USB-A ports (5A supply) or 600mA (lesser one)."""
        by_root: dict[str, float] = {}
        for dev in devices:
            if "root-hub" in dev.tags:
                continue
            draw = dev.metrics.get("max_power_ma", 0.0)
            root = dev.parent or ""
            while root and not root.startswith("usb:usb") and root != "host:board":
                parent = next((d for d in devices if d.uid == root), None)
                root = parent.parent if parent else ""
            if root.startswith("usb:usb"):
                by_root[root] = by_root.get(root, 0.0) + draw
        for dev in devices:
            if dev.uid in by_root:
                total = by_root[dev.uid]
                dev.detail["downstream_draw"] = f"{total:.0f}mA advertised"
                if total > 1600:
                    dev.status = Status.DEGRADED
                    dev.issue(
                        Severity.WARN,
                        f"downstream devices advertise {total:.0f}mA — over the "
                        "1.6A USB-A budget",
                        fix="Use a powered hub, or `usb_max_current_enable=1` with a 5A PSU.",
                    )
