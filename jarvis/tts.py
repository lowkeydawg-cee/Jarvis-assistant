"""Text to speech backends. Each has synthesize(text) -> audio, play(audio) and stop()."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np


class PiperVoice:
    def __init__(self, cfg: dict):
        from piper import PiperVoice as _Piper
        import sounddevice as sd

        path = Path(cfg["voice"])
        if not path.is_file():
            raise FileNotFoundError(
                f"Piper voice not found at {path}. Run 'python scripts/download_voice.py' first."
            )
        self.sd = sd
        self.voice = _Piper.load(str(path))
        self.rate = self.voice.config.sample_rate
        self._stopped = False

    def synthesize(self, text: str) -> np.ndarray:
        # piper-tts >= 1.3 yields AudioChunk objects; older releases yield raw int16 bytes.
        if not hasattr(self.voice, "synthesize_stream_raw"):
            parts = [chunk.audio_int16_array for chunk in self.voice.synthesize(text)]
            return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
        raw = b"".join(self.voice.synthesize_stream_raw(text))
        return np.frombuffer(raw, dtype=np.int16)

    def play(self, audio: np.ndarray) -> None:
        """Play and block until done or stop() is called."""
        self._stopped = False
        self.sd.play(audio, self.rate)
        end = time.monotonic() + len(audio) / self.rate + 0.2
        while time.monotonic() < end and not self._stopped:
            time.sleep(0.02)
        self.sd.stop()

    def stop(self) -> None:
        self._stopped = True


class PrintVoice:
    """Text mode: the Speaker prints each sentence, nothing is played."""

    def synthesize(self, text: str):
        return None

    def play(self, audio) -> None:
        pass

    def stop(self) -> None:
        pass
