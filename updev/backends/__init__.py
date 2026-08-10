"""Backend registry.

Order here is the order backends appear in reports, so it runs roughly
"inside out": the board, then its buses, then what's on them, then the network.
"""

from __future__ import annotations

from ..core.registry import Backend
from .bluetooth import BluetoothBackend
from .camera import CameraBackend
from .display import DisplayBackend
from .gpio import GpioBackend
from .host import HostBackend
from .i2c import I2cBackend
from .network import LanBackend, NetworkBackend
from .serial_ import SerialBackend
from .spi import SpiBackend
from .storage import StorageBackend
from .usb import UsbBackend

BACKEND_CLASSES: tuple[type[Backend], ...] = (
    HostBackend,
    StorageBackend,
    UsbBackend,
    I2cBackend,
    SpiBackend,
    SerialBackend,
    CameraBackend,
    DisplayBackend,
    GpioBackend,
    NetworkBackend,
    BluetoothBackend,
    LanBackend,
)


def all_backends() -> list[Backend]:
    return [cls() for cls in BACKEND_CLASSES]


def backend_names() -> list[str]:
    return [cls.name for cls in BACKEND_CLASSES]


__all__ = ["BACKEND_CLASSES", "all_backends", "backend_names"]
