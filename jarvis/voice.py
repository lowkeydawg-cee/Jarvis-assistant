"""Streaming speech output with barge-in.

Speaker runs two background threads:
  text queue -> synthesizer thread (Piper) -> audio queue -> playback thread
so sentence two is being synthesized while sentence one is playing.

While audio plays, an optional BargeIn detector listens to the mic with the
Silero voice activity model. If you start talking, playback stops, the rest
of the queued speech is dropped, and the audio it heard is handed to the
microphone so your first words aren't lost.
"""

from __future__ import annotations

import queue
import threading

import numpy as np


class Vad:
    """Speech detector: Silero (bundled with faster-whisper) with an energy fallback.

    Silero needs context: judged 50 ms at a time it missed most quiet or noisy
    speech (9 to 14 percent of frames), so Jarvis sat at [listening] forever.
    Each frame is scored together with the half second before it, which caught
    85 to 91 percent of the same speech and still flagged none of pure noise.
    """

    WINDOW = 512  # samples at 16 kHz, 32 ms
    CONTEXT = 8000  # half a second at 16 kHz

    def __init__(self, threshold: float = 0.5, energy_threshold: float = 0.02):
        self.threshold = threshold
        self.energy_threshold = energy_threshold
        self._recent = np.zeros(0, dtype=np.float32)
        try:
            from faster_whisper.vad import get_vad_model

            self.model = get_vad_model()
        except Exception:
            self.model = None

    def reset(self) -> None:
        self._recent = np.zeros(0, dtype=np.float32)

    def speech_prob(self, frame: np.ndarray) -> float:
        frame = np.asarray(frame, dtype=np.float32)
        if self.model is None:
            rms = float(np.sqrt(np.mean(frame ** 2))) if frame.size else 0.0
            return min(1.0, rms / self.energy_threshold / 2)
        self._recent = np.concatenate([self._recent, frame])[-self.CONTEXT:]
        n = (self._recent.size // self.WINDOW) * self.WINDOW
        if n == 0:
            return 0.0
        probs = self.model(self._recent[-n:])
        # Score the newest audio: the windows covering this frame.
        last = max(1, min(len(probs), -(-frame.size // self.WINDOW)))
        return float(np.max(probs[-last:]))

    def is_speech(self, frame: np.ndarray) -> bool:
        return self.speech_prob(frame) >= self.threshold


class Speaker:
    def __init__(self, voice, barge_in: "BargeIn | None" = None, echo: bool = True):
        self.voice = voice
        self.barge_in = barge_in
        self.echo = echo
        self.text_q: queue.Queue = queue.Queue()
        self.audio_q: queue.Queue = queue.Queue(maxsize=3)
        self.interrupted = threading.Event()
        threading.Thread(target=self._synth_loop, daemon=True, name="tts-synth").start()
        threading.Thread(target=self._play_loop, daemon=True, name="tts-play").start()

    def feed(self, sentence: str) -> None:
        if sentence and not self.interrupted.is_set():
            self.text_q.put(sentence)

    def wait(self) -> bool:
        """Block until everything fed so far has been spoken. True if interrupted."""
        self.text_q.join()
        self.audio_q.join()
        was = self.interrupted.is_set()
        self.interrupted.clear()
        return was

    def say(self, text: str) -> bool:
        self.feed(text)
        return self.wait()

    def interrupt(self) -> None:
        self.interrupted.set()
        self.voice.stop()

    def _synth_loop(self) -> None:
        while True:
            text = self.text_q.get()
            try:
                if not self.interrupted.is_set():
                    if self.echo:
                        print(f"Jarvis: {text}")
                    audio = self.voice.synthesize(text)
                    if audio is not None and audio.size:
                        self.audio_q.put(audio)
            except Exception as exc:
                print(f"  [tts error] {exc}")
            finally:
                self.text_q.task_done()

    def _play_loop(self) -> None:
        while True:
            audio = self.audio_q.get()
            try:
                if not self.interrupted.is_set():
                    if self.barge_in:
                        self.barge_in.arm(self.interrupt)
                    self.voice.play(audio)
            finally:
                if self.barge_in:
                    self.barge_in.disarm()
                self.audio_q.task_done()


class BargeIn:
    """Watches the mic while Jarvis talks and calls back when the user cuts in."""

    def __init__(self, mic, vad: Vad, min_speech_ms: int = 250):
        self.mic = mic
        self.vad = vad
        self.frames_needed = max(1, int(min_speech_ms / 50))
        self.callback = None
        self.heard: list[np.ndarray] = []
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True, name="barge-in").start()

    def arm(self, callback) -> None:
        with self.lock:
            self.callback = callback

    def disarm(self) -> None:
        with self.lock:
            self.callback = None

    def take_audio(self) -> np.ndarray | None:
        """Audio captured during the interruption, to prepend to the next recording."""
        with self.lock:
            audio, self.heard = self.heard, []
        return np.concatenate(audio) if audio else None

    def _loop(self) -> None:
        run = 0
        recent: list[np.ndarray] = []
        for frame in self.mic.frames():
            with self.lock:
                cb = self.callback
            if cb is None:
                run, recent = 0, []
                continue
            recent = (recent + [frame])[-20:]
            run = run + 1 if self.vad.is_speech(frame) else 0
            if run >= self.frames_needed:
                with self.lock:
                    self.heard = list(recent)
                    self.callback = None
                run, recent = 0, []
                cb()
