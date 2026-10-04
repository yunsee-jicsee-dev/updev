"""Recognising algorithms in a compiled binary, by their constants.

Most classic algorithms carry a number that nothing else has any reason to
contain. 0xEDB88320 is the reversed CRC-32 polynomial; 1103515245 is the
multiplier ANSI C specified for `rand`; 0x5F3759DF is the fast inverse square
root's magic constant and appears nowhere else in computing. Find one of those
in a binary and you have not guessed at the algorithm, you have identified it.

That is the whole method here, and its limits are the point:

  * **A constant is evidence, not proof.** The same 32 bits can be a jump
    offset or a piece of a string. So every hit is reported with where it was
    found and how strong it is, and a verdict is a ranked argument rather than
    an answer.
  * **Absence proves nothing.** A table-driven CRC has the polynomial baked
    into 256 precomputed words and the constant itself never appears; an
    optimiser can fold a shift into an address mode. Not finding a signature
    is not evidence against it, and this module never claims otherwise.
  * **Structure is not read.** Nothing here disassembles or follows control
    flow. It reads bytes and, if a binary is not stripped, symbol names.

The pattern follows `usbclass.py` deliberately: weighted signatures, evidence
carried with the verdict, and the reasoning visible so a wrong answer can be
argued with instead of merely disbelieved.

Everything is read-only and offline: files are opened for reading and nothing
is executed. A binary is data here, never a program.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "Evidence",
    "Finding",
    "Report",
    "Signature",
    "SIGNATURES",
    "analyse",
    "signature_reference",
]


class Strength(StrEnum):
    """How much one hit is worth."""

    UNIQUE = "unique"      # this number means one thing and nothing else
    STRONG = "strong"      # rare enough that coincidence is unlikely
    WEAK = "weak"          # suggestive; needs company to mean anything


_WEIGHT = {Strength.UNIQUE: 100, Strength.STRONG: 45, Strength.WEAK: 12}


@dataclass(slots=True)
class Signature:
    """One recognisable marker of an algorithm."""

    algorithm: str
    label: str                      # Korean, for display
    detail: str                     # what the marker actually is
    strength: Strength
    #: Integer constants, in the widths worth searching for.
    constants: tuple[int, ...] = ()
    widths: tuple[int, ...] = (4,)
    #: Symbol/string fragments that betray the algorithm by name.
    names: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "label": self.label,
            "detail": self.detail,
            "strength": str(self.strength),
            "weight": _WEIGHT[self.strength],
            "constants": [f"0x{c:X}" if c > 0xFFFF else str(c) for c in self.constants],
            "names": list(self.names),
        }


# --------------------------------------------------------------------------
# the signature table
#
# Every constant here is one a person chose for a published reason. Where a
# number has a second life elsewhere it is marked weak, and where it exists
# only inside one algorithm it is unique.
# --------------------------------------------------------------------------

SIGNATURES: tuple[Signature, ...] = (
    Signature(
        "LCG", "선형 합동 난수 생성기",
        "ANSI C rand() 의 승수 1103515245 와 증분 12345",
        Strength.UNIQUE,
        constants=(1103515245, 12345),
    ),
    Signature(
        "LCG", "선형 합동 난수 생성기 (MSVC)",
        "MSVC rand() 의 214013 / 2531011",
        Strength.UNIQUE,
        constants=(214013, 2531011),
    ),
    Signature(
        "LCG", "선형 합동 난수 생성기 (Numerical Recipes)",
        "1664525 / 1013904223",
        Strength.UNIQUE,
        constants=(1664525, 1013904223),
    ),
    Signature(
        "Mersenne Twister", "메르센 트위스터",
        "MT19937 의 tempering 마스크 0x9D2C5680 / 0xEFC60000",
        Strength.UNIQUE,
        constants=(0x9D2C5680, 0xEFC60000, 0x9908B0DF),
    ),
    Signature(
        "xorshift", "xorshift 계열 난수",
        "Marsaglia 의 상수 2685821657736338717",
        Strength.STRONG,
        constants=(0x2545F4914F6CDD1D,), widths=(8,),
    ),
    Signature(
        "CRC-32", "CRC-32 체크섬",
        "역순 다항식 0xEDB88320 (또는 정순 0x04C11DB7)",
        Strength.UNIQUE,
        constants=(0xEDB88320, 0x04C11DB7),
    ),
    Signature(
        "CRC-32C", "CRC-32C (Castagnoli)",
        "다항식 0x82F63B78",
        Strength.UNIQUE,
        constants=(0x82F63B78,),
    ),
    Signature(
        "MD5", "MD5 해시",
        "초기 체이닝 값 0x67452301 / 0xEFCDAB89",
        Strength.STRONG,
        constants=(0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476),
    ),
    Signature(
        "SHA-256", "SHA-256 해시",
        "초기 해시값 0x6A09E667 — 2의 제곱근 소수부",
        Strength.UNIQUE,
        constants=(0x6A09E667, 0xBB67AE85, 0x428A2F98),
    ),
    Signature(
        "SHA-1", "SHA-1 해시",
        "라운드 상수 0x5A827999 / 0x6ED9EBA1",
        Strength.STRONG,
        constants=(0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xCA62C1D6),
    ),
    Signature(
        "AES", "AES 블록 암호",
        "S-box 앞머리 63 7C 77 7B F2 6B 6F C5",
        Strength.UNIQUE,
        names=(),
        constants=(),
    ),
    Signature(
        "fast inverse sqrt", "고속 역제곱근",
        "매직 상수 0x5F3759DF — Quake III 계보",
        Strength.UNIQUE,
        constants=(0x5F3759DF,),
    ),
    Signature(
        "VGA mode 13h", "VGA 320x200 256색",
        "320x200 해상도와 0xA000 프레임버퍼 세그먼트",
        Strength.STRONG,
        constants=(0xA000, 64000), widths=(2, 4),
    ),
    Signature(
        "PIT square wave", "8253 PIT 구형파",
        "PC 스피커 기준 주파수 1193182 Hz",
        Strength.UNIQUE,
        constants=(1193182, 1193180),
    ),
    Signature(
        "fixed point 16.16", "16.16 고정소수점",
        "65536 을 스케일로 쓰는 정수 산술",
        Strength.WEAK,
        constants=(65536,),
    ),
    Signature(
        "FNV-1a", "FNV-1a 해시",
        "offset basis 2166136261, prime 16777619",
        Strength.UNIQUE,
        constants=(2166136261, 16777619),
    ),
    Signature(
        "djb2", "djb2 해시",
        "초기값 5381 과 승수 33",
        Strength.WEAK,
        constants=(5381,),
    ),
    Signature(
        "Base64", "Base64 인코딩",
        "표준 알파벳 문자열",
        Strength.STRONG,
        names=("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",),
    ),
    Signature(
        "zlib/deflate", "zlib / DEFLATE 압축",
        "zlib 버전 문자열 또는 inflate 심볼",
        Strength.STRONG,
        names=("inflate", "deflate", "zlib"),
    ),
    Signature(
        "quicksort", "퀵소트",
        "libc qsort 심볼",
        Strength.WEAK,
        names=("qsort",),
    ),
    Signature(
        "Bresenham", "브레젠험 선/원 알고리즘",
        "심볼 이름에 드러난 경우에 한함",
        Strength.WEAK,
        names=("bresenham",),
    ),
    Signature(
        "isqrt (odd subtraction)", "정수 제곱근 (홀수 뺄셈)",
        "심볼 이름에 드러난 경우에 한함",
        Strength.WEAK,
        names=("isqrt",),
    ),
)

#: Byte patterns that are not integers — searched literally.
_BYTE_PATTERNS: tuple[tuple[str, bytes, Strength, str], ...] = (
    ("AES", bytes((0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5)),
     Strength.UNIQUE, "AES S-box 앞 8바이트"),
    ("SHA-256", bytes((0x98, 0x2F, 0x8A, 0x42)),
     Strength.STRONG, "SHA-256 라운드 상수 테이블 시작 (LE)"),
)


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Evidence:
    """One hit, and where it was."""

    what: str                       # the marker that matched
    where: str                      # "constant", "symbol", "string", "bytes"
    offset: int = -1                # byte offset, when there is one
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {"what": self.what, "where": self.where, "detail": self.detail}
        if self.offset >= 0:
            out["offset"] = f"0x{self.offset:X}"
        return out


@dataclass(slots=True)
class Finding:
    """One algorithm, with everything that pointed at it."""

    algorithm: str
    label: str
    score: int = 0
    strength: Strength = Strength.WEAK
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def certain(self) -> bool:
        return self.strength is Strength.UNIQUE

    def as_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "label": self.label,
            "score": self.score,
            "strength": str(self.strength),
            "certain": self.certain,
            "evidence": [e.as_dict() for e in self.evidence],
        }


@dataclass(slots=True)
class Report:
    path: str
    size: int = 0
    kind: str = ""
    stripped: bool | None = None
    findings: list[Finding] = field(default_factory=list)
    symbols: int = 0
    error: str = ""

    @property
    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.findings:
            return "알려진 시그니처 없음 — 없다는 증거는 아닙니다"
        best = self.findings[0]
        more = f" 외 {len(self.findings) - 1}개" if len(self.findings) > 1 else ""
        return f"{best.label}{more}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "kind": self.kind,
            "stripped": self.stripped,
            "symbols": self.symbols,
            "summary": self.summary,
            "error": self.error,
            "findings": [f.as_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------

#: Printable runs of at least this length are treated as strings.
_MIN_STRING = 6
_STRING_RE = re.compile(rb"[\x20-\x7E]{%d,}" % _MIN_STRING)


def _encodings(value: int, widths: tuple[int, ...]) -> list[bytes]:
    """Every byte encoding of an integer worth searching for.

    Both endiannesses, because a file can be either and the cost of checking
    is nothing. Values that do not fit a width are skipped rather than
    truncated — a truncated constant would match things that are not it.
    """
    out: list[bytes] = []
    for width in widths:
        fmt = {2: "H", 4: "I", 8: "Q"}.get(width)
        if fmt is None or value < 0 or value >= (1 << (width * 8)):
            continue
        for order in ("<", ">"):
            try:
                out.append(struct.pack(order + fmt, value))
            except struct.error:
                continue
    return out


#: AArch64 `mov` / `movk` with a 16-bit immediate, as 32-bit little-endian
#: instruction words.
#:
#: A fixed-width ISA cannot put a 32-bit constant in an instruction, so the
#: compiler builds it in halves: `mov w8, #0x4e6d` then `movk w8, #0x41c6,
#: lsl #16`. The constant therefore never exists as four contiguous bytes
#: anywhere in the file, and a byte search for it — the entire method of this
#: module — finds nothing at all.
#:
#: This was not hypothetical. The same LCG that byte-matches instantly in the
#: DOS build of a program is invisible in its AArch64 port for exactly this
#: reason. Searching the immediate fields recovers it.
_MOVZ_MASK = 0x7F800000
_MOVZ_W = 0x52800000            # MOVZ Wd, #imm16
_MOVK_W = 0x72800000            # MOVK Wd, #imm16, LSL #shift


def _aarch64_immediates(blob: bytes) -> dict[int, int]:
    """Every 16-bit immediate a MOVZ/MOVK builds, to the offset it was at.

    Only the immediate is taken, not the destination register or the shift:
    pairing halves back into the register they were assembled in would need
    dataflow, and for recognising a constant it is enough to know that both
    halves are present in the same binary.
    """
    out: dict[int, int] = {}
    for off in range(0, len(blob) - 3, 4):
        word = int.from_bytes(blob[off:off + 4], "little")
        op = word & _MOVZ_MASK
        if op not in (_MOVZ_W, _MOVK_W, _MOVZ_W | 0x80000000, _MOVK_W | 0x80000000):
            continue
        imm = (word >> 5) & 0xFFFF
        out.setdefault(imm, off)
    return out


def _split_halves(value: int) -> tuple[int, int] | None:
    """(low 16, high 16) for a value that needs both, else None."""
    if value <= 0xFFFF or value > 0xFFFFFFFF:
        return None
    return value & 0xFFFF, (value >> 16) & 0xFFFF


def _find_all(blob: bytes, needle: bytes, limit: int = 4) -> list[int]:
    offsets: list[int] = []
    start = 0
    while len(offsets) < limit:
        at = blob.find(needle, start)
        if at < 0:
            break
        offsets.append(at)
        start = at + 1
    return offsets


def _elf_symbols(blob: bytes) -> tuple[list[str], bool | None]:
    """Function names from an ELF symbol table, without an ELF parser.

    Symbol *names* live in a string table that is just NUL-separated text, so
    the printable-run scan already has them; what is needed is to know whether
    the table is there at all. `.symtab` present and non-empty is the test,
    and its absence is what "stripped" means.
    """
    if not blob.startswith(b"\x7fELF"):
        return [], None
    stripped = b".symtab" not in blob
    return [], stripped


def _kind_of(blob: bytes) -> str:
    if blob.startswith(b"\x7fELF"):
        return "ELF"
    if blob[:2] in (b"MZ", b"ZM"):
        return "PE/DOS EXE"
    if blob[:4] in (b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe"):
        return "Mach-O"
    if len(blob) >= 512 and blob[510:512] == b"\x55\xaa":
        return "부트섹터 / 디스크 이미지"
    if b"\x00" not in blob[:4096]:
        return "텍스트"
    return "raw binary"


def analyse(path: str | Path, max_bytes: int = 64 * 1024 * 1024) -> Report:
    """Read a file and report which algorithms its constants point to.

    Read-only. The file is opened, read and closed; nothing is executed and
    nothing is written.
    """
    path = Path(path)
    report = Report(path=str(path))
    try:
        blob = path.read_bytes()[:max_bytes]
    except OSError as e:
        report.error = f"읽을 수 없습니다: {e.strerror or e}"
        return report

    report.size = len(blob)
    if not blob:
        report.error = "빈 파일입니다"
        return report

    report.kind = _kind_of(blob)
    _, stripped = _elf_symbols(blob)
    report.stripped = stripped

    text = {s.decode("ascii", "replace") for s in _STRING_RE.findall(blob)}
    report.symbols = len(text)
    lowered = [t.lower() for t in text]

    found: dict[str, Finding] = {}

    def note(algorithm: str, label: str, strength: Strength, ev: Evidence) -> None:
        finding = found.get(algorithm)
        if finding is None:
            finding = Finding(algorithm=algorithm, label=label, strength=strength)
            found[algorithm] = finding
        finding.evidence.append(ev)
        finding.score += _WEIGHT[strength]
        # A finding is as strong as its strongest single piece of evidence.
        if _WEIGHT[strength] > _WEIGHT[finding.strength]:
            finding.strength = strength

    # Only worth building for AArch64, and only once.
    immediates = _aarch64_immediates(blob) if blob[:4] == b"\x7fELF" and \
        len(blob) > 20 and blob[18:20] == b"\xb7\x00" else {}

    for sig in SIGNATURES:
        for value in sig.constants:
            hit = False
            for encoded in _encodings(value, sig.widths):
                offsets = _find_all(blob, encoded)
                if not offsets:
                    continue
                shown = value if value <= 0xFFFF else f"0x{value:X}"
                note(sig.algorithm, sig.label, sig.strength, Evidence(
                    what=str(shown), where="constant", offset=offsets[0],
                    detail=f"{sig.detail} — {len(offsets)}곳",
                ))
                hit = True
                break                       # one encoding is enough per constant

            if hit or not immediates:
                continue

            # Not present as bytes. On AArch64 that is expected rather than
            # meaningful, so look for the two halves a MOVZ/MOVK pair would
            # have used. Both must be there: either half alone is a common
            # enough 16-bit number to mean nothing.
            halves = _split_halves(value)
            if halves and halves[0] in immediates and halves[1] in immediates:
                note(sig.algorithm, sig.label, sig.strength, Evidence(
                    what=f"0x{value:X}", where="immediate",
                    offset=immediates[halves[0]],
                    detail=(f"{sig.detail} — MOVZ/MOVK 즉치값 "
                            f"0x{halves[0]:04X} + 0x{halves[1]:04X}"),
                ))
        for needle in sig.names:
            low = needle.lower()
            hits = [t for t in lowered if low in t]
            if hits:
                note(sig.algorithm, sig.label, sig.strength, Evidence(
                    what=needle, where="symbol", detail=sig.detail,
                ))

    for algorithm, pattern, strength, detail in _BYTE_PATTERNS:
        offsets = _find_all(blob, pattern)
        if offsets:
            label = next((s.label for s in SIGNATURES if s.algorithm == algorithm),
                         algorithm)
            note(algorithm, label, strength, Evidence(
                what=pattern.hex(" ").upper(), where="bytes", offset=offsets[0],
                detail=detail,
            ))

    report.findings = sorted(found.values(), key=lambda f: (-f.score, f.algorithm))
    return report


def signature_reference() -> list[dict[str, Any]]:
    """The rule table, for `updev algo --signatures`."""
    return [s.as_dict() for s in SIGNATURES]
