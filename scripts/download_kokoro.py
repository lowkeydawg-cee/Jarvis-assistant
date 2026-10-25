"""Download the Kokoro voice model into ./voices (a far more natural voice than Piper).

Install the package first:  pip install -e ".[kokoro]"

Usage:
    python scripts/download_kokoro.py          # int8 model, about 90 MB, fastest on a CPU
    python scripts/download_kokoro.py full     # full-precision model, about 310 MB
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"


def main() -> None:
    model = "kokoro-v1.0.onnx" if sys.argv[1:] == ["full"] else "kokoro-v1.0.int8.onnx"
    out_dir = Path("voices")
    out_dir.mkdir(exist_ok=True)
    for name in (model, "voices-v1.0.bin"):
        target = out_dir / name
        if target.exists():
            print(f"Already have {target}")
            continue
        print(f"Downloading {name}...")
        urllib.request.urlretrieve(f"{BASE}/{name}", target)
    print(f"Done. In config.yaml set tts.engine: kokoro and tts.kokoro_model: voices/{model}")


if __name__ == "__main__":
    main()
