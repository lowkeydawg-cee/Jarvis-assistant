"""Microphone capture with end-of-speech detection.

One input stream is shared by everything that listens (recording, wake word,
barge-in), so Windows and macOS never have to open the device twice.
"""

from __future__ import annotations

import queue
import threading

import numpy as np


class Microphone:
    FRAME_SECONDS = 0.05

    def __init__(self, cfg: dict, vad=None):
        import sounddevice as sd  # imported lazily so text mode needs no audio stack

        self.rate = int(cfg["sample_rate"])
        self.threshold = float(cfg["silence_threshold"])
        self.silence_seconds = float(cfg["silence_seconds"])
        self.max_seconds = float(cfg["max_record_seconds"])
        self.vad = vad
        self.block = int(self.rate * self.FRAME_SECONDS)
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.stream = sd.InputStream(
            samplerate=self.rate, channels=1, dtype="float32", blocksize=self.block,
            callback=self._on_audio, device=cfg.get("device"),
        )
        self.stream.start()

    def _on_audio(self, data, *_):
        frame = data[:, 0].copy()
        with self._lock:
            for q in self._subs:
                q.put(frame)

    def _subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def _unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.remove(q)

    def frames(self, stop: threading.Event | None = None):
        """Yield float32 mono frames until `stop` is set."""
        q = self._subscribe()
        try:
            while stop is None or not stop.is_set():
                try:
                    yield q.get(timeout=0.2)
                except queue.Empty:
                    continue
        finally:
            self._unsubscribe(q)

    def _loud(self, frame: np.ndarray) -> bool:
        if self.vad is not None:
            return self.vad.is_speech(frame)
        return float(np.sqrt(np.mean(frame ** 2))) > self.threshold

    def record_utterance(self, prefix: np.ndarray | None = None, stop: threading.Event | None = None) -> np.ndarray:
        """Record until the speaker goes quiet. Returns float32 audio at self.rate.

        `prefix` is audio already heard (from a barge-in), so recording starts
        as if speech is in progress. Setting `stop` ends recording early.
        """
        if self.vad is not None and hasattr(self.vad, "reset"):
            self.vad.reset()  # don't let the end of Jarvis's own reply count as you talking
        chunks: list[np.ndarray] = [prefix] if prefix is not None and prefix.size else []
        started = bool(chunks)
        quiet, waited = 0, 0
        quiet_limit = int(self.silence_seconds / self.FRAME_SECONDS)
        max_frames = int(self.max_seconds / self.FRAME_SECONDS)
        wait_limit = int(5 / self.FRAME_SECONDS)  # give up if nobody speaks for 5 seconds

        for frame in self.frames(stop):
            loud = self._loud(frame)
            if not started:
                waited += 1
                if loud:
                    started = True
                    chunks.append(frame)
                elif waited >= wait_limit:
                    break
                continue
            chunks.append(frame)
            quiet = 0 if loud else quiet + 1
            if quiet >= quiet_limit or len(chunks) >= max_frames:
                break
        if stop is not None and stop.is_set() and not started:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
