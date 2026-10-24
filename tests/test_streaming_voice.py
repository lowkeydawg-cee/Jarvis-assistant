import threading
import time

import numpy as np

from jarvis.streaming import SentenceStream
from jarvis.voice import Speaker, Vad


def stream_all(text, step=3):
    s, out = SentenceStream(), []
    for i in range(0, len(text), step):
        out += s.feed(text[i : i + step])
    return out + s.flush()


def test_sentences_come_out_as_soon_as_they_end():
    s = SentenceStream()
    assert s.feed("Your build passed. Two warn") == ["Your build passed."]
    assert s.feed("ings remain.") == []
    assert s.flush() == ["Two warnings remain."]


def test_code_blocks_are_never_read_aloud():
    out = stream_all("Try this fix.\n```python\nimport os\nprint(os.getcwd())\n```\nThat should do it.")
    assert out == ["Try this fix.", "I've left the code on screen.", "That should do it."]


def test_bullets_and_markdown_become_clean_sentences():
    out = stream_all("Here's the plan:\n- **Pull** the repo\n- Run the tests\n")
    assert out == ["Here's the plan:", "Pull the repo.", "Run the tests."]


def test_short_fragments_are_not_split():
    assert stream_all("Ok. That's done now.") == ["Ok. That's done now."]


class FakeVoice:
    def __init__(self):
        self.played, self.stopped = [], threading.Event()

    def synthesize(self, text):
        return np.full(10, len(self.played) + 1, dtype=np.int16)

    def play(self, audio):
        self.played.append(audio)
        self.stopped.wait(0.2)

    def stop(self):
        self.stopped.set()


def test_speaker_plays_in_order_and_reports_interrupts():
    voice = FakeVoice()
    sp = Speaker(voice, echo=False)
    for s in ["one.", "two.", "three."]:
        sp.feed(s)
    time.sleep(0.05)
    sp.interrupt()
    assert sp.wait() is True
    assert len(voice.played) < 3  # the rest of the reply was dropped
    voice.stopped.clear()
    assert sp.say("fresh start.") is False


def test_vad_tells_speech_from_silence():
    vad = Vad()
    assert not vad.is_speech(np.zeros(800, dtype=np.float32))
    vad.model = None  # energy fallback
    assert vad.is_speech((0.2 * np.sin(np.arange(800) / 3)).astype(np.float32))
