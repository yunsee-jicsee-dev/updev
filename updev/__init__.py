"""updev — a unified device manager for the Raspberry Pi and friends.

Enumerates the host board, its buses (USB, I2C, SPI, serial, GPIO), attached
cameras and storage, the network interfaces and the neighbours on the LAN —
all through one model, one CLI and one live dashboard.
"""

__version__ = "1.0.0"

from .core.model import Device, Kind, ScanResult, Severity, Status
from .core.registry import ProbeContext, Scanner, build_scanner

__all__ = [
    "Device",
    "Kind",
    "ProbeContext",
    "ScanResult",
    "Scanner",
    "Severity",
    "Status",
    "__version__",
    "build_scanner",
]
