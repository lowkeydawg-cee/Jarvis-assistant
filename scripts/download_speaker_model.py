"""Download the speaker-recognition model (about 25 MB) into ./voices, so Jarvis can recognise your voice.

Install the package first:  pip install -e ".[voiceid]"
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
       "wespeaker_en_voxceleb_resnet34.onnx")


def main() -> None:
    target = Path("voices") / "speaker-id.onnx"
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        print(f"Already have {target}")
        return
    print("Downloading the speaker-recognition model...")
    urllib.request.urlretrieve(URL, target)
    print(f"Done: {target}. Then open Settings > Teach Jarvis your voice in the browser app.")


if __name__ == "__main__":
    main()
