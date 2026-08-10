"""A 1.44 MB floppy image that refuses to boot, on purpose.

Two payloads in one image:

  * **The boot sector** is real 16-bit x86 that prints ASCII art through the
    BIOS teletype call and then halts. Boot it and you get art instead of an
    operating system — the disk works, it just declines to do the usual thing.
  * **The filesystem** is a hand-built FAT12 with the art as plain text files,
    so mounting it (or opening it in anything) shows the same thing.

The FAT12 layout is assembled here rather than shelling out to mkfs, because
the whole point is a self-contained artifact — and because a 1.44 MB floppy
has the one filesystem geometry simple enough to write by hand:

    sector 0        boot sector (BPB + our boot code)
    sectors 1-9     FAT #1
    sectors 10-18   FAT #2 (mirror)
    sectors 19-32   root directory, 224 entries
    sectors 33+     data, one sector per cluster, first cluster is #2

It also makes a good test article for the classifier: written to a real USB
floppy drive, `updev usb classify` should call it FUSB with certainty.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

__all__ = ["FLOPPY_SIZE", "FloppyImage", "boot_message", "build_image", "gallery"]

BYTES_PER_SECTOR = 512
SECTORS_PER_CLUSTER = 1
RESERVED_SECTORS = 1
FAT_COUNT = 2
ROOT_ENTRIES = 224
TOTAL_SECTORS = 2880
SECTORS_PER_FAT = 9
SECTORS_PER_TRACK = 18
HEADS = 2
MEDIA_DESCRIPTOR = 0xF0
FLOPPY_SIZE = TOTAL_SECTORS * BYTES_PER_SECTOR          # 1,474,560

ROOT_DIR_SECTORS = (ROOT_ENTRIES * 32) // BYTES_PER_SECTOR      # 14
FIRST_DATA_SECTOR = RESERVED_SECTORS + FAT_COUNT * SECTORS_PER_FAT + ROOT_DIR_SECTORS
DATA_CLUSTERS = TOTAL_SECTORS - FIRST_DATA_SECTOR

BOOT_CODE_OFFSET = 0x3E          # where the BPB ends and our code begins
SIGNATURE_OFFSET = 0x1FE


# --------------------------------------------------------------------------
# the art
# --------------------------------------------------------------------------

WORDMARK = r"""
   _   _ ____  ____  _______     __
  | | | |  _ \|  _ \| ____\ \   / /
  | | | | |_) | | | |  _|  \ \ / /
  | |_| |  __/| |_| | |___  \ V /
   \___/|_|   |____/|_____|  \_/
"""

DISK = r"""
        .-------------------------.
        |  ___________________    |
        | |                   |   |
        | |    3.5" HD  1.44M |   |
        | |___________________|   |
        |                         |
        |   .-----------------.   |
        |   |  ###########    |   |
        |   |  #         #    |   |
        |   |  #  (  )   #    |   |
        |   |  #         #    |   |
        |   |  ###########    |   |
        |   '-----------------'   |
        |                         |
        '-------------------------'
             NOT BOOTABLE, BY DESIGN
"""

PI = r"""
              .~~.   .~~.
             '. \ ' ' / .'
              .~ .~~~..~.
             : .~.'~'.~. :
            ~ (   ) (   ) ~
           ( : '~'.~.'~' : )
            ~ .~ (   ) ~. ~
             (  : '~' :  )
              '~ .~~~. ~'
                  '~'

        R A S P B E R R Y   P I   5
     ------------------------------------
       BCM2712  ·  Cortex-A76 x4
       RP1 southbridge  ·  PCIe x1
       40-pin header  ·  2x CSI/DSI
"""

BUS_MAP = r"""
   HOW EVERYTHING HANGS TOGETHER
   =============================

            +--------------------+
            |     BCM2712 SoC    |
            +---------+----------+
                      |
             PCIe x1  |
                      v
            +--------------------+
            |    RP1 southbridge |
            +--+------+-------+--+
               |      |       |
        +------+  +---+---+   +------+
        |         |       |          |
        v         v       v          v
      USB 2/3   40-pin  Ethernet   2x CSI
                 |
       +---------+---------+---------+
       |         |         |         |
       v         v         v         v
      I2C       SPI      UART      GPIO
     pin 3/5   19/21/23   8/10    the rest
"""

CLASSES = r"""
   USB STORAGE, SORTED BY SIGNATURE
   ================================

   NUSB  plain flash drive
         RMB=1, no VPD 0xB1, Bulk-Only transport

   FUSB  floppy
         bInterfaceSubClass 0x04 = UFI,
         which stands for USB Floppy Interface.
         (this disk's drive, most likely)

   HUSB  external spinning disk
         VPD 0xB1 rotation rate >= 0x0401,
         i.e. the drive states its own RPM

   SUSB  external solid-state disk
         VPD 0xB1 rotation rate = 0x0001,
         RMB=0, and usually UAS transport

   ODD   optical drive
         SCSI peripheral type 0x05
"""

COLOPHON = """\
WHAT IS THIS
============

A 1.44 MB floppy image that does not boot.

The boot sector is real 16-bit x86: it prints
ASCII art through BIOS INT 10h and then halts.
So booting it gives you art, not an OS.

Everything else is a FAT12 filesystem, built
by hand, holding the same art as text files.

Made by updev, a device manager for the
Raspberry Pi. If you have a USB floppy drive,
write this image to a disk and run:

    updev usb classify

It should come back FUSB, with certainty,
because bInterfaceSubClass 0x04 is literally
the USB Floppy Interface.

    updev floppy show
    updev floppy make -o disk.img
"""


def gallery() -> dict[str, str]:
    """Filename -> contents. 8.3 names, because FAT12 has no long-name support
    here and a floppy deserves the period-correct constraint."""
    return {
        "README.TXT": COLOPHON,
        "UPDEV.TXT": WORDMARK.strip("\n") + "\n",
        "DISK.TXT": DISK.strip("\n") + "\n",
        "PI5.TXT": PI.strip("\n") + "\n",
        "BUSES.TXT": BUS_MAP.strip("\n") + "\n",
        "USBCLASS.TXT": CLASSES.strip("\n") + "\n",
    }


def boot_message() -> str:
    """What the boot sector prints. Kept short — it has to fit in the sector."""
    return (
        "\r\n"
        "   _   _ ____  ____  _______     __\r\n"
        "  | | | |  _ \\|  _ \\| ____\\ \\   / /\r\n"
        "  | | | | |_) | | | |  _|  \\ \\ / /\r\n"
        "  | |_| |  __/| |_| | |___  \\ V /\r\n"
        "   \\___/|_|   |____/|_____|  \\_/\r\n"
        "\r\n"
        "  THIS DISK DOES NOT BOOT.\r\n"
        "  It is 1.44 MB of ASCII art instead.\r\n"
        "  Mount it and read README.TXT.\r\n"
        "\r\n"
        "  [halted]\r\n"
    )


# --------------------------------------------------------------------------
# boot sector
# --------------------------------------------------------------------------

def build_boot_code(message: str) -> bytes:
    """16-bit real-mode code: print `message` via INT 10h, then halt forever.

    Offsets are computed rather than hand-tabulated — the `mov si` operand has
    to point at the message's *loaded* address, and the BIOS loads this sector
    at 0x7C00, so the operand is 0x7C00 + the message's offset in the sector.
    """
    code = bytearray()
    code += b"\xFA"                          # cli
    code += b"\x31\xC0"                      # xor  ax, ax
    code += b"\x8E\xD8"                      # mov  ds, ax
    code += b"\x8E\xC0"                      # mov  es, ax
    code += b"\x8E\xD0"                      # mov  ss, ax
    code += b"\xBC\x00\x7C"                  # mov  sp, 0x7C00
    code += b"\xFB"                          # sti
    si_operand = len(code) + 1               # patched once the length is known
    code += b"\xBE\x00\x00"                  # mov  si, <message address>

    loop_start = len(code)
    code += b"\xAC"                          # lodsb
    code += b"\x08\xC0"                      # or   al, al
    jz_at = len(code)
    code += b"\x74\x00"                      # jz   halt   (displacement patched)
    code += b"\xB4\x0E"                      # mov  ah, 0x0E   (teletype output)
    code += b"\xBB\x07\x00"                  # mov  bx, 0x0007 (page 0, grey)
    code += b"\xCD\x10"                      # int  0x10
    jmp_at = len(code)
    code += b"\xEB\x00"                      # jmp  loop_start (patched)

    halt_at = len(code)
    code += b"\xF4"                          # hlt
    code += b"\xEB\xFD"                      # jmp  $-1  (back onto the hlt)

    # Relative displacements are measured from the end of the jump instruction.
    code[jz_at + 1] = (halt_at - (jz_at + 2)) & 0xFF
    code[jmp_at + 1] = (loop_start - (jmp_at + 2)) & 0xFF

    payload = message.encode("ascii", "replace") + b"\x00"
    message_offset = BOOT_CODE_OFFSET + len(code)
    struct.pack_into("<H", code, si_operand, 0x7C00 + message_offset)

    blob = bytes(code) + payload
    budget = SIGNATURE_OFFSET - BOOT_CODE_OFFSET
    if len(blob) > budget:
        raise ValueError(
            f"boot sector overflows by {len(blob) - budget} bytes — "
            f"shorten the boot message ({len(payload)} bytes of {budget} available)"
        )
    return blob


def build_boot_sector(label: str, message: str, serial: int | None = None) -> bytes:
    """BPB + boot code + 0xAA55, exactly 512 bytes."""
    sector = bytearray(BYTES_PER_SECTOR)

    sector[0:3] = b"\xEB\x3C\x90"                       # jmp short 0x3E; nop
    sector[3:11] = b"UPDEV1.0"                          # OEM name, 8 bytes

    struct.pack_into("<H", sector, 11, BYTES_PER_SECTOR)
    sector[13] = SECTORS_PER_CLUSTER
    struct.pack_into("<H", sector, 14, RESERVED_SECTORS)
    sector[16] = FAT_COUNT
    struct.pack_into("<H", sector, 17, ROOT_ENTRIES)
    struct.pack_into("<H", sector, 19, TOTAL_SECTORS)
    sector[21] = MEDIA_DESCRIPTOR
    struct.pack_into("<H", sector, 22, SECTORS_PER_FAT)
    struct.pack_into("<H", sector, 24, SECTORS_PER_TRACK)
    struct.pack_into("<H", sector, 26, HEADS)
    struct.pack_into("<I", sector, 28, 0)               # hidden sectors
    struct.pack_into("<I", sector, 32, 0)               # large total sectors

    sector[36] = 0x00                                   # drive number (A:)
    sector[37] = 0x00                                   # reserved
    sector[38] = 0x29                                   # extended boot signature
    struct.pack_into("<I", sector, 39, serial if serial is not None
                     else int(time.time()) & 0xFFFFFFFF)
    sector[43:54] = _pad_label(label)
    sector[54:62] = b"FAT12   "

    code = build_boot_code(message)
    sector[BOOT_CODE_OFFSET:BOOT_CODE_OFFSET + len(code)] = code
    sector[SIGNATURE_OFFSET:SIGNATURE_OFFSET + 2] = b"\x55\xAA"
    return bytes(sector)


# --------------------------------------------------------------------------
# FAT12
# --------------------------------------------------------------------------

def _pack_fat12(chain: dict[int, int]) -> bytes:
    """Pack cluster entries into 12-bit slots, two per three bytes."""
    fat = bytearray(SECTORS_PER_FAT * BYTES_PER_SECTOR)
    entries = dict(chain)
    entries[0] = 0xF00 | MEDIA_DESCRIPTOR       # media descriptor + padding
    entries[1] = 0xFFF                          # end-of-chain marker

    for index, value in entries.items():
        offset = (index * 3) // 2
        if offset + 1 >= len(fat):
            continue
        if index % 2 == 0:
            fat[offset] = value & 0xFF
            fat[offset + 1] = (fat[offset + 1] & 0xF0) | ((value >> 8) & 0x0F)
        else:
            fat[offset] = (fat[offset] & 0x0F) | ((value << 4) & 0xF0)
            fat[offset + 1] = (value >> 4) & 0xFF
    return bytes(fat)


def _pad_label(label: str) -> bytes:
    return label.upper().encode("ascii", "replace")[:11].ljust(11, b" ")


def _dir_entry(name: str, cluster: int, size: int, attr: int = 0x20,
               when: time.struct_time | None = None) -> bytes:
    entry = bytearray(32)
    if attr & 0x08:
        # A volume label occupies all 11 name bytes as one field, and must match
        # the boot sector byte for byte or fsck complains about the mismatch.
        entry[0:11] = _pad_label(name)
    else:
        stem, _, ext = name.partition(".")
        entry[0:8] = stem.upper().encode("ascii", "replace")[:8].ljust(8, b" ")
        entry[8:11] = ext.upper().encode("ascii", "replace")[:3].ljust(3, b" ")
    entry[11] = attr

    when = when or time.localtime()
    fat_time = (when.tm_hour << 11) | (when.tm_min << 5) | (when.tm_sec // 2)
    fat_date = ((max(when.tm_year, 1980) - 1980) << 9) | (when.tm_mon << 5) | when.tm_mday
    struct.pack_into("<H", entry, 22, fat_time)
    struct.pack_into("<H", entry, 24, fat_date)
    struct.pack_into("<H", entry, 26, cluster)
    struct.pack_into("<I", entry, 28, size)
    return bytes(entry)


@dataclass(slots=True)
class FloppyImage:
    data: bytes
    files: dict[str, int]        # name -> size in bytes
    label: str
    free_bytes: int

    @property
    def size(self) -> int:
        return len(self.data)


def build_image(
    label: str = "UPDEV ART",
    files: dict[str, str] | None = None,
    message: str | None = None,
    serial: int | None = None,
) -> FloppyImage:
    """Assemble the whole 1.44 MB image."""
    files = gallery() if files is None else files
    message = boot_message() if message is None else message

    image = bytearray(FLOPPY_SIZE)
    image[0:BYTES_PER_SECTOR] = build_boot_sector(label, message, serial)

    root = bytearray()
    root += _dir_entry(label, 0, 0, attr=0x08)

    fat: dict[int, int] = {}
    next_cluster = 2
    written = 0

    for name, text in files.items():
        payload = text.replace("\n", "\r\n").encode("ascii", "replace")
        clusters_needed = max(1, -(-len(payload) // BYTES_PER_SECTOR))
        if next_cluster + clusters_needed > DATA_CLUSTERS:
            raise ValueError(f"{name} does not fit on the disk")

        first = next_cluster
        for i in range(clusters_needed):
            cluster = first + i
            is_last = i == clusters_needed - 1
            fat[cluster] = 0xFFF if is_last else cluster + 1
            sector = FIRST_DATA_SECTOR + (cluster - 2)
            start = sector * BYTES_PER_SECTOR
            chunk = payload[i * BYTES_PER_SECTOR:(i + 1) * BYTES_PER_SECTOR]
            image[start:start + len(chunk)] = chunk

        root += _dir_entry(name, first, len(payload))
        next_cluster += clusters_needed
        written += len(payload)

    if len(root) > ROOT_ENTRIES * 32:
        raise ValueError("too many files for a 224-entry root directory")

    table = _pack_fat12(fat)
    for copy in range(FAT_COUNT):
        start = (RESERVED_SECTORS + copy * SECTORS_PER_FAT) * BYTES_PER_SECTOR
        image[start:start + len(table)] = table

    root_start = (RESERVED_SECTORS + FAT_COUNT * SECTORS_PER_FAT) * BYTES_PER_SECTOR
    image[root_start:root_start + len(root)] = root

    used_clusters = next_cluster - 2
    return FloppyImage(
        data=bytes(image),
        files={name: len(text.replace("\n", "\r\n").encode("ascii", "replace"))
               for name, text in files.items()},
        label=label,
        free_bytes=(DATA_CLUSTERS - used_clusters) * BYTES_PER_SECTOR,
    )


# --------------------------------------------------------------------------
# reading one back
# --------------------------------------------------------------------------

def inspect_image(data: bytes) -> dict:
    """Parse an image's BPB and root directory — used to verify our own output."""
    out: dict = {}
    if len(data) < BYTES_PER_SECTOR:
        return {"error": "shorter than one sector"}

    out["size"] = len(data)
    out["oem"] = data[3:11].decode("ascii", "replace").strip()
    out["bytes_per_sector"] = struct.unpack_from("<H", data, 11)[0]
    out["sectors_per_cluster"] = data[13]
    out["total_sectors"] = struct.unpack_from("<H", data, 19)[0]
    out["media_descriptor"] = f"0x{data[21]:02X}"
    out["sectors_per_fat"] = struct.unpack_from("<H", data, 22)[0]
    out["volume_label"] = data[43:54].decode("ascii", "replace").strip()
    out["fs_type"] = data[54:62].decode("ascii", "replace").strip()
    out["boot_signature"] = "0x%02X%02X" % (data[SIGNATURE_OFFSET + 1], data[SIGNATURE_OFFSET])
    out["bootable_signature_present"] = data[SIGNATURE_OFFSET:SIGNATURE_OFFSET + 2] == b"\x55\xAA"

    entries = []
    root_start = (RESERVED_SECTORS + FAT_COUNT * SECTORS_PER_FAT) * BYTES_PER_SECTOR
    for i in range(ROOT_ENTRIES):
        raw = data[root_start + i * 32:root_start + (i + 1) * 32]
        if not raw or raw[0] in (0x00,):
            break
        if raw[0] == 0xE5:
            continue
        attr = raw[11]
        if attr & 0x08:
            # A volume label is one 11-byte field, not name + extension.
            display = raw[0:11].decode("ascii", "replace").strip()
        else:
            name = raw[0:8].decode("ascii", "replace").strip()
            ext = raw[8:11].decode("ascii", "replace").strip()
            display = f"{name}.{ext}" if ext else name
        entry = {
            "name": display,
            "cluster": struct.unpack_from("<H", raw, 26)[0],
            "size": struct.unpack_from("<I", raw, 28)[0],
            "volume_label": bool(attr & 0x08),
        }
        entries.append(entry)
    out["entries"] = entries
    return out


def read_file(data: bytes, name: str) -> bytes:
    """Follow the FAT chain for one file, so we can prove the image is readable."""
    info = inspect_image(data)
    entry = next(
        (e for e in info.get("entries", [])
         if e["name"].upper() == name.upper() and not e["volume_label"]),
        None,
    )
    if entry is None:
        return b""

    fat_start = RESERVED_SECTORS * BYTES_PER_SECTOR
    out = bytearray()
    cluster = entry["cluster"]
    remaining = entry["size"]
    guard = 0
    while 2 <= cluster < 0xFF0 and remaining > 0 and guard < DATA_CLUSTERS:
        guard += 1
        sector = FIRST_DATA_SECTOR + (cluster - 2)
        start = sector * BYTES_PER_SECTOR
        take = min(BYTES_PER_SECTOR, remaining)
        out += data[start:start + take]
        remaining -= take

        offset = fat_start + (cluster * 3) // 2
        pair = struct.unpack_from("<H", data, offset)[0]
        cluster = (pair >> 4) if cluster % 2 else (pair & 0x0FFF)
    return bytes(out)
