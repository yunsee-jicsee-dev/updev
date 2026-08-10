"""Raw USB descriptors, read through the system's own USB permissions.

sysfs summarises a device; the descriptors *are* the device. Reading them
gives the things sysfs never surfaces:

  * every configuration, not just the active one
  * the full endpoint map — transfer type, max packet size, polling interval,
    and for SuperSpeed the burst depth
  * interface association descriptors, which are what actually group a
    composite device into functions
  * class-specific descriptors (HID, UVC, Audio) sitting between the standard
    ones
  * the real per-configuration power budget

Two sources, same bytes:

  ``/dev/bus/usb/BBB/DDD``  — usbfs. Ships ``crw-rw-r--``, so a plain read
                              works for any user; that read returns the cached
                              device and configuration descriptors. *Write*
                              access (root, or a udev rule) additionally allows
                              control transfers, which is what QEMU passthrough
                              needs.
  ``/sys/bus/usb/devices/<addr>/descriptors`` — the identical blob, always
                              world-readable. Used as the fallback.

Everything here is parsing, so it is testable against captured blobs with no
hardware present.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .core.util import read_text

__all__ = [
    "ConfigurationDescriptor",
    "DescriptorTree",
    "DeviceDescriptor",
    "EndpointDescriptor",
    "InterfaceDescriptor",
    "descriptor_source",
    "parse_descriptors",
    "read_raw_descriptors",
]

USB_ROOT = Path("/sys/bus/usb/devices")

# Standard descriptor types.
DT_DEVICE = 0x01
DT_CONFIG = 0x02
DT_STRING = 0x03
DT_INTERFACE = 0x04
DT_ENDPOINT = 0x05
DT_IAD = 0x0B
DT_BOS = 0x0F
DT_DEVICE_CAPABILITY = 0x10
DT_HID = 0x21
DT_REPORT = 0x22
DT_CS_INTERFACE = 0x24
DT_CS_ENDPOINT = 0x25
DT_SS_ENDPOINT_COMPANION = 0x30

DESCRIPTOR_NAMES: dict[int, str] = {
    DT_DEVICE: "Device",
    DT_CONFIG: "Configuration",
    DT_STRING: "String",
    DT_INTERFACE: "Interface",
    DT_ENDPOINT: "Endpoint",
    0x06: "Device Qualifier",
    0x07: "Other Speed Configuration",
    0x08: "Interface Power",
    0x09: "OTG",
    0x0A: "Debug",
    DT_IAD: "Interface Association",
    DT_BOS: "BOS",
    DT_DEVICE_CAPABILITY: "Device Capability",
    DT_HID: "HID",
    DT_REPORT: "HID Report",
    DT_CS_INTERFACE: "Class-specific Interface",
    DT_CS_ENDPOINT: "Class-specific Endpoint",
    DT_SS_ENDPOINT_COMPANION: "SuperSpeed Endpoint Companion",
}

TRANSFER_TYPES = {0: "control", 1: "isochronous", 2: "bulk", 3: "interrupt"}

#: Class-specific interface subtypes worth naming, keyed by interface class.
_CS_SUBTYPES: dict[int, dict[int, str]] = {
    0x01: {           # Audio
        0x01: "AC Header", 0x02: "Input Terminal", 0x03: "Output Terminal",
        0x04: "Mixer Unit", 0x06: "Feature Unit", 0x07: "Processing Unit",
    },
    0x0E: {           # Video
        0x01: "VC Header", 0x02: "Input Terminal", 0x03: "Output Terminal",
        0x05: "Processing Unit", 0x06: "Extension Unit",
    },
}
#: UVC VideoStreaming interface subtypes.
_VS_SUBTYPES = {
    0x01: "VS Input Header", 0x02: "VS Output Header",
    0x03: "Still Image Frame", 0x04: "Uncompressed Format",
    0x05: "Uncompressed Frame", 0x06: "MJPEG Format", 0x07: "MJPEG Frame",
    0x0A: "MPEG2-TS Format", 0x0C: "DV Format", 0x0D: "Colour Matching",
    0x10: "Frame-Based Format", 0x11: "Frame-Based Frame",
}

#: Class-specific *endpoint* subtypes — a different namespace from the
#: interface ones, which is easy to get wrong and produces confident nonsense.
_CS_ENDPOINT_SUBTYPES: dict[int, dict[int, str]] = {
    0x01: {0x01: "EP General"},                                   # Audio
    0x0E: {0x01: "EP General", 0x02: "EP Endpoint", 0x03: "EP Interrupt"},  # Video
}


@dataclass(slots=True)
class EndpointDescriptor:
    address: int = 0
    attributes: int = 0
    max_packet_size: int = 0
    interval: int = 0
    burst: int | None = None          # SuperSpeed companion, if present

    @property
    def number(self) -> int:
        return self.address & 0x0F

    @property
    def direction(self) -> str:
        return "IN" if self.address & 0x80 else "OUT"

    @property
    def transfer_type(self) -> str:
        return TRANSFER_TYPES.get(self.attributes & 0x03, "?")

    @property
    def packet_size(self) -> int:
        """Bits 0-10. Bits 11-12 carry extra high-speed transactions."""
        return self.max_packet_size & 0x07FF

    @property
    def transactions_per_microframe(self) -> int:
        return ((self.max_packet_size >> 11) & 0x03) + 1

    @property
    def bandwidth_per_frame(self) -> int:
        return self.packet_size * self.transactions_per_microframe

    def describe(self) -> str:
        out = (f"EP{self.number} {self.direction} {self.transfer_type} "
               f"{self.packet_size}B")
        if self.transactions_per_microframe > 1:
            out += f" ×{self.transactions_per_microframe}"
        if self.burst:
            out += f" burst {self.burst + 1}"
        if self.interval:
            out += f" interval {self.interval}"
        return out

    def as_dict(self) -> dict:
        return {
            "address": f"0x{self.address:02x}",
            "number": self.number,
            "direction": self.direction,
            "transfer_type": self.transfer_type,
            "max_packet_size": self.packet_size,
            "transactions_per_microframe": self.transactions_per_microframe,
            "interval": self.interval,
            "burst": self.burst,
        }


@dataclass(slots=True)
class InterfaceDescriptor:
    number: int = 0
    alternate: int = 0
    cls: int = 0
    subclass: int = 0
    protocol: int = 0
    string_index: int = 0
    endpoints: list[EndpointDescriptor] = field(default_factory=list)
    class_specific: list[str] = field(default_factory=list)

    @property
    def triple(self) -> str:
        return f"0x{self.cls:02x}/0x{self.subclass:02x}/0x{self.protocol:02x}"

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "alternate": self.alternate,
            "class": f"0x{self.cls:02x}",
            "subclass": f"0x{self.subclass:02x}",
            "protocol": f"0x{self.protocol:02x}",
            "endpoints": [e.as_dict() for e in self.endpoints],
            "class_specific": self.class_specific,
        }


@dataclass(slots=True)
class Association:
    first_interface: int = 0
    interface_count: int = 0
    cls: int = 0
    subclass: int = 0
    protocol: int = 0

    def as_dict(self) -> dict:
        return {
            "first_interface": self.first_interface,
            "interface_count": self.interface_count,
            "class": f"0x{self.cls:02x}",
            "subclass": f"0x{self.subclass:02x}",
            "protocol": f"0x{self.protocol:02x}",
        }


@dataclass(slots=True)
class ConfigurationDescriptor:
    value: int = 0
    interfaces: list[InterfaceDescriptor] = field(default_factory=list)
    associations: list[Association] = field(default_factory=list)
    attributes: int = 0
    max_power_ma: int = 0
    total_length: int = 0
    num_interfaces: int = 0

    @property
    def self_powered(self) -> bool:
        return bool(self.attributes & 0x40)

    @property
    def remote_wakeup(self) -> bool:
        return bool(self.attributes & 0x20)

    def as_dict(self) -> dict:
        return {
            "value": self.value,
            "max_power_ma": self.max_power_ma,
            "self_powered": self.self_powered,
            "remote_wakeup": self.remote_wakeup,
            "num_interfaces": self.num_interfaces,
            "associations": [a.as_dict() for a in self.associations],
            "interfaces": [i.as_dict() for i in self.interfaces],
        }


@dataclass(slots=True)
class DeviceDescriptor:
    usb_version: str = ""
    cls: int = 0
    subclass: int = 0
    protocol: int = 0
    max_packet_size0: int = 0
    vendor_id: str = ""
    product_id: str = ""
    device_version: str = ""
    num_configurations: int = 0

    def as_dict(self) -> dict:
        return {
            "usb_version": self.usb_version,
            "class": f"0x{self.cls:02x}",
            "subclass": f"0x{self.subclass:02x}",
            "protocol": f"0x{self.protocol:02x}",
            "max_packet_size0": self.max_packet_size0,
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
            "device_version": self.device_version,
            "num_configurations": self.num_configurations,
        }


@dataclass(slots=True)
class DescriptorTree:
    device: DeviceDescriptor | None = None
    configurations: list[ConfigurationDescriptor] = field(default_factory=list)
    source: str = ""
    raw_length: int = 0
    unknown: list[tuple[int, int]] = field(default_factory=list)   # (type, length)
    error: str = ""

    @property
    def endpoints(self) -> list[EndpointDescriptor]:
        return [
            ep for config in self.configurations
            for iface in config.interfaces
            for ep in iface.endpoints
        ]

    def has_transfer_type(self, kind: str) -> bool:
        return any(ep.transfer_type == kind for ep in self.endpoints)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "raw_length": self.raw_length,
            "device": self.device.as_dict() if self.device else None,
            "configurations": [c.as_dict() for c in self.configurations],
            "unknown_descriptors": [
                {"type": f"0x{t:02x}", "length": length} for t, length in self.unknown
            ],
            "error": self.error,
        }


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def usbfs_path(address: str) -> str:
    """`/dev/bus/usb/BBB/DDD` for a sysfs USB address like `1-2.3`."""
    base = USB_ROOT / address
    bus = read_text(base / "busnum")
    dev = read_text(base / "devnum")
    if not (bus and dev):
        return ""
    return f"/dev/bus/usb/{int(bus):03d}/{int(dev):03d}"


def descriptor_source(address: str) -> tuple[str, str, bool]:
    """Where we'll read from: (path, kind, writable).

    `writable` is what QEMU passthrough and control transfers need; reading
    descriptors does not.
    """
    node = usbfs_path(address)
    if node and os.access(node, os.R_OK):
        return node, "usbfs", os.access(node, os.W_OK)
    sysfs = USB_ROOT / address / "descriptors"
    if sysfs.exists():
        return str(sysfs), "sysfs", False
    return "", "", False


def read_raw_descriptors(address: str) -> tuple[bytes, str, str]:
    """Raw descriptor blob, plus where it came from."""
    path, kind, _ = descriptor_source(address)
    if not path:
        return b"", "", "no readable descriptor source"
    try:
        with open(path, "rb") as fh:
            return fh.read(65536), kind, ""
    except PermissionError:
        return b"", kind, f"permission denied reading {path}"
    except OSError as e:
        return b"", kind, f"{path}: {e}"


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def parse_descriptors(blob: bytes, source: str = "") -> DescriptorTree:
    """Walk the descriptor chain. Every descriptor is length-prefixed, so we
    hop by bLength and never trust an inner field to bound the walk."""
    tree = DescriptorTree(source=source, raw_length=len(blob))
    if not blob:
        tree.error = "empty descriptor blob"
        return tree

    offset = 0
    config: ConfigurationDescriptor | None = None
    iface: InterfaceDescriptor | None = None
    endpoint: EndpointDescriptor | None = None

    while offset + 2 <= len(blob):
        length = blob[offset]
        dtype = blob[offset + 1]
        if length < 2 or offset + length > len(blob):
            tree.error = f"truncated descriptor at offset {offset}"
            break
        chunk = blob[offset:offset + length]

        if dtype == DT_DEVICE and length >= 18:
            tree.device = _parse_device(chunk)
        elif dtype == DT_CONFIG and length >= 9:
            config = _parse_config(chunk)
            tree.configurations.append(config)
            iface = endpoint = None
        elif dtype == DT_IAD and length >= 8 and config is not None:
            config.associations.append(Association(
                first_interface=chunk[2], interface_count=chunk[3],
                cls=chunk[4], subclass=chunk[5], protocol=chunk[6],
            ))
        elif dtype == DT_INTERFACE and length >= 9 and config is not None:
            iface = InterfaceDescriptor(
                number=chunk[2], alternate=chunk[3], cls=chunk[5],
                subclass=chunk[6], protocol=chunk[7], string_index=chunk[8],
            )
            config.interfaces.append(iface)
            endpoint = None
        elif dtype == DT_ENDPOINT and length >= 7 and iface is not None:
            endpoint = EndpointDescriptor(
                address=chunk[2], attributes=chunk[3],
                max_packet_size=struct.unpack_from("<H", chunk, 4)[0],
                interval=chunk[6],
            )
            iface.endpoints.append(endpoint)
        elif dtype == DT_SS_ENDPOINT_COMPANION and length >= 6 and endpoint is not None:
            endpoint.burst = chunk[2]
        elif dtype in (DT_CS_INTERFACE, DT_CS_ENDPOINT) and iface is not None:
            iface.class_specific.append(_describe_class_specific(iface, dtype, chunk))
        elif dtype == DT_HID and iface is not None:
            iface.class_specific.append(_describe_hid(chunk))
        else:
            tree.unknown.append((dtype, length))

        offset += length

    return tree


def _parse_device(chunk: bytes) -> DeviceDescriptor:
    bcd_usb = struct.unpack_from("<H", chunk, 2)[0]
    bcd_dev = struct.unpack_from("<H", chunk, 12)[0]
    return DeviceDescriptor(
        usb_version=_bcd(bcd_usb),
        cls=chunk[4], subclass=chunk[5], protocol=chunk[6],
        max_packet_size0=chunk[7],
        vendor_id=f"{struct.unpack_from('<H', chunk, 8)[0]:04x}",
        product_id=f"{struct.unpack_from('<H', chunk, 10)[0]:04x}",
        device_version=_bcd(bcd_dev),
        num_configurations=chunk[17],
    )


def _parse_config(chunk: bytes) -> ConfigurationDescriptor:
    return ConfigurationDescriptor(
        total_length=struct.unpack_from("<H", chunk, 2)[0],
        num_interfaces=chunk[4],
        value=chunk[5],
        attributes=chunk[7],
        # bMaxPower counts 2 mA units on USB 2, 8 mA on SuperSpeed. We can't
        # tell which from the configuration alone, so report the USB 2 reading
        # and let the caller scale it when the device is SuperSpeed.
        max_power_ma=chunk[8] * 2,
    )


def _describe_class_specific(iface: InterfaceDescriptor, dtype: int, chunk: bytes) -> str:
    subtype = chunk[2] if len(chunk) > 2 else 0
    if dtype == DT_CS_ENDPOINT:
        table = _CS_ENDPOINT_SUBTYPES.get(iface.cls, {})
    elif iface.cls == 0x0E and iface.subclass == 0x02:
        # UVC splits VideoControl (subclass 1) from VideoStreaming (subclass 2),
        # and the two use different subtype numbering.
        table = _VS_SUBTYPES
    else:
        table = _CS_SUBTYPES.get(iface.cls, {})
    name = table.get(subtype, f"subtype 0x{subtype:02x}")
    return f"{name} ({len(chunk)}B)"


def _describe_hid(chunk: bytes) -> str:
    if len(chunk) < 9:
        return "HID descriptor (truncated)"
    version = _bcd(struct.unpack_from("<H", chunk, 2)[0])
    num = chunk[5]
    parts = []
    for i in range(num):
        base = 6 + i * 3
        if base + 2 >= len(chunk):
            break
        rtype = chunk[base]
        rlen = struct.unpack_from("<H", chunk, base + 1)[0]
        label = "report" if rtype == DT_REPORT else f"0x{rtype:02x}"
        parts.append(f"{label} {rlen}B")
    return f"HID {version}" + (f" — {', '.join(parts)}" if parts else "")


def _bcd(value: int) -> str:
    return f"{value >> 8}.{(value >> 4) & 0x0F}{value & 0x0F}"


# --------------------------------------------------------------------------
# what the descriptors add to role detection
# --------------------------------------------------------------------------

def descriptor_hints(tree: DescriptorTree) -> list[str]:
    """Observations the descriptor tree supports that sysfs alone does not."""
    hints: list[str] = []
    if not tree.device:
        return hints

    iso = [ep for ep in tree.endpoints if ep.transfer_type == "isochronous"]
    if iso:
        widest = max(ep.bandwidth_per_frame for ep in iso)
        hints.append(
            f"{len(iso)} isochronous endpoint(s), widest {widest} B/microframe — "
            "reserved bandwidth, which only streaming hardware (camera, audio) asks for"
        )

    interrupts = [ep for ep in tree.endpoints if ep.transfer_type == "interrupt"]
    if interrupts:
        fastest = min(ep.interval for ep in interrupts if ep.interval) if any(
            ep.interval for ep in interrupts) else 0
        if fastest:
            hints.append(
                f"interrupt endpoint polled every {fastest} — input devices poll fast, "
                "status endpoints poll slowly"
            )

    for config in tree.configurations:
        for assoc in config.associations:
            hints.append(
                f"interface association: {assoc.interface_count} interfaces from #"
                f"{assoc.first_interface} form one function (class 0x{assoc.cls:02x})"
            )

    if len(tree.configurations) > 1:
        hints.append(
            f"{len(tree.configurations)} configurations — the device can present "
            "itself more than one way"
        )

    for config in tree.configurations:
        if config.max_power_ma:
            hints.append(
                f"configuration {config.value} requests {config.max_power_ma} mA"
                + (" (self-powered)" if config.self_powered else " from the bus")
            )
    return hints
