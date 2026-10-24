"""Optional "Hey Jarvis" wake word using openWakeWord (runs on CPU, offline)."""

from __future__ import annotations

import numpy as np


class WakeWord:
    def __init__(self, cfg: dict):
        import openwakeword
        from openwakeword.model import Model

        openwakeword.utils.download_models()
        self.name = cfg.get("model", "hey_jarvis")
        self.threshold = float(cfg.get("threshold", 0.5))
        self.model = Model(wakeword_models=[self.name], inference_framework="onnx")

    def wait(self, frames) -> bool:
        """Block until the wake word is heard (True) or `frames` runs out (False)."""
        buf = np.zeros(0, dtype=np.int16)
        for frame in frames:
            buf = np.concatenate([buf, (frame * 32767).astype(np.int16)])
            while buf.size >= 1280:  # openWakeWord wants 80 ms chunks
                chunk, buf = buf[:1280], buf[1280:]
                scores = self.model.predict(chunk)
                if max(scores.values(), default=0) >= self.threshold:
                    self.model.reset()
                    return True
        return False
