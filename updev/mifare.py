"""What a MIFARE card *is*, as pure functions.

`nfc.py` moves bytes between the reader and the card. This module is the layer
above: what those bytes mean. It exists separately because none of it needs a
reader — geometry, access conditions and NDEF are all decidable from a dump,
so all of it is testable with a card nobody owns.

The centrepiece is `decode_access_bits()`. A MIFARE sector trailer carries
three bits per block group, stored twice (once inverted) so a corrupt trailer
is detectable, and those nine bits decide who may read, write, increment and
decrement every block in the sector. Editors that don't decode them can only
tell you a write failed; decoding them tells you it was never going to work,
and which key it would have taken.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "AccessBits",
    "CardLayout",
    "DEFAULT_KEYS",
    "NdefRecord",
    "block_permissions",
    "decode_access_bits",
    "layout_for_sak",
    "parse_ndef",
    "parse_ndef_message",
    "trailer_permissions",
]


# ==========================================================================
# geometry
# ==========================================================================

@dataclass(slots=True)
class CardLayout:
    """How many blocks this card has and where the sector boundaries fall."""

    name: str
    sectors: int = 0
    blocks: int = 0
    block_size: int = 16
    classic: bool = True        # MIFARE Classic (sector/trailer/keys) vs Ultralight

    def blocks_in(self, sector: int) -> list[int]:
        """Sectors 0-31 hold 4 blocks; 32-39 hold 16. The jump is why a 4K card
        is not simply "1K four times"."""
        if not self.classic:
            return []
        if sector < 32:
            base = sector * 4
            return list(range(base, base + 4))
        base = 128 + (sector - 32) * 16
        return list(range(base, base + 16))

    def sector_of(self, block: int) -> int:
        return block // 4 if block < 128 else 32 + (block - 128) // 16

    def trailer_of(self, sector: int) -> int:
        blocks = self.blocks_in(sector)
        return blocks[-1] if blocks else -1

    def is_trailer(self, block: int) -> bool:
        return self.classic and block == self.trailer_of(self.sector_of(block))

    def as_dict(self) -> dict:
        return {"name": self.name, "sectors": self.sectors, "blocks": self.blocks,
                "block_size": self.block_size, "classic": self.classic}


_LAYOUTS = {
    0x08: CardLayout("MIFARE Classic 1K", sectors=16, blocks=64),
    0x09: CardLayout("MIFARE Mini", sectors=5, blocks=20),
    0x18: CardLayout("MIFARE Classic 4K", sectors=40, blocks=256),
    0x19: CardLayout("MIFARE Classic 2K", sectors=32, blocks=128),
    0x00: CardLayout("MIFARE Ultralight / NTAG", sectors=0, blocks=0,
                     block_size=4, classic=False),
}


def layout_for_sak(sak: int) -> CardLayout | None:
    """SAK names the product, and the product fixes the geometry.

    Returns None for cards that have no block structure we can address — a
    DESFire (SAK 0x20) speaks ISO 7816 APDUs, not blocks, and pretending
    otherwise would produce a grid of garbage.
    """
    return _LAYOUTS.get(sak)


#: Keys that ship on cards or are published in the specs. Trying these is how
#: you find out a card is still on its factory key — it is not an attack, and
#: nothing here recovers a key that isn't on this list.
DEFAULT_KEYS: list[tuple[str, bytes]] = [
    ("factory / transport", bytes.fromhex("FFFFFFFFFFFF")),
    ("MAD (MIFARE Application Directory)", bytes.fromhex("A0A1A2A3A4A5")),
    ("NFC Forum NDEF public", bytes.fromhex("D3F7D3F7D3F7")),
    ("all zero", bytes.fromhex("000000000000")),
    ("B0B1…", bytes.fromhex("B0B1B2B3B4B5")),
    ("AABB…", bytes.fromhex("AABBCCDDEEFF")),
    ("Infineon default", bytes.fromhex("4D3A99C351DD")),
    ("Infineon default 2", bytes.fromhex("1A982C7E459A")),
]


# ==========================================================================
# access bits
# ==========================================================================

@dataclass(slots=True)
class AccessBits:
    """The nine bits from a sector trailer, and whether they were stored sanely.

    Bytes 6-8 hold C1/C2/C3 for four block groups, each stored twice: once
    plain and once inverted. `valid` is that cross-check. An invalid trailer is
    not a formatting quirk — the card itself refuses access to a sector whose
    inverted copy doesn't match, which is exactly how a sector gets bricked.
    """

    c1: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    c2: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    c3: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    valid: bool = True
    raw: bytes = b""

    def triple(self, group: int) -> tuple[int, int, int]:
        return self.c1[group], self.c2[group], self.c3[group]

    def as_dict(self) -> dict:
        return {"c1": self.c1, "c2": self.c2, "c3": self.c3, "valid": self.valid,
                "raw": self.raw.hex().upper()}


def decode_access_bits(trailer: bytes) -> AccessBits:
    """Bytes 6, 7 and 8 of a sector trailer.

        byte6:  C2' C1'      (both nibbles inverted)
        byte7:  C1  C3'
        byte8:  C3  C2

    Reading the plain copies from bytes 7 and 8 and checking them against the
    inverted copies is the whole of it — but the check matters, because an
    editor that shows access bits from a trailer the card would reject is
    telling you about a card that doesn't exist.
    """
    if len(trailer) < 9:
        return AccessBits(valid=False, raw=bytes(trailer))
    b6, b7, b8 = trailer[6], trailer[7], trailer[8]

    c1 = [(b7 >> (4 + i)) & 1 for i in range(4)]
    c2 = [(b8 >> i) & 1 for i in range(4)]
    c3 = [(b8 >> (4 + i)) & 1 for i in range(4)]

    inv_c1 = [(b6 >> i) & 1 for i in range(4)]
    inv_c2 = [(b6 >> (4 + i)) & 1 for i in range(4)]
    inv_c3 = [(b7 >> i) & 1 for i in range(4)]

    valid = all(
        c[i] != inv[i]
        for c, inv in ((c1, inv_c1), (c2, inv_c2), (c3, inv_c3))
        for i in range(4)
    )
    return AccessBits(c1=c1, c2=c2, c3=c3, valid=valid, raw=trailer[6:9])


#: (C1,C2,C3) -> (read, write, increment, decrement/transfer/restore) for a
#: data block. "A|B" means either key opens it; "—" means no key ever will.
_DATA_ACCESS: dict[tuple[int, int, int], tuple[str, str, str, str]] = {
    (0, 0, 0): ("A|B", "A|B", "A|B", "A|B"),
    (0, 1, 0): ("A|B", "—", "—", "—"),
    (1, 0, 0): ("A|B", "B", "—", "—"),
    (1, 1, 0): ("A|B", "B", "B", "A|B"),
    (0, 0, 1): ("A|B", "—", "—", "A|B"),
    (0, 1, 1): ("B", "B", "—", "—"),
    (1, 0, 1): ("B", "—", "—", "—"),
    (1, 1, 1): ("—", "—", "—", "—"),
}

#: (C1,C2,C3) -> what the trailer itself permits, in the order
#: (key A read, key A write, access-bit read, access-bit write, key B read,
#: key B write). Key A is never readable by anyone, on any card, ever.
_TRAILER_ACCESS: dict[tuple[int, int, int], tuple[str, ...]] = {
    (0, 0, 0): ("—", "A", "A", "—", "A", "A"),
    (0, 1, 0): ("—", "—", "A", "—", "A", "—"),
    (1, 0, 0): ("—", "B", "A|B", "—", "—", "B"),
    (1, 1, 0): ("—", "—", "A|B", "—", "—", "—"),
    (0, 0, 1): ("—", "A", "A", "A", "A", "A"),
    (0, 1, 1): ("—", "B", "A|B", "B", "—", "B"),
    (1, 0, 1): ("—", "—", "A|B", "B", "—", "—"),
    (1, 1, 1): ("—", "—", "A|B", "—", "—", "—"),
}


def block_permissions(bits: AccessBits, group: int) -> dict[str, str]:
    """Which key opens which operation on the data blocks of one group."""
    read, write, increment, decrement = _DATA_ACCESS.get(
        bits.triple(group), ("?", "?", "?", "?"))
    return {"read": read, "write": write, "increment": increment,
            "decrement": decrement}


def trailer_permissions(bits: AccessBits) -> dict[str, str]:
    """Group 3 governs the trailer itself — including whether the access bits
    can ever be changed again. `(1,1,1)` on a trailer is the one-way door."""
    values = _TRAILER_ACCESS.get(bits.triple(3), ("?",) * 6)
    keys = ("key_a_read", "key_a_write", "access_read", "access_write",
            "key_b_read", "key_b_write")
    return dict(zip(keys, values))


def group_of(layout: CardLayout, block: int) -> int:
    """Which of the four access groups a block falls in.

    In a 4-block sector each block is its own group. In a 16-block sector the
    first three groups cover five blocks each — a detail that silently breaks
    any decoder written only against 1K cards.
    """
    sector = layout.sector_of(block)
    blocks = layout.blocks_in(sector)
    if not blocks:
        return 0
    index = blocks.index(block)
    if len(blocks) == 4:
        return index
    return 3 if index == 15 else min(2, index // 5)


def describe_permissions(layout: CardLayout, block: int, trailer: bytes) -> dict[str, str]:
    """The one call an editor needs: what can be done to this block, and with
    which key, given its sector's trailer."""
    bits = decode_access_bits(trailer)
    if layout.is_trailer(block):
        return trailer_permissions(bits)
    return block_permissions(bits, group_of(layout, block))


# ==========================================================================
# NDEF
# ==========================================================================

_URI_PREFIXES = [
    "", "http://www.", "https://www.", "http://", "https://", "tel:", "mailto:",
    "ftp://anonymous:anonymous@", "ftp://ftp.", "ftps://", "sftp://", "smb://",
    "nfs://", "ftp://", "dav://", "news:", "telnet://", "imap:", "rtsp://",
    "urn:", "pop:", "sip:", "sips:", "tftp:", "btspp://", "btl2cap://",
    "btgoep://", "tcpobex://", "irdaobex://", "file://", "urn:epc:id:",
    "urn:epc:tag:", "urn:epc:pat:", "urn:epc:raw:", "urn:epc:", "urn:nfc:",
]

_TNF = {
    0x00: "empty", 0x01: "well-known", 0x02: "MIME", 0x03: "absolute URI",
    0x04: "external", 0x05: "unknown", 0x06: "unchanged", 0x07: "reserved",
}


@dataclass(slots=True)
class NdefRecord:
    tnf: int
    type: bytes
    payload: bytes
    identifier: bytes = b""

    @property
    def kind(self) -> str:
        return _TNF.get(self.tnf, f"TNF {self.tnf}")

    @property
    def text(self) -> str:
        """The human-readable content, for the record types that have one.

        A well-known 'T' record starts with a status byte whose low bits give
        the language-code length; a 'U' record starts with an index into a
        prefix table, which is how a URI fits in a tag with 46 usable bytes.
        """
        if self.tnf == 0x01 and self.type == b"T" and self.payload:
            status = self.payload[0]
            lang_len = status & 0x3F
            encoding = "utf-16" if status & 0x80 else "utf-8"
            body = self.payload[1 + lang_len:]
            return body.decode(encoding, "replace")
        if self.tnf == 0x01 and self.type == b"U" and self.payload:
            prefix = _URI_PREFIXES[self.payload[0]] if self.payload[0] < len(_URI_PREFIXES) else ""
            return prefix + self.payload[1:].decode("utf-8", "replace")
        return self.payload.decode("utf-8", "replace")

    @property
    def label(self) -> str:
        type_name = self.type.decode("ascii", "replace") or "—"
        return f"{self.kind} · {type_name}"

    def as_dict(self) -> dict:
        return {"tnf": self.tnf, "kind": self.kind,
                "type": self.type.decode("ascii", "replace"),
                "text": self.text, "bytes": len(self.payload)}


def parse_ndef_message(data: bytes) -> list[NdefRecord]:
    """Records out of one NDEF message. Stops cleanly on a truncated tail."""
    records: list[NdefRecord] = []
    offset = 0
    while offset < len(data):
        header = data[offset]
        if header == 0x00:
            break
        short = bool(header & 0x10)
        has_id = bool(header & 0x08)
        tnf = header & 0x07
        offset += 1
        if offset >= len(data):
            break
        type_len = data[offset]
        offset += 1
        if short:
            if offset >= len(data):
                break
            payload_len = data[offset]
            offset += 1
        else:
            if offset + 4 > len(data):
                break
            payload_len = int.from_bytes(data[offset:offset + 4], "big")
            offset += 4
        id_len = 0
        if has_id:
            if offset >= len(data):
                break
            id_len = data[offset]
            offset += 1
        record_type = data[offset:offset + type_len]
        offset += type_len
        identifier = data[offset:offset + id_len]
        offset += id_len
        payload = data[offset:offset + payload_len]
        offset += payload_len
        if len(payload) < payload_len:
            break                                  # truncated dump, not an error
        records.append(NdefRecord(tnf, record_type, payload, identifier))
        if header & 0x40:                          # ME — message end
            break
    return records


def parse_ndef(dump: bytes) -> list[NdefRecord]:
    """Find the NDEF message inside a raw dump and parse it.

    Data on a formatted card is wrapped in TLVs: 0x03 is the NDEF message,
    0x00 is padding, 0xFE terminates. Sector trailers sit in the middle of a
    Classic dump and are not part of the data, so this scans for the TLV rather
    than assuming an offset.
    """
    for start in range(len(dump)):
        if dump[start] != 0x03:
            continue
        rest = dump[start + 1:]
        if not rest:
            continue
        length = rest[0]
        body_at = 1
        if length == 0xFF:                         # three-byte length form
            if len(rest) < 3:
                continue
            length = int.from_bytes(rest[1:3], "big")
            body_at = 3
        if length == 0 or length > len(rest) - body_at:
            continue
        records = parse_ndef_message(rest[body_at:body_at + length])
        if records:
            return records
    return []
