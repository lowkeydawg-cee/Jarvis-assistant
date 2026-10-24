"""Download a Piper voice into ./voices so Jarvis can speak.

Usage:
    python scripts/download_voice.py                 # default British male voice
    python scripts/download_voice.py en_US-ryan-high # any voice from the Piper catalog
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def voice_url(name: str) -> str:
    # name looks like en_GB-alan-medium -> en/en_GB/alan/medium/en_GB-alan-medium.onnx
    locale, speaker, quality = name.split("-")
    lang = locale.split("_")[0]
    return f"{BASE}/{lang}/{locale}/{speaker}/{quality}/{name}.onnx"


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "en_GB-alan-medium"
    out_dir = Path("voices")
    out_dir.mkdir(exist_ok=True)
    url = voice_url(name)
    for suffix in ("", ".json"):
        target = out_dir / f"{name}.onnx{suffix}"
        if target.exists():
            print(f"Already have {target}")
            continue
        print(f"Downloading {target.name}...")
        urllib.request.urlretrieve(url + suffix, target)
    print(f"Done. Set tts.voice to voices/{name}.onnx in config.yaml if you picked a different voice.")


if __name__ == "__main__":
    main()
