"""Speech to text: local faster-whisper, or Groq's hosted Whisper with a local fallback."""

from __future__ import annotations

import io
import os
import wave

import numpy as np


class Transcriber:
    def __init__(self, cfg: dict):
        from faster_whisper import WhisperModel

        self.language = cfg.get("language") or None
        self.model = WhisperModel(
            cfg["model"], device=cfg.get("device", "auto"), compute_type=cfg.get("compute_type", "int8")
        )

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        segments, _ = self.model.transcribe(
            audio, language=self.language, beam_size=1, vad_filter=True
        )
        return " ".join(seg.text.strip() for seg in segments).strip()


def to_wav(audio: np.ndarray, rate: int = 16000) -> bytes:
    """float32 mono in [-1, 1] -> 16-bit WAV bytes."""
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class CloudTranscriber:
    """Whisper large v3 turbo on Groq: about a second for a sentence, even on a slow laptop.

    Only the recorded clip is sent. If the request fails, the local model is
    loaded (once) and used instead, so Jarvis keeps working offline.
    """

    def __init__(self, cfg: dict, key: str, post=None):
        import requests

        c = cfg.get("cloud") or {}
        self.url = c.get("base_url", "https://api.groq.com/openai/v1").rstrip("/") + "/audio/transcriptions"
        self.model = c.get("model", "whisper-large-v3-turbo")
        self.timeout = int(c.get("timeout", 20))
        self.language = cfg.get("language") or None
        self.key = key
        self.cfg = cfg
        self.post = post or requests.post
        self._local: Transcriber | None = None

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        try:
            data = {"model": self.model, "response_format": "json"}
            if self.language:
                data["language"] = self.language
            resp = self.post(
                self.url, headers={"Authorization": f"Bearer {self.key}"}, data=data,
                files={"file": ("speech.wav", to_wav(audio), "audio/wav")}, timeout=self.timeout,
            )
            resp.raise_for_status()
            return str(resp.json().get("text", "")).strip()
        except Exception as exc:
            print(f"  [stt] cloud transcription failed, using the local model: {exc}")
            if self._local is None:
                self._local = Transcriber(self.cfg)
            return self._local.transcribe(audio)


def build_transcriber(cfg: dict):
    """stt.provider: local (default) or cloud. Cloud without a key quietly stays local."""
    if str(cfg.get("provider", "local")).lower() == "cloud":
        key = os.environ.get((cfg.get("cloud") or {}).get("api_key_env", "GROQ_API_KEY"), "")
        if key:
            return CloudTranscriber(cfg, key)
        print("  [stt] no API key set, so speech is transcribed locally")
    return Transcriber(cfg)
