"""Fetching weights, resumably.

850MB over a home connection is long enough that an interrupted download has
to be survivable. Every file lands in a `.part` beside its destination and is
only renamed once the byte count matches what the registry recorded, so a
half-written file can never masquerade as a working model — and a retry picks
up with a Range request instead of starting over.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from .registry import LABEL_URLS, MODEL_DIR, ModelSpec

CHUNK = 1 << 18
USER_AGENT = "camtoy/1.0 (+https://github.com/yunsee-jicsee-dev/updev)"

Progress = Callable[[str, int, int], None]      # key, done_bytes, total_bytes


class DownloadError(RuntimeError):
    pass


def _open(url: str, offset: int = 0):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if offset:
        request.add_header("Range", f"bytes={offset}-")
    return urllib.request.urlopen(request, timeout=60)


def fetch(spec: ModelSpec, progress: Progress | None = None, retries: int = 3) -> Path:
    """Download one model unless it is already there and the right size."""
    if spec.present:
        if progress:
            progress(spec.key, spec.size, spec.size)
        return spec.path

    dest = spec.path
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".onnx.part")

    for attempt in range(retries):
        done = part.stat().st_size if part.exists() else 0
        if done > spec.size:                    # stale junk from a changed URL
            part.unlink()
            done = 0
        try:
            with _open(spec.url, done) as response:
                # A server that ignores Range restarts the body at zero; if we
                # appended we would end up with a corrupt, plausibly-sized file.
                if done and response.status != 206:
                    done = 0
                mode = "ab" if done else "wb"
                with open(part, mode) as fh:
                    while chunk := response.read(CHUNK):
                        fh.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(spec.key, done, spec.size)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if attempt == retries - 1:
                raise DownloadError(f"{spec.key}: {e}") from None
            continue

        actual = part.stat().st_size
        if actual == spec.size:
            part.replace(dest)
            return dest
        if attempt == retries - 1:
            raise DownloadError(
                f"{spec.key}: got {actual} bytes, registry says {spec.size}. "
                "The upstream file may have changed — re-check the registry."
            )

    raise DownloadError(f"{spec.key}: exhausted {retries} attempts")


def fetch_all(specs: Iterable[ModelSpec], progress: Progress | None = None) -> list[Path]:
    return [fetch(spec, progress) for spec in specs]


def fetch_labels(name: str = "imagenet") -> Path:
    """Class-name lists live next to the weights and are tiny."""
    dest = MODEL_DIR / "labels" / f"{name}.txt"
    if dest.is_file() and dest.stat().st_size > 1024:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _open(LABEL_URLS[name]) as response:
        dest.write_bytes(response.read())
    return dest
