"""Every turn is timed; only timings are stored."""

import json

from jarvis.assistant import Assistant
from jarvis.config import DEFAULTS
from jarvis.metrics import KEEP, Metrics, percentile


def test_percentiles():
    assert percentile([], 50) == 0
    assert percentile([100, 200, 300, 400, 500], 50) == 300
    assert percentile([100, 200, 300, 400, 500], 95) == 500


def test_summary_by_source_and_persistence(tmp_path):
    m = Metrics(tmp_path / "metrics.jsonl")
    for ms in (900, 1000, 1200):
        m.record("cloud", ms, ms + 300, tools=1, tokens=800, model="gpt")
    m.record("instant", 4, 4)
    s = m.summary()
    assert s["turns"] == 4 and s["by_source"]["cloud"] == {"count": 3, "p50_ms": 1000, "p95_ms": 1200}
    assert s["avg_tokens"] == 800 and s["recent"][-1] == {"source": "instant", "first_ms": 4}
    assert Metrics(tmp_path / "metrics.jsonl").summary()["turns"] == 4  # survives a restart


def test_the_file_stays_small(tmp_path):
    m = Metrics(tmp_path / "metrics.jsonl")
    for _ in range(KEEP + 20):
        m.record("cloud", 1, 1)
    assert len(m.turns) == KEEP
    assert len((tmp_path / "metrics.jsonl").read_text().splitlines()) <= KEEP + 20


class Model:
    model = "m"
    last_provider, last_model = "cloud", "gpt-oss"

    def chat_stream(self, messages, tools=None, on_text=None):
        on_text and on_text("A secret reply. ")
        return {"role": "assistant", "content": "A secret reply."}


class Quiet:
    def say(self, s):
        return False

    def feed(self, s):
        pass

    def wait(self):
        return False


def test_turns_are_timed_and_words_are_never_stored(tmp_path):
    bot = Assistant(Model(), Quiet(), lambda: "", DEFAULTS)
    bot.metrics = Metrics(tmp_path / "metrics.jsonl")
    bot.respond("my private question")
    bot.respond("what time is it")
    a, b = bot.metrics.turns
    assert a["source"] == "cloud" and a["model"] == "gpt-oss" and a["tokens"] > 0 and b["source"] == "instant"
    raw = (tmp_path / "metrics.jsonl").read_text()
    assert "private" not in raw and "secret" not in raw
    assert all(isinstance(json.loads(line), dict) for line in raw.splitlines())
