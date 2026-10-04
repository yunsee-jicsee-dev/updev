"""A small additive synthesiser, driven from outside the audio thread.

One oscillator per voice, each with its own amplitude and a brightness knob
that mixes in odd harmonics. Two details matter more than the sound design:

* Amplitude ramps across the block instead of stepping at the block boundary.
  A camera updates at 20fps and audio at 86 blocks/sec, so without the ramp
  every frame boundary is a discontinuity, and a discontinuity is a click.
* Phase is carried across blocks. Restarting sin() at zero each block would
  do the same thing for the same reason.

The control thread writes a whole new array to `targets`; the audio thread
reads that attribute once per block. Rebinding a name is atomic under the
GIL, so neither side needs a lock — which is the point, because blocking an
audio callback on a mutex is how you get dropouts.
"""

from __future__ import annotations

import numpy as np

A4_MIDI, A4_HZ = 69, 440.0

SCALES: dict[str, tuple[int, ...]] = {
    "minor-pentatonic": (0, 3, 5, 7, 10),
    "major-pentatonic": (0, 2, 4, 7, 9),
    "whole-tone": (0, 2, 4, 6, 8, 10),
    "chromatic": tuple(range(12)),
}


def scale_freqs(voices: int, scale: str = "minor-pentatonic", root_midi: int = 45) -> np.ndarray:
    """`voices` ascending frequencies stepping through a scale from the root."""
    steps = SCALES.get(scale, SCALES["minor-pentatonic"])
    midi = [root_midi + 12 * (i // len(steps)) + steps[i % len(steps)] for i in range(voices)]
    return A4_HZ * 2.0 ** ((np.array(midi, dtype=np.float64) - A4_MIDI) / 12.0)


class Synth:
    """Additive voices summed to mono. Silent and harmless if audio fails."""

    def __init__(self, voices: int = 8, samplerate: int = 44100, blocksize: int = 512) -> None:
        self.voices = voices
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.master = 0.6
        self.ok = False
        self.error = ""

        self.freqs = scale_freqs(voices)
        self.targets = np.zeros(voices, dtype=np.float64)
        self.brightness = np.zeros(voices, dtype=np.float64)

        self._amp = np.zeros(voices, dtype=np.float64)
        self._phase = np.zeros(voices, dtype=np.float64)
        self._stream = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        try:
            import sounddevice as sd
        except Exception as e:                      # library missing or no PortAudio
            self.error = f"sounddevice unavailable: {e}"
            return False
        try:
            self._stream = sd.OutputStream(
                samplerate=self.samplerate, channels=1, dtype="float32",
                blocksize=self.blocksize, callback=self._callback,
            )
            self._stream.start()
            self.ok = True
        except Exception as e:                      # no output device, busy, etc.
            self.error = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
            self._stream = None
        return self.ok

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self.ok = False

    def set_scale(self, scale: str, root_midi: int = 45) -> None:
        self.freqs = scale_freqs(self.voices, scale, root_midi)

    # -- audio thread ------------------------------------------------------

    def _callback(self, outdata, frames, time_info, status) -> None:
        targets = self.targets              # one atomic read; may change mid-block
        bright = self.brightness
        sr = self.samplerate
        step = 2.0 * np.pi / sr
        t = np.arange(frames, dtype=np.float64)
        mix = np.zeros(frames, dtype=np.float64)

        for i in range(self.voices):
            a0, a1 = self._amp[i], float(targets[i])
            if a0 < 1e-4 and a1 < 1e-4:
                self._amp[i] = a1
                continue
            phase = self._phase[i] + step * self.freqs[i] * t
            wave = np.sin(phase)
            if (b := float(bright[i])) > 0.01:
                wave += b * (0.5 * np.sin(3 * phase) + 0.25 * np.sin(5 * phase))
            mix += np.linspace(a0, a1, frames) * wave
            self._phase[i] = (phase[-1] + step * self.freqs[i]) % (2.0 * np.pi)
            self._amp[i] = a1

        # tanh rather than a hard clip: many voices at once should compress,
        # not buzz.
        outdata[:, 0] = np.tanh(mix * self.master).astype(np.float32)
