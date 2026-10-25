"""Lite mode: instant answers, a lighter local fallback, the mic check, cloud speech to text."""

from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest

from jarvis import stt
from jarvis.assistant import Assistant
from jarvis.cloud import HybridLLM, build_llm
from jarvis.config import DEFAULTS, _merge
from jarvis.mictest import diagnose
from jarvis.router import (
    BRIEFING_PATTERN, Router, add_builtin_routes, add_notes_route, briefing_route, normalise,
)

NOON = lambda: datetime(2026, 10, 25, 14, 5)  # noqa: E731  a Sunday


def router(**kw):
    r = Router(routes=False)
    add_builtin_routes(r, now=NOON, battery=kw.get("battery", lambda: (80, True)),
                       disk_usage=lambda p: SimpleNamespace(free=12 * 1024 ** 3))
    return r


def test_normalise_strips_openers_and_punctuation():
    assert normalise("Hey Jarvis, what's the time?") == "whats the time"
    assert normalise("Could you tell me what time it is please") == "what time it is"


@pytest.mark.parametrize("said", ["What time is it?", "hey jarvis what's the time", "Time please", "what is the time"])
def test_time_is_instant(said):
    assert router().answer(said) == ("time", "It's 2:05 PM.")


def test_longer_questions_still_go_to_the_model():
    r = router()
    assert r.answer("what time is it in Tokyo") is None
    assert r.answer("how do I free up disk space on windows") is None
    assert r.answer("why is my battery draining so fast") is None


def test_date_battery_disk():
    r = router()
    assert r.answer("what day is it") == ("date", "It's Sunday, 25 October.")
    assert r.answer("how's my battery") == ("battery", "Your battery is at 80 percent and charging.")
    assert r.answer("how much disk space do I have left") == ("disk", "You have 12 gigabytes free on your main drive.")
    assert router(battery=lambda: None).answer("battery")[1] == "I can't find a battery on this computer."


def test_broken_shortcut_falls_back_to_the_model():
    def boom():
        raise RuntimeError("no sensor")
    assert router(battery=boom).answer("battery level") is None


def test_notes_route_reads_the_best_match():
    r = Router(routes=False)
    seen = []

    def search(q, k):
        seen.append(q)
        return [(0.8, "C:/notes/auth.md", "The token was   expiring early,\nso I raised the TTL to an hour.")]

    add_notes_route(r, search)
    name, reply = r.answer("Where did I write about the auth fix?")
    assert name == "notes" and seen == ["auth fix"]
    assert reply == "The best match is in auth.md. It says: The token was expiring early, so I raised the TTL to an hour."
    add_notes_route(r2 := Router(routes=False), lambda q, k: [(0.1, "x.md", "unrelated")])
    add_notes_route(r3 := Router(routes=False), lambda q, k: [
        (0.9, "README.md", "# Catalog [![ci](https://x/b.svg)](https://x)\nA local agent. Docs at https://example.com")])
    assert r3.answer("search my notes for catalog")[1] == \
        "The best match is in README.md. It says: Catalog. A local agent. Docs at."
    assert r2.answer("search my notes for invoices")[1] == "I couldn't find anything about invoices in your notes."


def test_briefing_route_speaks_the_facts():
    r = Router(routes=False)
    r.add("briefing", BRIEFING_PATTERN, briefing_route(lambda: "Today is Sunday 25 October, it's 14:05.\nDisk: 12 GB free."))
    assert r.answer("Brief me") == ("briefing", "Today is Sunday 25 October, it's 14:05. Disk: 12 GB free.")
    assert r.answer("what does my day look like")[0] == "briefing"


class NoLLM:
    def chat_stream(self, *a, **k):
        raise AssertionError("the model should not be called")


class Spoken:
    def __init__(self):
        self.said = []

    def say(self, s):
        self.said.append(s)
        return False


def test_assistant_answers_instantly_without_the_model():
    speaker = Spoken()
    bot = Assistant(NoLLM(), speaker, lambda: "", DEFAULTS)
    bot.router = router()
    assert bot.respond("what time is it") == "It's 2:05 PM."
    assert speaker.said == ["It's 2:05 PM."]
    assert [m["role"] for m in bot.history] == ["user", "assistant"]


def test_router_can_be_switched_off():
    bot = Assistant(NoLLM(), Spoken(), lambda: "", _merge(DEFAULTS, {"router": {"enabled": False}}))
    assert bot.router is None


class Recorder:
    def __init__(self):
        self.tools = "unset"
        self.model = "llama3.2:1b"

    def chat_stream(self, messages, tools=None, on_text=None):
        self.tools = tools
        self.messages = messages
        return {"role": "assistant", "content": "ok"}


def test_local_fallback_can_skip_tool_descriptions():
    local = Recorder()
    HybridLLM(local, None, log=lambda m: None, local_tools=False).chat_stream(
        [{"role": "system", "content": "long persona about tools"}, {"role": "user", "content": "hi"}],
        tools=[{"x": 1}])
    assert local.tools is None
    assert local.messages[0]["content"].startswith("You are Jarvis, a helpful voice assistant")
    assert local.messages[1] == {"role": "user", "content": "hi"}
    HybridLLM(local, None, log=lambda m: None).chat_stream([], tools=[{"x": 1}])
    assert local.tools == [{"x": 1}]


def test_build_llm_local_without_tools_wraps_ollama(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    llm = build_llm(_merge(DEFAULTS, {"llm": {"provider": "local", "local_tools": False}}), log=lambda m: None)
    assert isinstance(llm, HybridLLM) and llm.cloud is None and llm.local_tools is False


def test_mic_diagnosis_names_the_problem():
    assert "No audio" in diagnose([], [], 0.5, 0.012)[0]
    assert "silent" in diagnose([0.0005] * 50, [0.0] * 50, 0.5, 0.012)[1]
    assert "very quiet" in diagnose([0.01] * 50, [0.0] * 50, 0.5, 0.012)[1]
    assert "isn't recognised as speech" in diagnose([0.1] * 50, [0.2] * 50, 0.5, 0.012)[1]
    assert "mic is fine" in diagnose([0.1] * 50, [0.9] * 50, 0.5, 0.012)[1]


def test_vad_scores_each_frame_with_half_a_second_of_context():
    from jarvis.voice import Vad

    seen = []

    def model(audio):
        seen.append(audio.size)
        return np.linspace(0, 1, audio.size // 512)  # newest windows score highest

    vad = Vad()
    vad.model = model
    for _ in range(20):  # 20 frames of 50 ms = 1 s of audio
        p = vad.speech_prob(np.zeros(800, dtype=np.float32))
    assert seen[0] == 512 and max(seen) == (8000 // 512) * 512  # context grows, capped at 0.5 s
    assert p == 1.0
    vad.reset()
    vad.speech_prob(np.zeros(800, dtype=np.float32))
    assert seen[-1] == 512


def test_to_wav_is_16_bit_mono():
    wav = stt.to_wav(np.zeros(1600, dtype=np.float32))
    assert wav[:4] == b"RIFF" and len(wav) == 44 + 3200


class FakeResp:
    def __init__(self, text=None, fail=False):
        self.text, self.fail = text, fail

    def raise_for_status(self):
        if self.fail:
            raise RuntimeError("503")

    def json(self):
        return {"text": self.text}


def test_cloud_transcriber_sends_wav_and_falls_back(monkeypatch):
    calls = []

    def post(url, headers, data, files, timeout):
        calls.append((url, headers["Authorization"], data["model"], files["file"][0]))
        return FakeResp(" Hello there. ")

    t = stt.CloudTranscriber(DEFAULTS["stt"], "k", post=post)
    assert t.transcribe(np.ones(1600, dtype=np.float32) * 0.1) == "Hello there."
    assert calls == [("https://api.groq.com/openai/v1/audio/transcriptions", "Bearer k",
                      "whisper-large-v3-turbo", "speech.wav")]

    class Local:
        def __init__(self, cfg):
            pass

        def transcribe(self, audio):
            return "local words"

    monkeypatch.setattr(stt, "Transcriber", Local)
    t = stt.CloudTranscriber(DEFAULTS["stt"], "k", post=lambda *a, **k: FakeResp(fail=True))
    assert t.transcribe(np.ones(10, dtype=np.float32)) == "local words"
    assert t.transcribe(np.zeros(0, dtype=np.float32)) == ""


def test_build_transcriber_without_key_stays_local(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(stt, "Transcriber", lambda cfg: "local")
    assert stt.build_transcriber(_merge(DEFAULTS["stt"], {"provider": "cloud"})) == "local"
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert isinstance(stt.build_transcriber(_merge(DEFAULTS["stt"], {"provider": "cloud"})), stt.CloudTranscriber)
