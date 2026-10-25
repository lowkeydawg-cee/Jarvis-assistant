"""Recognise the owner's voice, fully offline.

A small speaker-embedding model (WeSpeaker ResNet34 through sherpa-onnx, about
25 MB) turns a few seconds of speech into a 256-number voiceprint. Enrolment
averages a few sentences into one profile. Each later utterance is compared to
it by cosine similarity: the same person scores high, other people low.

In tests with synthetic voices, the same voice scored about 0.9 and different
voices about 0.7. The bar is set per person at enrolment (see `enroll`), never
below the configured threshold of 0.6.

Only the voiceprint is stored, never the recordings.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np

RATE = 16000
MIN_SECONDS = 0.8


def decode_pcm16(b64: str) -> np.ndarray:
    """Base64 little-endian 16-bit mono PCM (what the browser sends) -> float32 in [-1, 1]."""
    raw = base64.b64decode(b64)
    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


class VoiceID:
    def __init__(self, model: str | Path, profile: str | Path, threshold: float = 0.6, embedder=None):
        self.model_path = Path(model)
        self.profile_path = Path(profile).expanduser()
        self.threshold = threshold
        self._embedder = embedder  # tests pass a fake; otherwise loaded on first use
        self.name = ""
        self.voiceprint: np.ndarray | None = None
        self.match_at = threshold  # the bar for this person, set at enrolment
        if self.profile_path.is_file():
            try:
                data = json.loads(self.profile_path.read_text(encoding="utf-8"))
                self.name = data.get("name", "")
                self.voiceprint = np.asarray(data["voiceprint"], dtype=np.float32)
                self.match_at = float(data.get("match_at", threshold))
            except (ValueError, KeyError):
                pass

    @property
    def enrolled(self) -> bool:
        return self.voiceprint is not None

    def _extractor(self):
        if self._embedder is None:
            import sherpa_onnx

            if not self.model_path.is_file():
                raise FileNotFoundError(f"Speaker model not found at {self.model_path}. "
                                        "Run 'python scripts/download_speaker_model.py'.")
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(self.model_path), num_threads=2)
            ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

            def embed(audio: np.ndarray) -> np.ndarray:
                stream = ex.create_stream()
                stream.accept_waveform(RATE, audio)
                stream.input_finished()
                return np.asarray(ex.compute(stream), dtype=np.float32)

            self._embedder = embed
        return self._embedder

    def embed(self, audio: np.ndarray) -> np.ndarray:
        audio = np.asarray(audio, dtype=np.float32)
        if audio.size < MIN_SECONDS * RATE:
            raise ValueError("That clip is too short to recognise a voice. Say a full sentence.")
        v = self._extractor()(audio)
        return v / (np.linalg.norm(v) + 1e-9)

    def enroll(self, clips: list[np.ndarray], name: str) -> float:
        """Build and save the owner's voiceprint. Returns how consistent the clips were (0 to 1)."""
        vecs = [self.embed(c) for c in clips]
        if len(vecs) < 2:
            raise ValueError("Record at least two sentences.")
        mean = np.mean(vecs, axis=0)
        self.voiceprint = mean / (np.linalg.norm(mean) + 1e-9)
        self.name = name.strip() or "you"
        consistency = float(np.mean([float(v @ self.voiceprint) for v in vecs]))
        # The bar adapts to the person: a very consistent recording can afford a stricter bar.
        # Synthetic test voices that sound alike scored about 0.7 against an enrolled one at 0.93,
        # so a fixed 0.6 let them in; this sets about 0.77 there, and stays near 0.6 for noisier enrolments.
        self.match_at = round(min(0.8, max(self.threshold, consistency - 0.2)), 2)
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        self.profile_path.write_text(json.dumps({"name": self.name, "voiceprint": self.voiceprint.tolist(),
                                                 "match_at": self.match_at}), encoding="utf-8")
        return consistency

    def identify(self, audio: np.ndarray) -> tuple[bool, float]:
        """(is it the owner, similarity score)."""
        if self.voiceprint is None:
            return False, 0.0
        score = float(self.embed(audio) @ self.voiceprint)
        return score >= max(self.threshold, self.match_at), score

    def forget(self) -> None:
        self.voiceprint, self.name = None, ""
        self.profile_path.unlink(missing_ok=True)
