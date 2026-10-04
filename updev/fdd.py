"""Managing a floppy *drive*, as opposed to building a floppy *image*.

`floppy.py` assembles 1.44 MB images byte by byte. This module deals with the
physical thing: is a drive attached, is there a disk in it, can the disk still
be read, and what is on it.

Floppies are the one storage medium where "the device is fine" and "your data
is fine" routinely disagree. A 30-year-old disk in a working drive will read
its boot sector instantly and then throw I/O errors somewhere in the data
area, and nothing in `lsblk` will tell you — the capacity still says 1.4M,
the device still says online. The only way to know is to read every sector and
see which ones answer.

So the central operation here is a surface scan, and the design follows from
one fact: **a bad sector is slow, not just wrong.** The drive retries, the USB
bridge retries, and a single unreadable sector can cost seconds. Reading 2,880
sectors one at a time would be correct and unusably slow, so the scan reads in
tracks and only falls back to sector-at-a-time inside a track that failed.
Good media costs one pass; bad media costs one pass plus detail work exactly
where it is needed.

Everything here is read-only. Nothing in this module writes to a disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .core.model import Device, ScanResult
from .floppy import FLOPPY_SIZE
from .usbclass import UsbClass

__all__ = [
    "SECTOR",
    "Drive",
    "Medium",
    "Surface",
    "find_drives",
    "read_medium",
    "surface_scan",
]

SECTOR = 512
SECTORS_PER_TRACK = 18
TOTAL_SECTORS = FLOPPY_SIZE // SECTOR          # 2,880

#: The first 33 sectors are boot sector, both FATs and the root directory.
#: A bad sector here costs you the whole disk; past it, one file.
SYSTEM_SECTORS = 33


# --------------------------------------------------------------------------
# drives
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Drive:
    """A USB floppy drive, and the block node its medium appears as."""

    uid: str
    label: str
    node: str = ""                  # /dev/sdb, or "" when no medium is exposed
    usb_address: str = ""
    size: int = 0                   # bytes the kernel reports for the medium

    @property
    def has_medium(self) -> bool:
        """A drive with no disk in it reports zero capacity, not absence.

        The block device stays; it just becomes 0 bytes. Treating size as the
        presence test is what the kernel actually gives us.
        """
        return self.size > 0

    @property
    def standard_geometry(self) -> bool:
        return self.size == FLOPPY_SIZE

    def as_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "label": self.label,
            "node": self.node,
            "usb_address": self.usb_address,
            "size": self.size,
            "has_medium": self.has_medium,
            "standard_geometry": self.standard_geometry,
        }


def find_drives(result: ScanResult) -> list[Drive]:
    """Every floppy drive in a scan, with its medium attached.

    The drive is a USB device tagged FUSB by `usbclass.py`; the medium is a
    block device that the storage backend already parented to it. Joining them
    here means neither backend needs to know about floppies.
    """
    drives: list[Drive] = []
    for dev in result.devices:
        if str(UsbClass.FUSB) not in dev.tags:
            continue
        drive = Drive(uid=dev.uid, label=dev.label, usb_address=dev.address)
        block = _medium_for(dev, result)
        if block is not None:
            drive.node = block.node
            drive.size = int(block.metrics.get("size_bytes", 0))
        drives.append(drive)
    return drives


def _medium_for(drive: Device, result: ScanResult) -> Device | None:
    """The block device that is this drive's disk.

    Storage hangs USB disks off the USB device they arrived on, so the medium
    is simply the child whose parent is the drive.
    """
    for dev in result.devices:
        if dev.parent == drive.uid and dev.node.startswith("/dev/"):
            return dev
    return None


# --------------------------------------------------------------------------
# surface scan
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Surface:
    """Which sectors answered and which did not."""

    total: int = 0
    bad: list[int] = field(default_factory=list)
    scanned: int = 0
    aborted: str = ""               # why the scan stopped early, if it did

    @property
    def good(self) -> int:
        return self.scanned - len(self.bad)

    @property
    def healthy(self) -> bool:
        return self.scanned > 0 and not self.bad

    @property
    def system_area_damaged(self) -> bool:
        """A bad sector in the FATs or root directory, which costs everything."""
        return any(s < SYSTEM_SECTORS for s in self.bad)

    def bad_ranges(self) -> list[tuple[int, int]]:
        """Collapse the bad list into runs — damage is contiguous far more
        often than it is scattered, and 12 ranges read better than 400 numbers."""
        if not self.bad:
            return []
        runs: list[tuple[int, int]] = []
        start = previous = self.bad[0]
        for sector in self.bad[1:]:
            if sector == previous + 1:
                previous = sector
                continue
            runs.append((start, previous))
            start = previous = sector
        runs.append((start, previous))
        return runs

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "scanned": self.scanned,
            "good": self.good,
            "bad": len(self.bad),
            "bad_ranges": [list(r) for r in self.bad_ranges()],
            "healthy": self.healthy,
            "system_area_damaged": self.system_area_damaged,
            "aborted": self.aborted,
        }


#: Default wall-clock ceiling for a surface scan, in seconds.
#:
#: Measured on a TEAC USB drive: a clean track costs milliseconds, but a track
#: containing bad sectors costs *seconds* — the drive retries, then the USB
#: bridge retries. A disk with a damaged region can therefore take longer to
#: scan than anyone will sit through, and a bad-sector count alone does not
#: bound it, because the cost is per retry rather than per sector. Wall clock
#: is the only budget that actually holds.
DEFAULT_BUDGET = 90.0


def surface_scan(
    node: str,
    total: int = TOTAL_SECTORS,
    give_up_after: int = 0,
    budget: float = DEFAULT_BUDGET,
) -> Iterator[Surface]:
    """Read every sector, reporting progress. Read-only, always.

    Yields a running `Surface` once per track so a caller can draw a progress
    bar — a full pass takes the better part of a minute on good media and can
    run for many minutes on failing media.

    Two stops, because they bound different things. `give_up_after` caps how
    much damage is worth enumerating: past a few hundred bad sectors the disk
    is a write-off and the rest is detail. `budget` caps wall clock, which is
    what actually runs away — see `DEFAULT_BUDGET`. Pass 0 to either to
    disable it.

    A scan that stops early says so in `aborted`, and its `bad` list stays
    truthful: those sectors really are bad, there are simply more sectors it
    never reached.
    """
    import time

    surface = Surface(total=total)
    try:
        fd = os.open(node, os.O_RDONLY)
    except OSError as e:
        surface.aborted = f"{node} 을 열 수 없습니다: {e}"
        yield surface
        return

    deadline = (time.monotonic() + budget) if budget else 0.0
    try:
        for start in range(0, total, SECTORS_PER_TRACK):
            count = min(SECTORS_PER_TRACK, total - start)
            if _read_at(fd, start, count) is None:
                # The track failed as a unit; find out which sectors in it are
                # actually bad rather than condemning all eighteen.
                for sector in range(start, start + count):
                    if _read_at(fd, sector, 1) is None:
                        surface.bad.append(sector)
                    surface.scanned += 1
            else:
                surface.scanned += count

            if give_up_after and len(surface.bad) >= give_up_after:
                surface.aborted = f"배드섹터 {len(surface.bad)}개에서 중단"
                yield surface
                return
            if deadline and time.monotonic() >= deadline:
                done = surface.scanned / total * 100
                surface.aborted = (
                    f"{budget:.0f}초 예산 초과 — {done:.0f}%까지만 확인했습니다"
                )
                yield surface
                return
            yield surface
    finally:
        os.close(fd)

    yield surface


def _read_at(fd: int, sector: int, count: int) -> bytes | None:
    """One positioned read. None means the medium refused it."""
    try:
        data = os.pread(fd, count * SECTOR, sector * SECTOR)
    except OSError:
        return None
    return data if len(data) == count * SECTOR else None


# --------------------------------------------------------------------------
# reading the medium
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Medium:
    """What is on the disk, as far as it can be read."""

    node: str
    size: int = 0
    readable: bool = False
    fat12: bool = False
    oem: str = ""
    label: str = ""
    files: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    info: dict[str, Any] = field(default_factory=dict)
    #: The first 33 sectors, kept so a file's FAT chain can be walked without
    #: reading the disk a second time.
    system: bytes = b""

    @property
    def summary(self) -> str:
        if not self.readable:
            return self.error or "읽을 수 없음"
        if not self.fat12:
            return "FAT12가 아님 — 포맷되지 않았거나 다른 파일시스템"
        bits = [self.label or "(레이블 없음)", f"파일 {len(self.files)}개"]
        if self.oem:
            bits.append(f"포맷: {self.oem}")
        return " · ".join(bits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "size": self.size,
            "readable": self.readable,
            "fat12": self.fat12,
            "oem": self.oem,
            "label": self.label,
            "files": self.files,
            "error": self.error,
            "summary": self.summary,
        }


def read_medium(node: str) -> Medium:
    """Read the system area and decode it with the image inspector.

    Only the first 33 sectors are needed to describe the disk, and reading
    just those keeps this fast enough to run on every `fly floppy` — the data
    area is what takes a minute, and that is the surface scan's job.

    The medium is opened directly rather than mounted. Mounting a failing
    floppy hands it to the FAT driver, which will retry, block, and sometimes
    wedge the whole USB bridge; reading sectors cannot.
    """
    medium = Medium(node=node)
    try:
        medium.size = os.stat(node).st_size or _size_via_seek(node)
    except OSError:
        pass

    try:
        with open(node, "rb") as fh:
            system = fh.read(SYSTEM_SECTORS * SECTOR)
    except OSError as e:
        medium.error = f"{e.strerror or e}"
        return medium

    if len(system) < SYSTEM_SECTORS * SECTOR:
        medium.error = "시스템 영역을 끝까지 읽지 못했습니다 — 디스켓이 없거나 손상"
        return medium

    medium.readable = True

    # `inspect_image` wants a whole image; give it one padded with zeros. The
    # BPB, the boot signature and the root directory all live in what we read,
    # so nothing it reports depends on the padding.
    from .floppy import inspect_image

    try:
        info = inspect_image(system + b"\0" * (FLOPPY_SIZE - len(system)))
    except Exception as e:                       # a garbage BPB, not our bug
        medium.error = f"부트섹터를 해석할 수 없습니다: {e}"
        return medium

    medium.info = info
    medium.system = system
    medium.fat12 = bool(info.get("bootable_signature_present")) and \
        info.get("bytes_per_sector") == SECTOR
    medium.oem = str(info.get("oem") or "").strip()
    medium.label = str(info.get("volume_label") or "").strip()
    medium.files = [e for e in info.get("entries", []) if not e.get("volume_label")]
    return medium


#: What a drive's medium is doing, as a number the fly can smell.
#: 0.0 present and registered · 0.5 in, not yet registered · 1.0 not there.
MEDIUM_PRESENT = 0.0
MEDIUM_SETTLING = 0.5
MEDIUM_ABSENT = 1.0


def probe_medium(node: str, reported_size: int) -> float:
    """Is there a disk in there — asked of the drive, not of the kernel.

    Capacity is a lagging indicator. Measured on a TEAC drive at 20Hz, with a
    disk going in: sector 0 became readable at t=4.41s and `/sys/block/sdb/size`
    only caught up at t=4.60. For 190 milliseconds the medium was in, readable,
    and reported as absent.

    That gap is a state of its own, and it is the one nobody can see. A watcher
    polling capacity has two states and learns two; one that also asks the
    drive has three, and the third is the only one that says "this is happening
    right now" rather than "this has happened".

    Read-only: one 512-byte read at offset 0. An empty drive answers ENOMEDIUM
    immediately rather than spinning, so this costs nothing when there is
    nothing there — which is the case it runs in most often.
    """
    try:
        fd = os.open(node, os.O_RDONLY)
    except OSError:
        return MEDIUM_ABSENT if reported_size <= 0 else MEDIUM_PRESENT
    try:
        readable = len(os.pread(fd, SECTOR, 0)) == SECTOR
    except OSError:
        readable = False
    finally:
        os.close(fd)

    if not readable:
        return MEDIUM_ABSENT
    return MEDIUM_PRESENT if reported_size > 0 else MEDIUM_SETTLING


#: Beyond this a file is summarised rather than shown. A floppy holds 1.44MB
#: and a terminal does not.
PREVIEW_BYTES = 2048


def _fat12_next(fat: bytes, cluster: int) -> int:
    """The next cluster in a chain. FAT12 packs three nibbles per entry, so
    every other one straddles a byte boundary."""
    offset = cluster + cluster // 2
    if offset + 1 >= len(fat):
        return 0xFFF
    pair = fat[offset] | (fat[offset + 1] << 8)
    return pair & 0x0FFF if cluster % 2 == 0 else pair >> 4


def read_file(node: str, entry: dict, system: bytes,
              limit: int = PREVIEW_BYTES) -> bytes:
    """Pull one file off the disk by following its FAT chain.

    `floppy.read_file` does the same thing but wants the whole 1.44MB image in
    hand. Reading all of it to show a 200-byte text file would take the better
    part of a minute on real hardware and drag the head across every bad
    sector on the way — the same reason `read_medium` stops at the system
    area. The FAT is already in `system`, so the chain can be walked and only
    the clusters that belong to this file ever get read.

    Stops at `limit`, and stops quietly on an unreadable cluster: a damaged
    disk should still show what is left of a file rather than nothing.
    """
    fat_start = 1 * SECTOR                      # straight after the boot sector
    fat = system[fat_start:fat_start + 9 * SECTOR]

    cluster = int(entry.get("cluster") or 0)
    remaining = min(int(entry.get("size") or 0), limit)
    out = bytearray()

    try:
        fd = os.open(node, os.O_RDONLY)
    except OSError:
        return b""
    try:
        guard = 0
        while 2 <= cluster < 0xFF0 and remaining > 0 and guard < 2880:
            guard += 1
            sector = SYSTEM_SECTORS + (cluster - 2)
            try:
                block = os.pread(fd, SECTOR, sector * SECTOR)
            except OSError:
                break                           # bad cluster; keep what we have
            if len(block) < SECTOR:
                break
            take = min(remaining, SECTOR)
            out += block[:take]
            remaining -= take
            cluster = _fat12_next(fat, cluster)
    finally:
        os.close(fd)
    return bytes(out)


def is_textual(blob: bytes) -> bool:
    """Worth printing, as opposed to worth describing.

    A NUL says binary outright; beyond that, mostly-printable is the test. A
    kernel image and a config file both live on this disk and only one of them
    belongs on a terminal.
    """
    if not blob or b"\0" in blob[:512]:
        return False
    printable = sum(1 for b in blob[:512] if 0x20 <= b < 0x7F or b in (9, 10, 13))
    return printable / len(blob[:512]) > 0.85


def _size_via_seek(node: str) -> int:
    """Block devices report st_size 0; seeking to the end gives the capacity."""
    try:
        with open(node, "rb") as fh:
            return fh.seek(0, os.SEEK_END)
    except OSError:
        return 0


# --------------------------------------------------------------------------
# innate judgement
# --------------------------------------------------------------------------

def medium_problems(drive: Drive, medium: Medium | None,
                    surface: Surface | None = None) -> list[tuple[str, str]]:
    """(message, what to do) for everything wrong with this disk.

    Deliberately not `Issue` objects: the fly's lateral horn consumes these,
    and keeping them plain means `fdd.py` does not need to know that.
    """
    problems: list[tuple[str, str]] = []

    if not drive.has_medium:
        return problems                          # an empty drive is not a fault

    if drive.size and not drive.standard_geometry:
        problems.append((
            f"용량이 {drive.size:,} 바이트 — 1.44MB 표준({FLOPPY_SIZE:,})이 아닙니다",
            "720KB 디스켓이거나 드라이브가 지오메트리를 잘못 보고하는 중",
        ))

    if medium is not None:
        if not medium.readable:
            problems.append((
                f"시스템 영역을 읽을 수 없습니다 — {medium.error}",
                "디스켓을 다시 넣거나 헤드를 청소해 보세요",
            ))
        elif not medium.fat12:
            problems.append((
                "FAT12 부트섹터가 아닙니다",
                "포맷되지 않았거나 다른 파일시스템입니다",
            ))

    if surface is not None and surface.bad:
        where = "시스템 영역(FAT·루트 디렉터리)" if surface.system_area_damaged \
            else "데이터 영역"
        cost = "디스켓 전체를 읽을 수 없게 됩니다" if surface.system_area_damaged \
            else "해당 섹터를 쓰는 파일만 깨집니다"
        problems.append((
            f"배드섹터 {len(surface.bad)}개 — {where}",
            f"{cost}. 읽히는 동안 내용을 복사해 두세요",
        ))

    return problems
