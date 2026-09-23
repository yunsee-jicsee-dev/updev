"""NFC readers on the header — the transports, and the probe that finds them.

`updev/nfc.py` holds the protocol and never imports a hardware library. This
file is the other half: spidev, smbus2 and pyserial, wrapped so the PN532 sees
one interface whichever way its DIP switches are set.

The probing rule here is stricter than everywhere else in updev, and on
purpose. SPI has no addressing — a transaction on CE0 is seen by whatever is
wired to CE0, and if that is an OLED rather than an RC522 then bytes meant as
a register read land as display commands. So the SPI probe only runs under
`--deep` or when you explicitly type `updev nfc detect`. I2C is safe by
comparison (0x24 is a read to one address, and nothing else answers there),
and the UART probe is off by default because /dev/serial0 usually has a login
console on it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..core.model import Device, Kind, Severity, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import glob, readable
from ..nfc import (
    WIRINGS,
    Mfrc522,
    NfcError,
    Pn532,
    bit_reverse,
    reverse_bytes,
)

try:
    import spidev
    HAVE_SPIDEV = True
except ImportError:                                   # pragma: no cover
    spidev = None                                     # type: ignore[assignment]
    HAVE_SPIDEV = False

try:
    from smbus2 import SMBus, i2c_msg
    HAVE_SMBUS = True
except ImportError:                                   # pragma: no cover
    SMBus = None                                      # type: ignore[assignment]
    i2c_msg = None                                    # type: ignore[assignment]
    HAVE_SMBUS = False

try:
    import serial as pyserial
    HAVE_SERIAL = True
except ImportError:                                   # pragma: no cover
    pyserial = None                                   # type: ignore[assignment]
    HAVE_SERIAL = False

PN532_I2C_ADDRESS = 0x24
RC522_SPI_HZ = 1_000_000
PN532_UART_BAUD = 115200


# ==========================================================================
# transports
# ==========================================================================

class Pn532SpiTransport:
    """PN532 SPI: least-significant bit first, with a one-byte command prefix.

    0x01 writes a frame, 0x02 reads the status byte, 0x03 reads a frame — each
    reversed on the way out, because the Pi's controller cannot clock LSB
    first and the chip will not do anything else.
    """

    def __init__(self, spi) -> None:
        self.spi = spi

    def send(self, frame: bytes) -> None:
        self.spi.xfer2([bit_reverse(0x01)] + list(reverse_bytes(frame)))
        time.sleep(0.005)

    def ready(self) -> bool:
        answer = self.spi.xfer2([bit_reverse(0x02), 0x00])
        return bool(bit_reverse(answer[1]) & 0x01)

    def receive(self, count: int) -> bytes:
        if not self.ready():
            return b""
        answer = self.spi.xfer2([bit_reverse(0x03)] + [0x00] * count)
        return reverse_bytes(bytes(answer[1:]))


class Pn532I2cTransport:
    """PN532 I2C: the chip prefixes every reply with 0x01 when it has one."""

    def __init__(self, bus, address: int = PN532_I2C_ADDRESS) -> None:
        self.bus = bus
        self.address = address

    def send(self, frame: bytes) -> None:
        self.bus.i2c_rdwr(i2c_msg.write(self.address, list(frame)))
        time.sleep(0.005)

    def receive(self, count: int) -> bytes:
        read = i2c_msg.read(self.address, count + 1)
        try:
            self.bus.i2c_rdwr(read)
        except OSError:
            return b""
        blob = bytes(bytearray(read))
        if not blob or blob[0] != 0x01:
            return b""          # not ready yet; the caller retries
        return blob[1:]


class Pn532UartTransport:
    """PN532 HSU: a plain 115200 8N1 stream, frames back to back."""

    def __init__(self, port) -> None:
        self.port = port

    def send(self, frame: bytes) -> None:
        self.port.reset_input_buffer()
        self.port.write(frame)
        self.port.flush()

    def receive(self, count: int) -> bytes:
        return self.port.read(count)


# ==========================================================================
# probing
# ==========================================================================

@dataclass(slots=True)
class Reader:
    """One reader that answered, or one that was asked and didn't."""

    module: str                 # "MFRC522" / "PN532"
    transport: str              # "spi" / "i2c" / "uart"
    where: str                  # "/dev/spidev0.0", "i2c-1 0x24", "/dev/serial0"
    found: bool = False
    detail: str = ""
    error: str = ""
    raw_version: int = 0
    #: I2C bus number, kept so reopening never has to re-parse `where`.
    bus_number: int = -1

    @property
    def uid(self) -> str:
        """Two modules can be probed on one chip-select, so the module is part
        of the identity — otherwise an RC522 and a PN532 on CE0 collide."""
        node = self.where.replace("/dev/", "").replace(" ", "")
        return f"nfc:{self.module.lower()}:{node}"

    def as_dict(self) -> dict:
        return {
            "module": self.module,
            "transport": self.transport,
            "where": self.where,
            "found": self.found,
            "detail": self.detail,
            "error": self.error,
        }


def probe_rc522(node: Path) -> Reader:
    """Read VersionReg over SPI. 0x91/0x92 is a genuine MFRC522."""
    where = str(node)
    reader = Reader("MFRC522", "spi", where)
    if not HAVE_SPIDEV:
        reader.error = "python3-spidev is not installed"
        return reader
    bus, cs = _spidev_numbers(node)
    if bus is None:
        reader.error = "could not parse the spidev node name"
        return reader
    spi = spidev.SpiDev()
    try:
        spi.open(bus, cs)
        spi.max_speed_hz = RC522_SPI_HZ
        spi.mode = 0
        chip = Mfrc522(spi, name=f"MFRC522 on {node.name}")
        raw, name = chip.version()
        reader.raw_version = raw
        if raw in (0x00, 0xFF):
            reader.error = (f"VersionReg read back 0x{raw:02x} — that is an idle "
                            f"bus, not a chip. Check 3V3, GND and the RST wire.")
            return reader
        reader.found = raw in (0x90, 0x91, 0x92, 0x88, 0x12)
        reader.detail = name
        if not reader.found:
            reader.error = (f"something answered on {node.name} but VersionReg is "
                            f"0x{raw:02x} — it is not an RC522")
    except OSError as e:
        reader.error = f"{e.strerror or e}"
    finally:
        try:
            spi.close()
        except Exception:                             # pragma: no cover
            pass
    return reader


def probe_pn532_i2c(bus_number: int) -> Reader:
    reader = Reader("PN532", "i2c", f"i2c-{bus_number} 0x{PN532_I2C_ADDRESS:02x}",
                    bus_number=bus_number)
    if not HAVE_SMBUS:
        reader.error = "python3-smbus2 is not installed"
        return reader
    try:
        with SMBus(bus_number) as bus:
            chip = Pn532(Pn532I2cTransport(bus), name=f"PN532 on i2c-{bus_number}")
            _, detail = chip.firmware_version()
            reader.found = True
            reader.detail = detail
    except OSError as e:
        reader.error = _i2c_reason(e, bus_number)
    except (NfcError, ValueError) as e:
        reader.error = str(e)
    return reader


def _i2c_reason(error: OSError, bus_number: int) -> str:
    """An errno is not an answer. Say what silence on this address means."""
    import errno as _errno

    if error.errno in (_errno.EREMOTEIO, _errno.ENXIO, _errno.EAGAIN):
        return (f"nothing acknowledged at 0x{PN532_I2C_ADDRESS:02x} on i2c-{bus_number}"
                f" — no PN532 wired to this bus, or its DIP switches are not on I2C")
    if error.errno == _errno.EACCES:
        return f"no access to /dev/i2c-{bus_number} — add yourself to the i2c group"
    if error.errno == _errno.EBUSY:
        return f"i2c-{bus_number} 0x{PN532_I2C_ADDRESS:02x} is claimed by a kernel driver"
    return error.strerror or str(error)


def probe_pn532_spi(node: Path) -> Reader:
    reader = Reader("PN532", "spi", str(node))
    if not HAVE_SPIDEV:
        reader.error = "python3-spidev is not installed"
        return reader
    bus, cs = _spidev_numbers(node)
    if bus is None:
        reader.error = "could not parse the spidev node name"
        return reader
    spi = spidev.SpiDev()
    try:
        spi.open(bus, cs)
        spi.max_speed_hz = RC522_SPI_HZ
        spi.mode = 0
        chip = Pn532(Pn532SpiTransport(spi), name=f"PN532 on {node.name}")
        _, detail = chip.firmware_version()
        reader.found = True
        reader.detail = detail
    except (OSError, NfcError, ValueError) as e:
        reader.error = str(e)
    finally:
        try:
            spi.close()
        except Exception:                             # pragma: no cover
            pass
    return reader


def probe_pn532_uart(port: str = "/dev/serial0") -> Reader:
    reader = Reader("PN532", "uart", port)
    if not HAVE_SERIAL:
        reader.error = "python3-serial is not installed"
        return reader
    try:
        with pyserial.Serial(port, PN532_UART_BAUD, timeout=0.3) as handle:
            chip = Pn532(Pn532UartTransport(handle), name=f"PN532 on {port}")
            _, detail = chip.firmware_version()
            reader.found = True
            reader.detail = detail
    except (OSError, NfcError, ValueError) as e:
        reader.error = str(e)
    return reader


def probe_all(spi: bool = True, i2c: bool = True, uart: bool = False) -> list[Reader]:
    """Ask every bus a reader could be on. Order matters: I2C first, because
    it is the one probe that cannot disturb another chip."""
    found: list[Reader] = []
    if i2c:
        for node in sorted(glob("/dev/i2c-*")):
            number = _i2c_number(node)
            if number is None or number > 20:
                continue
            found.append(probe_pn532_i2c(number))
    if spi:
        for node in sorted(glob("/dev/spidev*")):
            if not readable(node):
                continue
            rc = probe_rc522(node)
            found.append(rc)
            if not rc.found:
                found.append(probe_pn532_spi(node))
    if uart:
        for port in ("/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0"):
            if Path(port).exists():
                found.append(probe_pn532_uart(port))
                break
    return found


def open_reader(reader: Reader):
    """Turn a positive probe into a live chip object, ready to poll.

    Returns (chip, closer). The caller is responsible for calling the closer —
    an spidev handle left open blocks the next run.
    """
    if reader.module == "MFRC522":
        bus, cs = _spidev_numbers(Path(reader.where))
        if bus is None:
            raise OSError(f"cannot reopen {reader.where}")
        spi = spidev.SpiDev()
        spi.open(bus, cs)
        spi.max_speed_hz = RC522_SPI_HZ
        spi.mode = 0
        chip = Mfrc522(spi, name=f"MFRC522 on {Path(reader.where).name}")
        chip.begin()
        chip.gain()
        return chip, spi.close

    if reader.transport == "i2c":
        bus = SMBus(reader.bus_number)
        chip = Pn532(Pn532I2cTransport(bus), name=reader.where)
        chip.sam_configure()
        return chip, bus.close

    if reader.transport == "spi":
        bus, cs = _spidev_numbers(Path(reader.where))
        if bus is None:
            raise OSError(f"cannot reopen {reader.where}")
        spi = spidev.SpiDev()
        spi.open(bus, cs)
        spi.max_speed_hz = RC522_SPI_HZ
        spi.mode = 0
        chip = Pn532(Pn532SpiTransport(spi), name=reader.where)
        chip.sam_configure()
        return chip, spi.close

    handle = pyserial.Serial(reader.where, PN532_UART_BAUD, timeout=0.3)
    chip = Pn532(Pn532UartTransport(handle), name=reader.where)
    chip.sam_configure()
    return chip, handle.close


# ==========================================================================
# backend
# ==========================================================================

class NfcBackend(Backend):
    """Readers wired to the header. Nothing here enumerates — it interrogates."""

    name = "nfc"
    title = "NFC"
    kinds = (Kind.NFC,)
    #: Talking to an unknown SPI chip is not something a default scan should do.
    slow = True

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        if not (glob("/dev/spidev*") or glob("/dev/i2c-*")):
            return False, "neither SPI nor I2C is enabled — nothing to wire a reader to"
        if not (HAVE_SPIDEV or HAVE_SMBUS):
            return False, "install python3-spidev or python3-smbus2 to reach a reader"
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        readers = probe_all(spi=True, i2c=True, uart=False)
        devices = [self._device(r) for r in readers if r.found]
        if not devices:
            devices.append(self._nothing_found(readers))
        return devices

    def _device(self, reader: Reader) -> Device:
        dev = Device(
            uid=reader.uid,
            kind=Kind.NFC,
            name=reader.module,
            status=Status.ONLINE,
            bus=reader.transport,
            address=reader.where,
            node=reader.where if reader.where.startswith("/dev/") else "",
            parent="host:board",
            summary=f"{reader.detail} · {reader.transport}",
        )
        dev.detail["module"] = reader.module
        dev.detail["transport"] = reader.transport
        dev.detail["firmware"] = reader.detail
        dev.tags.append("nfc")
        dev.act("poll", "Hold a tag on it and watch", "updev nfc poll")
        dev.act("read", "Read one tag's UID", "updev nfc read")
        dev.act("dump", "Dump a MIFARE sector", "updev nfc dump --sector 1")
        dev.act("wiring", "The wiring this expects", f"updev nfc wiring {reader.transport}")
        return dev

    def _nothing_found(self, readers: list[Reader]) -> Device:
        """No reader answered. Say what was asked and what to check."""
        dev = Device(
            uid="nfc:header",
            kind=Kind.NFC,
            name="NFC reader",
            status=Status.ABSENT,
            bus="header",
            parent="host:board",
            summary="no reader answered on SPI or I2C",
        )
        dev.detail["probed"] = [f"{r.module} on {r.where}" for r in readers] or ["nothing"]
        for reader in readers:
            if reader.error:
                dev.detail.setdefault("errors", {})[reader.where] = reader.error

        dev.issue(
            Severity.INFO,
            "no RC522 or PN532 responded",
            fix="updev nfc wiring",
            doc="A module on jumper wires cannot announce itself — there is no "
                "enumeration on SPI and only one fixed address on I2C. If it is "
                "wired and still silent, the usual causes are 5V instead of 3V3, "
                "a missing RST wire on the RC522, or PN532 DIP switches set to a "
                "different mode than the bus you wired.",
        )
        dev.act("wiring", "Pin-by-pin wiring for both modules", "updev nfc wiring")
        dev.act("detect", "Probe every bus again, verbosely", "updev nfc detect -v")
        return dev


# --------------------------------------------------------------------------

def _spidev_numbers(node: Path) -> tuple[int | None, int]:
    stem = node.name.replace("spidev", "")
    if "." not in stem:
        return None, 0
    bus, _, cs = stem.partition(".")
    try:
        return int(bus), int(cs)
    except ValueError:
        return None, 0


def _i2c_number(node: Path) -> int | None:
    try:
        return int(str(node).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


def missing_buses() -> list[tuple[str, str]]:
    """(bus, how to enable) for the buses a reader needs but this board hasn't
    got switched on. Drives the hint at the bottom of `updev nfc wiring`."""
    out = []
    if not glob("/dev/spidev*"):
        out.append(("spi", next(w.enable for w in WIRINGS if w.bus == "spi")))
    if not glob("/dev/i2c-*"):
        out.append(("i2c", next(w.enable for w in WIRINGS if w.bus == "i2c")))
    return out
