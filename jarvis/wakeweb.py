"""'Hey Jarvis' for the browser app: the laptop listens for the wake word, offline.

Browsers can't spot a wake word on their own, so the laptop runs openWakeWord
(a small model, about 1 MB, entirely offline) on its microphone. When it hears
"Hey Jarvis" it tells the open Jarvis page, which plays a chime and starts
listening as if you had pressed Space. Nothing is recorded or sent anywhere
until then.

It is off until you switch it on in Settings, because it keeps the microphone
open. The choice is remembered.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

CHUNK = 1280  # 80 ms at 16 kHz, what openWakeWord expects


class WakeListener:
    def __init__(self, state_path: str | Path, on_wake: Callable[[], None], threshold: float = 0.5,
                 model_name: str = "hey_jarvis", cooldown: float = 2.0):
        self.state_path = Path(state_path).expanduser()
        self.on_wake = on_wake
        self.threshold = threshold
        self.model_name = model_name
        self.cooldown = cooldown
        self.enabled = self._load().get("enabled", False)
        self.error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last = 0.0

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"enabled": on}), encoding="utf-8")
        self.stop()
        if on:
            self.start()

    def heard(self, score: float, now: float | None = None) -> bool:
        """Decide whether a score is a wake-up (above the bar, and not right after the last one)."""
        now = time.monotonic() if now is None else now
        if score >= self.threshold and now - self._last >= self.cooldown:
            self._last = now
            return True
        return False

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="wake-word")
        self._thread.start()

    def stop(self, wait: float = 15) -> None:
        """Stop listening. Waits for the old listener (it can be mid-way through loading the model),
        so switching off and quickly on again can't leave it off."""
        self._stop.set()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(wait)

    def _run(self) -> None:
        try:
            import openwakeword
            import sounddevice as sd
            from openwakeword.model import Model

            openwakeword.utils.download_models(model_names=[self.model_name])  # once; a few MB
            model = Model(wakeword_models=[self.model_name], inference_framework="onnx")
            self.error = ""
            with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=CHUNK) as stream:
                print("  [wake] listening for 'Hey Jarvis'")
                while not self._stop.is_set():
                    audio, _ = stream.read(CHUNK)
                    score = max(model.predict(np.asarray(audio[:, 0], dtype=np.int16)).values(), default=0)
                    if self.heard(float(score)):
                        model.reset()
                        self.on_wake()
        except Exception as exc:
            self.error = str(exc)[:200]
            print(f"  [wake] stopped: {exc}")

    def status(self) -> dict:
        return {"available": True, "enabled": self.enabled,
                "running": bool(self._thread and self._thread.is_alive()), "error": self.error}
