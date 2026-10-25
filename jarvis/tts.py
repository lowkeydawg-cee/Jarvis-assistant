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


class KokoroVoice(PiperVoice):
    """Kokoro: much more natural than Piper. Needs a decent CPU to keep up in real time.

    On a slow laptop it takes about 1.5x the speech length to synthesize, so there are
    short pauses between sentences; Piper or the browser app's voices are faster there.
    """

    def __init__(self, cfg: dict):
        from kokoro_onnx import Kokoro
        import sounddevice as sd

        model, voices = Path(cfg["kokoro_model"]), Path(cfg["kokoro_voices"])
        if not model.is_file() or not voices.is_file():
            raise FileNotFoundError(
                f"Kokoro files not found ({model}, {voices}). Run 'python scripts/download_kokoro.py' first."
            )
        self.sd = sd
        self.kokoro = Kokoro(str(model), str(voices))
        self.voice_name = cfg.get("kokoro_voice", "bm_george")
        self.speed = float(cfg.get("speed", 1.1))
        self.lang = cfg.get("kokoro_lang", "en-gb")
        self.rate = 24000
        self._stopped = False

    def synthesize(self, text: str) -> np.ndarray:
        samples, self.rate = self.kokoro.create(text, voice=self.voice_name, speed=self.speed, lang=self.lang)
        return (np.clip(samples, -1, 1) * 32767).astype(np.int16)


def build_voice(cfg: dict):
    """tts.engine: piper (default, fast) or kokoro (natural, heavier)."""
    if str(cfg.get("engine", "piper")).lower() == "kokoro":
        return KokoroVoice(cfg)
    return PiperVoice(cfg)


class PrintVoice:
    """Text mode: the Speaker prints each sentence, nothing is played."""

    def synthesize(self, text: str):
        return None

    def play(self, audio) -> None:
        pass

    def stop(self) -> None:
        pass
