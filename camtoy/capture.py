"""Frames out of a V4L2 camera, via an ffmpeg pipe.

PyAV can open /dev/video0 directly, but on the cheap UVC sensors this project
targets it raises `avcodec_send_packet(): Invalid argument` after a few
hundred frames and never recovers. ffmpeg swallows the same corrupt buffers
with a warning and keeps streaming, so we shell out to it and read raw rgb24
off stdout. One dependency less, too — numpy is the only import that matters.

The reader runs on its own thread and keeps *only the newest frame*. Every
mode here is interactive, so a consumer that falls behind should skip ahead
to the present rather than replay a backlog.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass

import numpy as np

DEFAULT_DEVICE = "/dev/video0"

# Fallback when v4l2-ctl is missing or unparseable. Chosen for frame rate over
# pixels: at 160x120 the AX2311 gives ~7fps, at 320x240 it gives 4fps, and
# every mode here feels better fast than sharp.
FALLBACK_MODE = (160, 120, 15.0)


@dataclass(frozen=True)
class CameraMode:
    width: int
    height: int
    fps: float
    pixfmt: str = "yuyv422"

    @property
    def size(self) -> str:
        return f"{self.width}x{self.height}"

    def __str__(self) -> str:
        return f"{self.size} @ {self.fps:g}fps {self.pixfmt}"


class CaptureError(RuntimeError):
    """ffmpeg refused to start, or died while streaming."""


# --------------------------------------------------------------------------
# mode discovery
# --------------------------------------------------------------------------

_FMT_RE = re.compile(r"\[\d+\]:\s*'(\w+)'")
_SIZE_RE = re.compile(r"Size: Discrete (\d+)x(\d+)")
_INTERVAL_RE = re.compile(r"Interval: Discrete [\d.]+s \(([\d.]+) fps\)")

# v4l2 reports a FOURCC; ffmpeg's -input_format wants its own name for the
# same thing. Anything unmapped is passed through lowercased and will simply
# fail to open, which is the honest outcome for a format we cannot decode.
_FOURCC_TO_FFMPEG = {
    "YUYV": "yuyv422",
    "UYVY": "uyvy422",
    "YVYU": "yvyu422",
    "MJPG": "mjpeg",
    "JPEG": "mjpeg",
    "YU12": "yuv420p",
    "YV12": "yuv420p",
    "NV12": "nv12",
    "GREY": "gray",
    "RGB3": "rgb24",
    "BGR3": "bgr24",
    "H264": "h264",
}


def probe_modes(device: str = DEFAULT_DEVICE) -> list[CameraMode]:
    """Ask v4l2-ctl what this camera can actually do.

    Returns [] rather than raising: a missing v4l2-ctl is not a reason to
    refuse to open the camera, it just means we use FALLBACK_MODE.
    """
    if not shutil.which("v4l2-ctl"):
        return []
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-formats-ext"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    modes: list[CameraMode] = []
    pixfmt, size = "yuyv422", None
    for line in out.splitlines():
        if m := _FMT_RE.search(line):
            fourcc = m.group(1).upper()
            pixfmt = _FOURCC_TO_FFMPEG.get(fourcc, fourcc.lower())
        elif m := _SIZE_RE.search(line):
            size = (int(m.group(1)), int(m.group(2)))
        elif (m := _INTERVAL_RE.search(line)) and size:
            modes.append(CameraMode(size[0], size[1], float(m.group(1)), pixfmt))
    return modes


def pick_mode(modes: list[CameraMode], min_width: int = 128) -> CameraMode:
    """Best mode for interactive use: fastest first, then widest.

    `min_width` keeps us from picking a 64x48 mode just because it claims
    30fps — below that the terminal renderer has nothing to work with.
    """
    usable = [m for m in modes if m.width >= min_width] or modes
    if not usable:
        return CameraMode(*FALLBACK_MODE)
    return max(usable, key=lambda m: (m.fps, m.width))


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------

class Camera:
    """Streams uint8 (H, W, 3) RGB frames from a V4L2 device.

    Use as a context manager. `read()` blocks until a frame is available and
    then returns the newest one; `frames()` iterates newest-frame-only until
    the camera stops.
    """

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        mode: CameraMode | None = None,
        mirror: bool = True,
    ) -> None:
        self.device = device
        self.mode = mode or pick_mode(probe_modes(device))
        self.mirror = mirror

        self._proc: subprocess.Popen | None = None
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._cond = threading.Condition()
        self._stopping = False
        self._error: str | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._threads: list[threading.Thread] = []

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> "Camera":
        if not shutil.which("ffmpeg"):
            raise CaptureError("ffmpeg not found — install it with: sudo apt install ffmpeg")

        m = self.mode
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2",
            "-input_format", m.pixfmt,
            "-video_size", m.size,
            "-framerate", f"{m.fps:g}",
            "-i", self.device,
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            )
        except OSError as e:
            raise CaptureError(f"could not start ffmpeg: {e}") from e

        self._spawn(self._read_loop, "camtoy-capture")
        self._spawn(self._stderr_loop, "camtoy-stderr")
        return self

    def _spawn(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def close(self) -> None:
        self._stopping = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2)
        # Wake anyone parked in read() so they can see the camera is gone.
        with self._cond:
            self._cond.notify_all()
        for t in self._threads:
            t.join(timeout=1)

    def __enter__(self) -> "Camera":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- threads -----------------------------------------------------------

    def _read_loop(self) -> None:
        m = self.mode
        nbytes = m.width * m.height * 3
        stdout = self._proc.stdout
        try:
            while not self._stopping:
                buf = self._read_exactly(stdout, nbytes)
                if buf is None:
                    break
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(m.height, m.width, 3)
                if self.mirror:
                    frame = frame[:, ::-1]
                with self._cond:
                    self._frame = frame
                    self._seq += 1
                    self._cond.notify_all()
        except (OSError, ValueError) as e:
            self._error = str(e)
        finally:
            self._stopping = True
            with self._cond:
                self._cond.notify_all()

    @staticmethod
    def _read_exactly(stream, n: int) -> bytes | None:
        """Read n bytes or return None. A short read means ffmpeg exited."""
        chunks, remaining = [], n
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _stderr_loop(self) -> None:
        try:
            for raw in self._proc.stderr:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr.append(line)
        except (OSError, ValueError):
            pass

    # -- consumption -------------------------------------------------------

    @property
    def alive(self) -> bool:
        return not self._stopping

    def diagnosis(self) -> str:
        """Why the stream stopped, in terms a user can act on."""
        # ffmpeg prints its reason on the way out, but the reader thread
        # notices the closed pipe before the drain thread has parsed those
        # last lines. Without this wait the real error loses a race with the
        # generic fallback below, and the user is told "camera stream ended"
        # when ffmpeg actually said something useful.
        self._settle_stderr(0.5)

        if self._error:
            return self._error
        tail = [ln for ln in self._stderr if "corrupted data" not in ln]
        if tail:
            return tail[-1]
        if self._proc and self._proc.returncode not in (0, None, -15):
            return f"ffmpeg exited with code {self._proc.returncode}"
        if self._seq == 0:
            # Enumerating fine and streaming fine are different things, and
            # cheap UVC modules routinely do the first without the second
            # after a bad unplug. Say so, because "stream ended" sends people
            # looking for a software fault that is not there.
            waited = time.monotonic() - self._opened_at
            return (f"the camera opened but sent no frames in {waited:.0f}s. "
                    "It is enumerated but not streaming — unplug it and plug it "
                    "back in. A driver rebind or USB re-authorize usually will "
                    "not clear this.")
        return "camera stream ended"

    def _settle_stderr(self, timeout: float) -> None:
        for thread in self._threads:
            if thread.name == "camtoy-stderr":
                thread.join(timeout=timeout)

    def read(self, timeout: float = 5.0) -> np.ndarray | None:
        """Newest frame, or None if the camera stopped or went quiet."""
        with self._cond:
            seen = self._seq
            if self._frame is not None:
                return self._frame
            self._cond.wait_for(lambda: self._seq != seen or self._stopping, timeout)
            return self._frame

    def frames(self, timeout: float = 5.0):
        """Yield each new frame, skipping any the consumer was too slow for."""
        # Seed from the live counter, not a sentinel: starting at -1 would make
        # the first wait_for pass instantly on the seq==0 no-frame-yet state and
        # hand the caller a None.
        with self._cond:
            last = self._seq
        while True:
            with self._cond:
                if not self._cond.wait_for(
                    lambda: self._seq != last or self._stopping, timeout
                ):
                    return                      # camera went silent
                if self._stopping and self._seq == last:
                    return
                last, frame = self._seq, self._frame
            if frame is None:
                return
            yield frame
