"""How fast is it *really* — a read-only benchmark for block devices.

`updev usb path` reports the link speed the device negotiated, which is a
ceiling and not a promise: a SuperSpeed stick that negotiated 5 Gbps will
still hand you 30 MB/s if the flash behind the bridge is cheap. This measures
what the medium actually gives, and says which of the two is the limit.

The second half is a seek probe, and it exists because it argues with the
classifier on the classifier's own terms. `usbclass` decides HUSB vs SUSB from
VPD page 0xB1 — the drive's own claim about its rotation rate. A median random
read latency of 12 ms is a platter moving a head; 0.2 ms is not. When the two
disagree, the enclosure was lying and now you can prove it.

Reads only. Nothing here opens a device for writing, and the buffers are
thrown away.
"""

from __future__ import annotations

import mmap
import os
import statistics
import time
from dataclasses import dataclass, field

__all__ = [
    "BenchResult",
    "link_comparison",
    "rotation_hint",
    "run",
]

_MIB = 1024 * 1024


@dataclass(slots=True)
class BenchResult:
    node: str
    size_bytes: int = 0
    chunk_bytes: int = 0
    reads: int = 0
    seconds: float = 0.0
    throughput_mbs: float = 0.0
    samples_mbs: list[float] = field(default_factory=list)
    seek_ms: list[float] = field(default_factory=list)
    direct: bool = False
    note: str = ""

    @property
    def seek_median(self) -> float:
        return statistics.median(self.seek_ms) if self.seek_ms else 0.0

    @property
    def steady_mbs(self) -> float:
        """Median of the per-chunk rates — a cache hit at the start of the
        device shouldn't get to set the headline number."""
        return statistics.median(self.samples_mbs) if self.samples_mbs else 0.0

    def as_dict(self) -> dict:
        verdict, reason = rotation_hint(self.seek_median)
        return {
            "node": self.node,
            "size_bytes": self.size_bytes,
            "chunk_bytes": self.chunk_bytes,
            "reads": self.reads,
            "seconds": round(self.seconds, 4),
            "throughput_mbs": round(self.throughput_mbs, 2),
            "steady_mbs": round(self.steady_mbs, 2),
            "samples_mbs": [round(v, 2) for v in self.samples_mbs],
            "seek_ms_median": round(self.seek_median, 3),
            "seek_ms": [round(v, 3) for v in self.seek_ms],
            "seek_verdict": verdict,
            "seek_reason": reason,
            "direct_io": self.direct,
            "note": self.note,
        }


def rotation_hint(median_ms: float) -> tuple[str, str]:
    """Rotating or not, from random-read latency alone.

    A 5400 rpm platter averages ~5.5 ms of rotational latency before the seek
    even counts, so anything at or above ~4 ms has moving parts. Flash lands
    one to two orders of magnitude below that, and the gap in between is where
    a busy bus or a sleeping drive muddies the answer.
    """
    if median_ms <= 0:
        return "unknown", "no seek samples were taken"
    if median_ms < 0.01:
        return "cached", (
            f"median random read {median_ms:.3f} ms — that is memory speed, not "
            f"a device: the reads were answered from cache. Nothing can be said "
            f"about the medium from this"
        )
    if median_ms >= 4.0:
        return "rotating", (
            f"median random read {median_ms:.1f} ms — rotational latency alone "
            f"puts a platter above 4 ms, so this has moving parts"
        )
    if median_ms <= 1.0:
        return "solid-state", (
            f"median random read {median_ms:.2f} ms — no mechanism can move a "
            f"head that fast, so this is flash"
        )
    return "unclear", (
        f"median random read {median_ms:.2f} ms — between the two, usually a "
        f"drive waking up or a bus under load. Re-run it."
    )


def link_comparison(mbs: float, link_mbps: float) -> str:
    """Whether the bus or the medium is the limit, in one sentence."""
    if link_mbps <= 0 or mbs <= 0:
        return ""
    ceiling = link_mbps / 8.0 * 0.8      # 8b/10b-ish framing overhead, roughly
    share = mbs / ceiling * 100
    if share >= 85:
        return (f"{share:.0f}% of what the {link_mbps:.0f} Mbps link can carry — "
                f"the bus is the limit; a faster port would help")
    if share >= 40:
        return (f"{share:.0f}% of the {link_mbps:.0f} Mbps link — medium and bus "
                f"are in the same league")
    return (f"{share:.0f}% of the {link_mbps:.0f} Mbps link — the medium is the "
            f"limit, not the bus; a faster port would change nothing")


def run(
    path: str,
    chunk_mb: int = 4,
    reads: int = 8,
    seeks: int = 48,
    direct: bool = True,
) -> BenchResult:
    """Sequential throughput, then random-read latency. Read-only throughout.

    O_DIRECT is tried first so the page cache can't answer for the device; when
    the kernel refuses it (some bridges and every loop device), we fall back to
    buffered reads and drop the cache with `posix_fadvise` before each one,
    which gets close enough.
    """
    chunk = max(1, chunk_mb) * _MIB
    result = BenchResult(node=path, chunk_bytes=chunk)

    flags = os.O_RDONLY
    use_direct = direct and hasattr(os, "O_DIRECT")
    if use_direct:
        flags |= os.O_DIRECT
    try:
        fd = os.open(path, flags)
        result.direct = use_direct
    except OSError as e:
        if not use_direct:
            raise
        fd = os.open(path, os.O_RDONLY)          # O_DIRECT unsupported here
        result.note = f"O_DIRECT refused ({e.strerror}); using cache-dropped reads"

    try:
        result.size_bytes = _size(fd, path)
        if result.size_bytes < chunk * 2:
            chunk = max(64 * 1024, result.size_bytes // 4)
            result.chunk_bytes = chunk
        buf = mmap.mmap(-1, chunk)               # page-aligned, as O_DIRECT wants
        view = memoryview(buf)

        t0 = time.perf_counter()
        offset = 0
        for _ in range(max(1, reads)):
            if offset + chunk > result.size_bytes:
                break
            took, got = _timed_read(fd, view, offset, result.direct)
            if got <= 0:
                break
            result.reads += 1
            result.seconds += took
            result.samples_mbs.append(got / _MIB / took if took else 0.0)
            offset += chunk
        elapsed = time.perf_counter() - t0
        moved = result.reads * chunk
        result.throughput_mbs = (moved / _MIB / result.seconds) if result.seconds else 0.0
        if not result.note and elapsed and moved:
            result.note = f"{moved / _MIB:.0f} MiB in {elapsed:.2f}s"

        result.seek_ms = _seek_probe(fd, result.size_bytes, seeks, result.direct)
        view.release()
        buf.close()
    finally:
        os.close(fd)
    return result


def _timed_read(fd: int, view: memoryview, offset: int, direct: bool) -> tuple[float, int]:
    if not direct:
        _drop_cache(fd, offset, len(view))
    t0 = time.perf_counter()
    got = os.preadv(fd, [view], offset)
    return time.perf_counter() - t0, got


def _seek_probe(fd: int, size: int, samples: int, direct: bool) -> list[float]:
    """Small reads at pseudo-random offsets. The spread of the *latency* is the
    signal, so the block size stays at one sector to keep transfer time out of
    the measurement."""
    if size <= 0 or samples <= 0:
        return []
    block = 4096
    buf = mmap.mmap(-1, block)
    view = memoryview(buf)
    span = max(1, (size - block) // block)
    out: list[float] = []
    # A fixed stride that is coprime-ish with the span walks the whole device
    # without needing a PRNG, and repeats exactly on a re-run.
    stride = max(1, span // (samples + 1)) * 7 + 1
    position = span // 3
    try:
        for _ in range(samples):
            position = (position + stride) % span
            offset = position * block
            if not direct:
                _drop_cache(fd, offset, block)
            t0 = time.perf_counter()
            try:
                if os.preadv(fd, [view], offset) <= 0:
                    break
            except OSError:
                break
            out.append((time.perf_counter() - t0) * 1000)
    finally:
        view.release()
        buf.close()
    return out


def _drop_cache(fd: int, offset: int, length: int) -> None:
    """Ask the kernel to forget these pages so the next read reaches the device."""
    try:
        os.posix_fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass


def _size(fd: int, path: str) -> int:
    """Block devices report 0 from stat(); lseek to the end instead."""
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        os.lseek(fd, 0, os.SEEK_SET)
        if size > 0:
            return size
    except OSError:
        pass
    try:
        return os.stat(path).st_size
    except OSError:
        return 0
