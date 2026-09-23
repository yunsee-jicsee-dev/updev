"""NFC readers you wire yourself: MFRC522 and PN532 on the 40-pin header.

A USB NFC reader announces itself — it has a descriptor, a CCID class, a
vendor ID. A module on jumper wires announces *nothing*. There is no bus
enumeration on SPI at all, and on I2C the module is one more address that
either answers or doesn't. So the device manager has to do two things it does
nowhere else: tell you which pin goes where, and then go ask the chip whether
you got it right.

Both halves live here.

  * **Wiring** — `WIRINGS`, a table of module → header pin, so `updev nfc
    wiring` can print the eight wires and `updev doctor` can notice the bus
    they need is switched off.

  * **Protocol** — the MFRC522 register interface over SPI, and the PN532
    frame format over SPI, I2C or HSU. The frame builders, the ISO 14443-A
    CRC and the ATQA/SAK decoding are pure functions, so the parts that are
    easy to get subtly wrong are tested without a reader on the desk.

Reading is the default and writing is opt-in. `write_block()` exists because
an editor needs it, but it refuses the two blocks that make a card worse:
block 0 (the manufacturer block — read-only on a genuine card, and the reason
"magic" clones exist) and the sector trailers, which hold the keys and the
access bits. A trailer written with an invalid access-bit combination locks
that sector permanently, so it takes an explicit `allow_trailer=True` that the
GUI only passes after a second confirmation.

Key A defaults to the factory FFFFFFFFFFFF because that is what an unwritten
MIFARE Classic ships with — if it has been keyed, authentication fails and
says so rather than trying anything clever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

__all__ = [
    "WIRINGS",
    "Wiring",
    "Tag",
    "Mfrc522",
    "Pn532",
    "PN532_ACK",
    "atqa_describe",
    "bit_reverse",
    "check_page_writable",
    "check_writable",
    "crc_a",
    "is_trailer",
    "parse_pn532_frame",
    "pn532_frame",
    "sak_describe",
    "uid_from_anticollision",
]


# ==========================================================================
# wiring
# ==========================================================================

@dataclass(slots=True)
class Wiring:
    """One module in one mode, as a list of wires you can follow with a finger."""

    module: str
    bus: str                       # "spi" | "i2c" | "uart"
    note: str
    #: (module pin, header pin, what the Pi calls it)
    wires: list[tuple[str, int, str]] = field(default_factory=list)
    enable: str = ""               # command that turns the bus on
    probe: str = ""                # how updev checks for it

    def as_dict(self) -> dict:
        return {
            "module": self.module,
            "bus": self.bus,
            "note": self.note,
            "enable": self.enable,
            "probe": self.probe,
            "wires": [{"module": m, "pin": p, "pi": n} for m, p, n in self.wires],
        }


WIRINGS: list[Wiring] = [
    Wiring(
        module="MFRC522 (RC522)",
        bus="spi",
        note="SPI only. 3.3V — the 5V pin will kill it. RST is not optional: "
             "the chip comes up in an undefined state and needs the reset line "
             "held high.",
        wires=[
            ("3.3V", 1, "3V3 power"),
            ("RST", 22, "GPIO25"),
            ("GND", 6, "GND (ground)"),
            ("MISO", 21, "GPIO9 / SPI0 MISO"),
            ("MOSI", 19, "GPIO10 / SPI0 MOSI"),
            ("SCK", 23, "GPIO11 / SPI0 SCLK"),
            ("SDA (=CS)", 24, "GPIO8 / SPI0 CE0"),
            ("IRQ", 18, "GPIO24 — optional, unused by updev"),
        ],
        enable="sudo raspi-config nonint do_spi 0 && sudo reboot",
        probe="reads VersionReg (0x37): 0x91/0x92 is a genuine MFRC522",
    ),
    Wiring(
        module="PN532 — I2C mode",
        bus="i2c",
        note="Set both DIP switches to I2C (SEL0=1, SEL1=0). Answers at 7-bit "
             "address 0x24, which is why `updev i2c scan` can find it and the "
             "RC522 can never be found that way.",
        wires=[
            ("VCC", 1, "3V3 power"),
            ("GND", 6, "GND (ground)"),
            ("SDA", 3, "GPIO2 / I2C1 SDA"),
            ("SCL", 5, "GPIO3 / I2C1 SCL"),
            ("IRQ", 18, "GPIO24 — optional"),
            ("RSTO", 22, "GPIO25 — optional"),
        ],
        enable="sudo raspi-config nonint do_i2c 0 && sudo reboot",
        probe="GetFirmwareVersion (0x02) at i2c-1 0x24",
    ),
    Wiring(
        module="PN532 — SPI mode",
        bus="spi",
        note="DIP switches to SPI (SEL0=0, SEL1=1). The PN532 clocks SPI "
             "least-significant bit first and the Pi's controller cannot, so "
             "updev reverses every byte in software.",
        wires=[
            ("VCC", 1, "3V3 power"),
            ("GND", 6, "GND (ground)"),
            ("SCK", 23, "GPIO11 / SPI0 SCLK"),
            ("MISO", 21, "GPIO9 / SPI0 MISO"),
            ("MOSI", 19, "GPIO10 / SPI0 MOSI"),
            ("SS (=NSS)", 24, "GPIO8 / SPI0 CE0"),
        ],
        enable="sudo raspi-config nonint do_spi 0 && sudo reboot",
        probe="GetFirmwareVersion (0x02) on /dev/spidev0.0",
    ),
    Wiring(
        module="PN532 — HSU (UART) mode",
        bus="uart",
        note="DIP switches to HSU (SEL0=0, SEL1=0), 115200 baud. TX and RX "
             "cross over: the module's TX goes to the Pi's RX. Needs the "
             "console off the port, or the login prompt eats every frame.",
        wires=[
            ("VCC", 1, "3V3 power"),
            ("GND", 6, "GND (ground)"),
            ("TXD", 10, "GPIO15 / UART RXD"),
            ("RXD", 8, "GPIO14 / UART TXD"),
        ],
        enable="sudo raspi-config nonint do_serial_hw 0   # and disable the console",
        probe="GetFirmwareVersion (0x02) on /dev/serial0 at 115200",
    ),
]


def wiring_for(name: str) -> Wiring | None:
    want = name.strip().lower()
    for w in WIRINGS:
        if want in w.module.lower() or want == w.bus:
            return w
    return None


# ==========================================================================
# ISO 14443-A, in pure functions
# ==========================================================================

def crc_a(data: bytes) -> bytes:
    """The CRC_A every ISO 14443-A frame carries, LSB first.

    Preset 0x6363, polynomial x^16 + x^12 + x^5 + 1 in its reflected form.
    The RC522 can compute this in hardware, but doing it here means the frame
    builders are testable and the chip's answer has something to be checked
    against.
    """
    crc = 0x6363
    for byte in data:
        cur = byte ^ (crc & 0xFF)
        cur = (cur ^ (cur << 4)) & 0xFF
        crc = ((crc >> 8) ^ (cur << 8) ^ (cur << 3) ^ (cur >> 4)) & 0xFFFF
    return bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def uid_from_anticollision(payload: bytes) -> bytes:
    """Five bytes back from ANTICOLLISION: four of UID and a BCC check byte.

    The BCC is the XOR of the other four. A wrong one means two tags answered
    at once or the read was corrupt, and returning the UID anyway would invent
    a card that isn't there.
    """
    if len(payload) < 5:
        raise ValueError(f"anticollision returned {len(payload)} bytes, expected 5")
    uid, bcc = payload[:4], payload[4]
    check = uid[0] ^ uid[1] ^ uid[2] ^ uid[3]
    if check != bcc:
        raise ValueError(f"BCC mismatch: computed 0x{check:02x}, card sent 0x{bcc:02x}")
    return uid


def atqa_describe(atqa: bytes) -> str:
    """ATQA says how long the UID is and whether the tag does bit-frame
    anticollision. It does not identify the product — SAK does that."""
    if len(atqa) < 2:
        return "no ATQA"
    value = atqa[0] | (atqa[1] << 8)
    size = {0b00: "4-byte UID", 0b01: "7-byte UID", 0b10: "10-byte UID"}.get(
        (atqa[0] >> 6) & 0b11, "reserved UID size")
    return f"ATQA 0x{value:04x} — {size}"


_SAK = {
    0x00: "MIFARE Ultralight / NTAG",
    0x08: "MIFARE Classic 1K",
    0x09: "MIFARE Mini",
    0x10: "MIFARE Plus 2K (SL2)",
    0x11: "MIFARE Plus 4K (SL2)",
    0x18: "MIFARE Classic 4K",
    0x19: "MIFARE Classic 2K",
    0x20: "ISO 14443-4 (DESFire, JCOP, phone HCE)",
    0x28: "JCOP with MIFARE emulation",
    0x38: "MIFARE Plus SL3 / SmartMX",
}


def sak_describe(sak: int) -> str:
    """SAK is the one byte that names the product family."""
    if sak in _SAK:
        return _SAK[sak]
    if sak & 0x20:
        return "ISO 14443-4 compliant (unknown product)"
    if sak & 0x08:
        return "MIFARE Classic family (unknown capacity)"
    return f"unknown SAK 0x{sak:02x}"


@dataclass(slots=True)
class Tag:
    """One card, as far as reading its UID gets you."""

    uid: bytes
    atqa: bytes = b""
    sak: int = -1
    reader: str = ""

    @property
    def uid_hex(self) -> str:
        return ":".join(f"{b:02X}" for b in self.uid)

    @property
    def kind(self) -> str:
        return sak_describe(self.sak) if self.sak >= 0 else "unknown"

    def as_dict(self) -> dict:
        return {
            "uid": self.uid_hex,
            "uid_bytes": list(self.uid),
            "atqa": atqa_describe(self.atqa) if self.atqa else "",
            "sak": self.sak,
            "kind": self.kind,
            "reader": self.reader,
        }


# ==========================================================================
# PN532 frames, in pure functions
# ==========================================================================

PN532_PREAMBLE = bytes((0x00, 0x00, 0xFF))
PN532_ACK = bytes((0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00))
PN532_NACK = bytes((0x00, 0x00, 0xFF, 0xFF, 0x00, 0x00))
_HOST_TO_PN532 = 0xD4
_PN532_TO_HOST = 0xD5


def pn532_frame(command: int, params: bytes = b"") -> bytes:
    """A normal information frame: 00 00 FF LEN LCS D4 cmd params DCS 00.

    LEN counts TFI and everything after it; LCS makes LEN+LCS come to zero in
    one byte, and DCS does the same for the data. Both are the reason a
    half-received frame is rejected instead of half-executed.
    """
    data = bytes((_HOST_TO_PN532, command)) + params
    length = len(data)
    if length > 255:
        raise ValueError("extended frames are not used by any command updev sends")
    lcs = (-length) & 0xFF
    dcs = (-sum(data)) & 0xFF
    return PN532_PREAMBLE + bytes((length, lcs)) + data + bytes((dcs, 0x00))


def parse_pn532_frame(blob: bytes) -> tuple[int, bytes]:
    """(response code, payload) from a PN532 reply, or ValueError with a reason.

    Accepts leading padding, because I2C and SPI both hand back a run of 0x00
    (and, on I2C, a 0x01 ready byte) before the preamble starts.
    """
    start = blob.find(PN532_PREAMBLE)
    if start < 0:
        raise ValueError("no 00 00 FF preamble in the reply")
    body = blob[start + 3:]
    if len(body) < 2:
        raise ValueError("frame ends before its length byte")
    if body[0] == 0x00 and body[1] == 0xFF:
        raise ValueError("this is an ACK, not a response frame")
    if body[0] == 0xFF and body[1] == 0xFF:
        raise ValueError("extended frames are not supported")
    length, lcs = body[0], body[1]
    if (length + lcs) & 0xFF:
        raise ValueError(f"length checksum failed: LEN 0x{length:02x} LCS 0x{lcs:02x}")
    data = body[2:2 + length]
    if len(data) < length:
        raise ValueError(f"frame claims {length} bytes, {len(data)} arrived")
    if len(body) < 2 + length + 1:
        raise ValueError("frame ends before its data checksum")
    dcs = body[2 + length]
    if (sum(data) + dcs) & 0xFF:
        raise ValueError("data checksum failed — the frame arrived corrupt")
    if not data or data[0] != _PN532_TO_HOST:
        raise ValueError(f"TFI 0x{data[0]:02x} is not a PN532→host frame")
    if len(data) < 2:
        raise ValueError("frame carries no response code")
    return data[1], data[2:]


def bit_reverse(value: int) -> int:
    """LSB-first, for the PN532's SPI mode.

    The BCM2712's SPI controller only clocks MSB first, so every byte in both
    directions gets reversed here instead. This is the single most common
    reason a PN532 works on I2C and stays silent on SPI.
    """
    return int(f"{value & 0xFF:08b}"[::-1], 2)


_REVERSED = bytes(bit_reverse(i) for i in range(256))


def reverse_bytes(data: bytes) -> bytes:
    return bytes(_REVERSED[b] for b in data)


# ==========================================================================
# MFRC522 over SPI
# ==========================================================================

# Registers we touch. The chip has 64; these are the ones a reader needs.
_R_COMMAND = 0x01
_R_COM_IEN = 0x02
_R_COM_IRQ = 0x04
_R_DIV_IRQ = 0x05
_R_ERROR = 0x06
_R_STATUS2 = 0x08
_R_FIFO_DATA = 0x09
_R_FIFO_LEVEL = 0x0A
_R_CONTROL = 0x0C
_R_BIT_FRAMING = 0x0D
_R_COLL = 0x0E
_R_MODE = 0x11
_R_TX_CONTROL = 0x14
_R_TX_ASK = 0x15
_R_CRC_RESULT_H = 0x21
_R_CRC_RESULT_L = 0x22
_R_MOD_WIDTH = 0x24
_R_RF_CFG = 0x26
_R_T_MODE = 0x2A
_R_T_PRESCALER = 0x2B
_R_T_RELOAD_H = 0x2C
_R_T_RELOAD_L = 0x2D
_R_VERSION = 0x37

_C_IDLE = 0x00
_C_CALC_CRC = 0x03
_C_TRANSCEIVE = 0x0C
_C_AUTHENT = 0x0E
_C_SOFT_RESET = 0x0F

# PICC commands
PICC_REQA = 0x26
PICC_WUPA = 0x52
PICC_ANTICOLL = (0x93, 0x20)
PICC_SELECT = (0x93, 0x70)
PICC_HALT = (0x50, 0x00)
PICC_AUTH_KEY_A = 0x60
PICC_AUTH_KEY_B = 0x61
PICC_READ = 0x30
PICC_WRITE = 0xA0
#: Ultralight/NTAG write one 4-byte page. A different opcode because the
#: page is a quarter the size of a Classic block.
PICC_WRITE_PAGE = 0xA2

#: A 4-bit 0x0A back from the card means "accepted". Anything else is a NAK.
MIFARE_ACK = 0x0A

DEFAULT_KEY = bytes([0xFF] * 6)

_VERSIONS = {0x91: "MFRC522 v1.0", 0x92: "MFRC522 v2.0", 0x90: "MFRC522 v0.0",
             0x88: "clone (FM17522)", 0x12: "clone (unbranded)"}


class NfcError(RuntimeError):
    """Something the reader or the card refused. The message says which."""


class Mfrc522:
    """The RC522, driven over spidev.

    `spi` is anything with `xfer2(list[int]) -> list[int]`, which is both
    `spidev.SpiDev` and, in the tests, a recorded fake.
    """

    def __init__(self, spi, name: str = "MFRC522") -> None:
        self.spi = spi
        self.name = name

    # -- register access ---------------------------------------------------

    @staticmethod
    def address(register: int, read: bool) -> int:
        """Address byte: bit 7 is the direction, bits 6-1 the register."""
        return ((register << 1) & 0x7E) | (0x80 if read else 0x00)

    def read_register(self, register: int) -> int:
        return self.spi.xfer2([self.address(register, True), 0x00])[1]

    def write_register(self, register: int, value: int) -> None:
        self.spi.xfer2([self.address(register, False), value & 0xFF])

    def set_bits(self, register: int, mask: int) -> None:
        self.write_register(register, self.read_register(register) | mask)

    def clear_bits(self, register: int, mask: int) -> None:
        self.write_register(register, self.read_register(register) & (~mask & 0xFF))

    # -- lifecycle ---------------------------------------------------------

    def version(self) -> tuple[int, str]:
        raw = self.read_register(_R_VERSION)
        return raw, _VERSIONS.get(raw, f"unknown silicon 0x{raw:02x}")

    def reset(self) -> None:
        self.write_register(_R_COMMAND, _C_SOFT_RESET)
        # The datasheet gives no completion flag for a soft reset; 50 ms is the
        # settling time everyone else waits and it is not worth being clever.
        time.sleep(0.05)

    def begin(self) -> None:
        """Reset, set the 13.56 MHz timer up, switch the field on."""
        self.reset()
        self.write_register(_R_T_MODE, 0x8D)        # auto-restart, prescaler high bits
        self.write_register(_R_T_PRESCALER, 0x3E)   # ~25 us per tick
        self.write_register(_R_T_RELOAD_L, 30)
        self.write_register(_R_T_RELOAD_H, 0)
        self.write_register(_R_TX_ASK, 0x40)        # 100% ASK modulation
        self.write_register(_R_MODE, 0x3D)          # CRC preset 0x6363
        self.write_register(_R_MOD_WIDTH, 0x26)
        self.antenna(True)

    def antenna(self, on: bool) -> None:
        if on:
            if not self.read_register(_R_TX_CONTROL) & 0x03:
                self.set_bits(_R_TX_CONTROL, 0x03)
        else:
            self.clear_bits(_R_TX_CONTROL, 0x03)

    def gain(self, value: int = 0x07) -> None:
        """RxGain, bits 6-4 of RFCfgReg. 0x07 is the 48 dB maximum."""
        self.write_register(_R_RF_CFG, (value & 0x07) << 4)

    # -- transfers ---------------------------------------------------------

    def transceive(self, data: bytes, bits: int = 0) -> tuple[bytes, int]:
        """Send a frame and collect the answer. `bits` is the count of valid
        bits in the last byte — REQA is a 7-bit frame, which is how a card
        tells a REQA apart from a data byte."""
        return self._command(_C_TRANSCEIVE, data, irq_enable=0x77, wait_irq=0x30, bits=bits)

    def _command(self, command: int, data: bytes, irq_enable: int, wait_irq: int,
                 bits: int = 0) -> tuple[bytes, int]:
        self.write_register(_R_COM_IEN, irq_enable | 0x80)
        self.clear_bits(_R_COM_IRQ, 0x80)
        self.set_bits(_R_FIFO_LEVEL, 0x80)          # flush the FIFO
        self.write_register(_R_COMMAND, _C_IDLE)

        for byte in data:
            self.write_register(_R_FIFO_DATA, byte)
        self.write_register(_R_BIT_FRAMING, bits & 0x07)
        self.write_register(_R_COMMAND, command)
        if command == _C_TRANSCEIVE:
            self.set_bits(_R_BIT_FRAMING, 0x80)     # StartSend

        # The timer reload above expires in ~25 ms; this bounds the wait even
        # if the interrupt never arrives at all.
        deadline = time.monotonic() + 0.2
        irq = 0
        while time.monotonic() < deadline:
            irq = self.read_register(_R_COM_IRQ)
            if irq & wait_irq:
                break
            if irq & 0x01:                          # timer expired: no card
                raise NfcError("no answer — no tag in the field")
        else:
            raise NfcError("reader did not raise an interrupt — check wiring and RST")

        self.clear_bits(_R_BIT_FRAMING, 0x80)
        error = self.read_register(_R_ERROR)
        if error & 0x13:                            # buffer overflow, parity, protocol
            raise NfcError(f"transfer failed, ErrorReg 0x{error:02x}")
        if error & 0x08:
            raise NfcError("collision — more than one tag in the field")

        if command != _C_TRANSCEIVE:
            return b"", 0
        count = self.read_register(_R_FIFO_LEVEL)
        last_bits = self.read_register(_R_CONTROL) & 0x07
        payload = bytes(self.read_register(_R_FIFO_DATA) for _ in range(count))
        return payload, (count - 1) * 8 + last_bits if last_bits else count * 8

    def crc(self, data: bytes) -> bytes:
        """Let the chip compute CRC_A. Cross-checked against `crc_a()` so a
        mis-set ModeReg preset shows up as a mismatch instead of a bad frame."""
        self.write_register(_R_COMMAND, _C_IDLE)
        self.clear_bits(_R_DIV_IRQ, 0x04)
        self.set_bits(_R_FIFO_LEVEL, 0x80)
        for byte in data:
            self.write_register(_R_FIFO_DATA, byte)
        self.write_register(_R_COMMAND, _C_CALC_CRC)
        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline:
            if self.read_register(_R_DIV_IRQ) & 0x04:
                break
        else:
            raise NfcError("CRC coprocessor did not finish")
        self.write_register(_R_COMMAND, _C_IDLE)
        return bytes((self.read_register(_R_CRC_RESULT_L),
                      self.read_register(_R_CRC_RESULT_H)))

    # -- the card ----------------------------------------------------------

    def request(self, wake: bool = False) -> bytes:
        """REQA (or WUPA for a halted card). Returns the 2-byte ATQA."""
        self.write_register(_R_BIT_FRAMING, 0x07)
        atqa, bits = self.transceive(bytes((PICC_WUPA if wake else PICC_REQA,)), bits=7)
        if bits != 16:
            raise NfcError(f"ATQA came back as {bits} bits, expected 16")
        return atqa

    def anticollision(self) -> bytes:
        self.write_register(_R_BIT_FRAMING, 0x00)
        payload, _ = self.transceive(bytes(PICC_ANTICOLL))
        return uid_from_anticollision(payload)

    def select(self, uid: bytes) -> int:
        """SELECT the UID we just read; the card answers with its SAK."""
        bcc = uid[0] ^ uid[1] ^ uid[2] ^ uid[3]
        frame = bytes(PICC_SELECT) + uid[:4] + bytes((bcc,))
        sak, _ = self.transceive(frame + self.crc(frame))
        if not sak:
            raise NfcError("card did not answer SELECT")
        return sak[0]

    def poll(self, wake: bool = False) -> Tag:
        """One full pass: REQA → ANTICOLLISION → SELECT."""
        atqa = self.request(wake=wake)
        uid = self.anticollision()
        sak = self.select(uid)
        return Tag(uid=uid, atqa=atqa, sak=sak, reader=self.name)

    def authenticate(self, block: int, uid: bytes, key: bytes = DEFAULT_KEY,
                     key_b: bool = False) -> None:
        """Crypto1 authentication happens inside the chip — we only hand it the
        key and the UID. Failure here almost always means the card is not on
        the factory key."""
        if len(key) != 6:
            raise ValueError("a MIFARE key is 6 bytes")
        command = PICC_AUTH_KEY_B if key_b else PICC_AUTH_KEY_A
        self._command(_C_AUTHENT, bytes((command, block)) + key + uid[:4],
                      irq_enable=0x12, wait_irq=0x10)
        if not self.read_register(_R_STATUS2) & 0x08:
            raise NfcError(f"authentication failed on block {block} — wrong key?")

    def read_block(self, block: int) -> bytes:
        frame = bytes((PICC_READ, block))
        data, _ = self.transceive(frame + self.crc(frame))
        if len(data) < 16:
            raise NfcError(f"block {block} returned {len(data)} bytes, expected 16")
        return data[:16]

    def write_block(self, block: int, payload: bytes,
                    allow_trailer: bool = False) -> None:
        """MIFARE write is two frames: the card ACKs the command, then the data.

        Both ACKs are 4-bit 0x0A. Checking only the first one is a common way
        to report success for a write the card actually refused.
        """
        check_writable(block, allow_trailer)
        if len(payload) != 16:
            raise ValueError(f"a MIFARE block is 16 bytes, got {len(payload)}")

        frame = bytes((PICC_WRITE, block))
        answer, bits = self.transceive(frame + self.crc(frame))
        self._expect_ack(answer, bits, f"card refused the write command for block {block}")

        answer, bits = self.transceive(bytes(payload) + self.crc(bytes(payload)))
        self._expect_ack(answer, bits, f"card refused the data for block {block}")

    @staticmethod
    def _expect_ack(answer: bytes, bits: int, context: str) -> None:
        if bits != 4 or not answer:
            raise NfcError(f"{context}: expected a 4-bit ACK, got {bits} bits")
        code = answer[0] & 0x0F
        if code != MIFARE_ACK:
            raise NfcError(f"{context}: NAK 0x{code:02x}")

    def read_page(self, page: int) -> bytes:
        """Ultralight/NTAG READ returns 16 bytes — four pages, wrapping at the
        end of memory. No authentication: these cards have none by default."""
        frame = bytes((PICC_READ, page))
        data, _ = self.transceive(frame + self.crc(frame))
        if len(data) < 16:
            raise NfcError(f"page {page} returned {len(data)} bytes, expected 16")
        return data[:16]

    def write_page(self, page: int, payload: bytes) -> None:
        """One 4-byte page. Pages 0-3 are refused — see `check_page_writable`."""
        check_page_writable(page)
        if len(payload) != 4:
            raise ValueError(f"an Ultralight page is 4 bytes, got {len(payload)}")
        frame = bytes((PICC_WRITE_PAGE, page)) + bytes(payload)
        answer, bits = self.transceive(frame + self.crc(frame))
        self._expect_ack(answer, bits, f"card refused the write for page {page}")

    def stop_crypto(self) -> None:
        self.clear_bits(_R_STATUS2, 0x08)

    def halt(self) -> None:
        frame = bytes(PICC_HALT)
        try:
            self.transceive(frame + self.crc(frame))
        except NfcError:
            pass            # HALT is answered with silence when it works


# ==========================================================================
# PN532 over SPI / I2C / HSU
# ==========================================================================

CMD_GET_FIRMWARE_VERSION = 0x02
CMD_SAM_CONFIGURATION = 0x14
CMD_IN_LIST_PASSIVE_TARGET = 0x4A
CMD_IN_DATA_EXCHANGE = 0x40

_PN532_ICS = {0x32: "PN532"}


class Pn532:
    """The PN532, over whichever transport its DIP switches are set to.

    A transport is any object with `send(bytes)` and `receive(int) -> bytes`;
    the three concrete ones live in `backends/nfc.py` because they need
    spidev, smbus2 and pyserial, and this module stays importable without them.
    """

    def __init__(self, transport, name: str = "PN532") -> None:
        self.transport = transport
        self.name = name

    def call(self, command: int, params: bytes = b"", timeout: float = 1.0) -> bytes:
        frame = pn532_frame(command, params)
        self.transport.send(frame)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            blob = self.transport.receive(64)
            if not blob:
                time.sleep(0.01)
                continue
            try:
                code, payload = parse_pn532_frame(blob)
            except ValueError as e:
                if "ACK" in str(e):
                    continue            # the ACK precedes the answer
                raise NfcError(str(e)) from None
            if code != command + 1:
                raise NfcError(
                    f"answer 0x{code:02x} does not match command 0x{command:02x}")
            return payload
        raise NfcError(f"no answer to command 0x{command:02x} within {timeout:.1f}s")

    def firmware_version(self) -> tuple[int, str]:
        payload = self.call(CMD_GET_FIRMWARE_VERSION)
        if len(payload) < 4:
            raise NfcError("firmware reply too short")
        ic, ver, rev, support = payload[0], payload[1], payload[2], payload[3]
        name = _PN532_ICS.get(ic, f"IC 0x{ic:02x}")
        return ic, f"{name} firmware {ver}.{rev} (support 0x{support:02x})"

    def sam_configure(self) -> None:
        """Normal mode, 1 s timeout, IRQ pin used. Without this the chip stays
        in its power-up state and ignores InListPassiveTarget."""
        self.call(CMD_SAM_CONFIGURATION, bytes((0x01, 0x14, 0x01)))

    def poll(self, timeout: float = 1.0) -> Tag:
        """InListPassiveTarget for one 106 kbps type-A target."""
        payload = self.call(CMD_IN_LIST_PASSIVE_TARGET, bytes((0x01, 0x00)),
                            timeout=timeout)
        if not payload or payload[0] == 0:
            raise NfcError("no tag in the field")
        # tag number, ATQA(2), SAK, UID length, UID…
        if len(payload) < 6:
            raise NfcError("target report too short")
        atqa = bytes((payload[3], payload[2]))      # reported MSB first
        sak = payload[4]
        uid_len = payload[5]
        uid = payload[6:6 + uid_len]
        if len(uid) != uid_len:
            raise NfcError("target report ended mid-UID")
        return Tag(uid=uid, atqa=atqa, sak=sak, reader=self.name)

    def read_block(self, block: int, uid: bytes, key: bytes = DEFAULT_KEY) -> bytes:
        """Authenticate then read, both through InDataExchange on target 1."""
        self.call(CMD_IN_DATA_EXCHANGE,
                  bytes((0x01, PICC_AUTH_KEY_A, block)) + key + uid[:4])
        payload = self.call(CMD_IN_DATA_EXCHANGE, bytes((0x01, PICC_READ, block)))
        if not payload or payload[0] != 0x00:
            raise NfcError(f"block {block} read failed, status 0x{payload[0]:02x}"
                           if payload else "empty read")
        return payload[1:17]

    def write_block(self, block: int, uid: bytes, payload: bytes,
                    key: bytes = DEFAULT_KEY, allow_trailer: bool = False) -> None:
        """Same two-step write, but the PN532 does the framing for us."""
        check_writable(block, allow_trailer)
        if len(payload) != 16:
            raise ValueError(f"a MIFARE block is 16 bytes, got {len(payload)}")
        self.call(CMD_IN_DATA_EXCHANGE,
                  bytes((0x01, PICC_AUTH_KEY_A, block)) + key + uid[:4])
        answer = self.call(CMD_IN_DATA_EXCHANGE,
                           bytes((0x01, PICC_WRITE, block)) + bytes(payload))
        if not answer or answer[0] != 0x00:
            raise NfcError(f"block {block} write failed, status "
                           f"0x{answer[0]:02x}" if answer else "empty write reply")


# ==========================================================================
# shared helpers
# ==========================================================================

def sector_blocks(sector: int) -> list[int]:
    """MIFARE Classic: sectors 0-31 hold 4 blocks, 32-39 hold 16."""
    if sector < 32:
        base = sector * 4
        return list(range(base, base + 4))
    base = 128 + (sector - 32) * 16
    return list(range(base, base + 16))


def is_trailer(block: int) -> bool:
    """Every fourth block below 128, every sixteenth above it, holds the keys."""
    if block < 128:
        return (block + 1) % 4 == 0
    return (block - 128 + 1) % 16 == 0


def check_writable(block: int, allow_trailer: bool = False) -> None:
    """Refuse the writes that damage a card rather than change it."""
    if block == 0:
        raise NfcError(
            "block 0 is the manufacturer block — it holds the UID and is "
            "read-only on a genuine card"
        )
    if is_trailer(block) and not allow_trailer:
        raise NfcError(
            f"block {block} is a sector trailer (keys + access bits). Writing "
            f"an invalid access-bit combination locks the sector permanently, "
            f"so it needs an explicit override"
        )


def check_page_writable(page: int) -> None:
    """Ultralight pages 0-3 are not ordinary memory.

    0-1 are the UID and its check bytes, 2 holds the lock bits, and 3 is the
    one-time-programmable capability container: writing it ORs into the
    existing value and can never be undone. A card formatted by accident this
    way stays formatted.
    """
    if page <= 3:
        names = {0: "UID", 1: "UID", 2: "lock bytes", 3: "OTP capability container"}
        raise NfcError(
            f"page {page} is the {names[page]} — writing it is either impossible "
            f"or permanent, so updev does not offer it"
        )


def describe_block(block: int, data: bytes) -> str:
    """Block 0 of sector 0 is the manufacturer block; every fourth block is a
    trailer holding the keys, which read back as zeros by design."""
    if block == 0:
        return "manufacturer block — UID and vendor data, read-only"
    if block < 128 and (block + 1) % 4 == 0:
        return "sector trailer — key A, access bits, key B (keys read as 0)"
    if block >= 128 and (block - 128 + 1) % 16 == 0:
        return "sector trailer"
    return "data"


def hexdump(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def ascii_dump(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "·" for b in data)
