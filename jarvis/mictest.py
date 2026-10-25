"""`jarvis --mic-test`: see what Jarvis actually hears.

Lists the input devices, records a few seconds with a live level meter, runs
the same speech detector Jarvis uses, then transcribes the clip. The summary
says in plain words what's wrong when Jarvis never gets past [listening].
"""

from __future__ import annotations

import sys
import time

import numpy as np

FRAME_SECONDS = 0.05


def list_inputs(sd) -> list[tuple[int, str, bool]]:
    """(index, name, is_default) for every device that can record."""
    default = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
    return [(i, d["name"], i == default) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def diagnose(levels: list[float], probs: list[float], vad_threshold: float, silence_threshold: float) -> list[str]:
    """Turn raw measurements into advice. Pure, so it's easy to test."""
    if not levels:
        return ["No audio came in at all. Another app may be holding the microphone, or Windows blocks access: "
                "Settings > Privacy & security > Microphone > let desktop apps use it."]
    peak = max(levels)
    speech = sum(p >= vad_threshold for p in probs) * FRAME_SECONDS
    out = [f"Loudest level {peak:.3f} (talking normally is usually 0.02 to 0.3). Speech detected for {speech:.1f} s."]
    if peak < 0.002:
        out.append("The mic is basically silent. It's muted, the wrong device, or blocked in Windows privacy "
                   "settings. Try another device with audio.device in config.yaml.")
    elif peak < silence_threshold * 1.5:
        out.append("The mic works but is very quiet. Raise its level in Windows Sound settings (Input > volume), "
                   "or move closer.")
    elif speech < 0.3:
        out.append(f"Sound comes in, but it isn't recognised as speech. Lower voice.vad_threshold "
                   f"(now {vad_threshold}) to 0.3, and cut background noise or use a headset.")
    else:
        out.append("The mic is fine. If Jarvis still misses you, wait for [listening] before you speak.")
    return out


def run(cfg: dict, seconds: float = 6.0, transcribe: bool = True) -> None:
    import sounddevice as sd

    from jarvis.voice import Vad

    print("Input devices (* = Windows default):")
    for i, name, default in list_inputs(sd):
        print(f"  {'*' if default else ' '} {i:>2}  {name}")
    device = cfg["audio"].get("device")
    print(f"\nUsing: {device if device is not None else 'the default'}  (set audio.device in config.yaml to change)")

    rate = int(cfg["audio"]["sample_rate"])
    vad = Vad(threshold=float(cfg["voice"]["vad_threshold"]))
    print(f"\nSay something for the next {seconds:.0f} seconds...\n")
    clip = sd.rec(int(seconds * rate), samplerate=rate, channels=1, dtype="float32", device=device)
    block = int(rate * FRAME_SECONDS)
    levels: list[float] = []
    probs: list[float] = []
    start, pos = time.monotonic(), 0
    while (done := time.monotonic() - start) < seconds:
        time.sleep(FRAME_SECONDS)
        upto = min(int(done * rate), len(clip))
        if upto - pos < block:
            continue
        frame, pos = clip[pos:upto, 0], upto  # each sample once, so the detector's context stays true
        level = float(np.sqrt(np.mean(frame ** 2)))
        prob = vad.speech_prob(frame)
        levels.append(level)
        probs.append(prob)
        bar = "#" * min(40, int(level * 400))
        sys.stdout.write(f"\r  level {level:.3f} |{bar:<40}| {'SPEECH' if prob >= vad.threshold else '      '}")
        sys.stdout.flush()
    sd.wait()
    print("\n")
    for line in diagnose(levels, probs, vad.threshold, float(cfg["audio"]["silence_threshold"])):
        print("  " + line)

    if transcribe and max(levels, default=0) >= 0.002:
        from jarvis.stt import build_transcriber

        print("\nTranscribing what you said...")
        text = build_transcriber(cfg["stt"]).transcribe(clip[:, 0])
        print(f'  Jarvis heard: "{text}"' if text else "  Jarvis heard no words.")
