"""Speech to text with faster-whisper, fully offline after the first model download."""

from __future__ import annotations

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
