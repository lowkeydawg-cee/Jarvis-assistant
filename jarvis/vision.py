"""Screen awareness: grab a screenshot and ask a local vision model about it.

Screenshots are only taken when you ask (by voice or the hotkey), are sent
only to your local Ollama, and are never written to disk.
"""

from __future__ import annotations

import base64
import io

DEFAULT_PROMPT = (
    "You are looking at a screenshot of the user's computer screen. "
    "Describe what matters: the app in focus, any error messages or warnings word for word, "
    "and anything that looks wrong. Be concise."
)


def capture_screen(max_width: int = 1600, monitor: int = 1) -> bytes:
    """Return the chosen monitor as PNG bytes, downscaled so vision models stay fast."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        mon = sct.monitors[min(monitor, len(sct.monitors) - 1)]
        shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return encode_png(img, max_width)


def encode_png(img, max_width: int = 1600) -> bytes:
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class ScreenVision:
    def __init__(self, llm, cfg: dict, capture=capture_screen):
        self.llm = llm
        self.model = cfg.get("model", "qwen2.5vl:7b")
        self.max_width = int(cfg.get("max_width", 1600))
        self.monitor = int(cfg.get("monitor", 1))
        self.capture = capture

    def look(self, question: str = "") -> str:
        png = self.capture(max_width=self.max_width, monitor=self.monitor)
        prompt = DEFAULT_PROMPT + (f"\nThe user asks: {question}" if question else "")
        return self.llm.vision(self.model, prompt, base64.b64encode(png).decode("ascii"))


class Hotkey:
    """Global hotkey (default Ctrl+Alt+J) that works while any app is focused."""

    def __init__(self, combo: str, callback):
        from pynput import keyboard

        self.listener = keyboard.GlobalHotKeys({combo: callback})

    def start(self) -> None:
        self.listener.daemon = True
        self.listener.start()
