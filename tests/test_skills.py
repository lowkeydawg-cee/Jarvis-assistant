"""Reminders, schedule, memory and voice recognition."""

import base64
import json
import threading
from datetime import datetime, timedelta

import numpy as np
import pytest

from jarvis.assistant import Assistant
from jarvis.config import DEFAULTS
from jarvis.memory import Memory, second_person
from jarvis.reminders import ReminderService, ReminderStore, announcement, parse_reminder, when_text
from jarvis.router import Router
from jarvis.skills import Skills
from jarvis.voiceid import VoiceID, decode_pcm16

NOW = datetime(2026, 10, 25, 13, 30)  # a Sunday afternoon


@pytest.mark.parametrize("said, what, due, repeat", [
    ("remind me to call mum at 5", "Call mum", NOW.replace(hour=17, minute=0), ""),
    ("Remind me at 5 p.m. to call mum", "Call mum", NOW.replace(hour=17, minute=0), ""),
    ("remind me in 20 minutes to check the oven", "Check the oven", NOW + timedelta(minutes=20), ""),
    ("remind me in half an hour to stretch", "Stretch", NOW + timedelta(minutes=30), ""),
    ("remind me to submit the report by friday", "Submit the report", datetime(2026, 10, 30, 9, 0), ""),
    ("remind me to call mum at 5 pm tomorrow", "Call mum", datetime(2026, 10, 26, 17, 0), ""),
    ("remind me at 9 tomorrow to pray", "Pray", datetime(2026, 10, 26, 9, 0), ""),
    ("remind me tonight at 8 to take my medicine", "Take my medicine", NOW.replace(hour=20, minute=0), ""),
    ("set a reminder for the gym at 18:30", "The gym", NOW.replace(hour=18, minute=30), ""),
    ("remind me every weekday at 8 to stand up", "Stand up", datetime(2026, 10, 26, 8, 0), "weekdays"),
    ("remind me every monday at 9am about the team meeting", "The team meeting", datetime(2026, 10, 26, 9, 0), "monday"),
    ("schedule a meeting with Alice tomorrow at 10", "A meeting with Alice", datetime(2026, 10, 26, 10, 0), ""),
])
def test_parse_reminder(said, what, due, repeat):
    assert parse_reminder(said, NOW) == (what, due, repeat)


def test_parse_without_a_time():
    assert parse_reminder("remind me to buy 2 eggs", NOW) == ("Buy 2 eggs", None, "")


def test_when_text_reads_naturally():
    assert when_text(NOW + timedelta(minutes=20), NOW) == "in 20 minutes"
    assert when_text(NOW.replace(hour=17, minute=0), NOW) == "at 5 PM"
    assert when_text(datetime(2026, 10, 26, 9, 15), NOW) == "tomorrow at 9:15 AM"
    assert when_text(datetime(2026, 10, 26, 8, 0), NOW, "weekdays") == "every weekday at 8 AM"


@pytest.fixture
def skills(tmp_path):
    clock = {"now": NOW}
    store = ReminderStore(tmp_path / "reminders.json")
    fired = []
    service = ReminderService(store, lambda r, late: fired.append(announcement(r, late)), now=lambda: clock["now"])
    s = Skills(store, Memory(tmp_path / "memory.json"), service, now=lambda: clock["now"])
    r = Router(routes=False)
    s.add_routes(r)
    return s, r, store, service, clock, fired


def say(router, text):
    hit = router.answer(text)
    return hit[1] if hit else None


def test_reminder_round_trip(skills):
    s, r, store, service, clock, fired = skills
    assert say(r, "Remind me to call mum at 5 pm") == "Okay, I'll remind you to call mum at 5 PM."
    assert say(r, "what's on my schedule today") == "You have 1 thing today: at 5 PM, call mum."
    clock["now"] = NOW.replace(hour=17, minute=0)
    service.tick()
    assert fired == ["Reminder: call mum."]
    assert store.upcoming() == []
    assert say(r, "snooze for 5 minutes") == "Okay, I'll remind you again in 5 minutes."
    assert store.upcoming()[0].when == NOW.replace(hour=17, minute=5)


def test_reminders_survive_a_restart(skills, tmp_path):
    s, r, *_ = skills
    say(r, "remind me tomorrow at 9 to email John")
    again = ReminderStore(tmp_path / "reminders.json")
    assert [x.text for x in again.upcoming()] == ["Email John"]


def test_repeating_reminder_moves_to_the_next_weekday(skills):
    s, r, store, service, clock, fired = skills
    say(r, "remind me every weekday at 8 to stand up")
    clock["now"] = datetime(2026, 10, 30, 8, 0)  # Friday
    store.items[0].due = clock["now"].isoformat(timespec="minutes")
    service.tick()
    assert fired == ["Reminder: stand up."]
    assert store.upcoming()[0].when == datetime(2026, 11, 2, 8, 0)  # Monday, skipping the weekend


def test_missed_reminder_is_announced_as_late(skills):
    s, r, store, service, clock, fired = skills
    say(r, "remind me in 10 minutes to stretch")
    clock["now"] = NOW + timedelta(hours=2)
    service.tick()
    assert fired == ["While I was off, you asked me to remind you to stretch, at 1:40 PM."]


def test_asks_when_then_completes(skills):
    s, r, store, *_ = skills
    assert say(r, "remind me to buy eggs") == "Sure. When should I remind you to buy eggs?"
    assert say(r, "in ten minutes") == "Okay, I'll remind you to buy eggs in 10 minutes."
    assert say(r, "in ten minutes") is None  # nothing pending any more: goes to the model


def test_cancel_and_clear(skills):
    s, r, store, *_ = skills
    say(r, "remind me to call mum at 5")
    say(r, "remind me to go to the gym at 7 pm")
    assert say(r, "cancel the gym reminder") == "Cancelled: go to the gym."
    assert say(r, "what are my reminders") == "Coming up: call mum at 5 PM."
    assert say(r, "clear all reminders") == "Cleared all 1 reminders."


def test_memory(skills):
    s, r, *_ = skills
    assert say(r, "Remember that my standup is at 10:30") == "Got it. I'll remember that your standup is at 10:30."
    assert say(r, "what do you remember about standup") == "You told me your standup is at 10:30."
    say(r, "remember my standup is at 11 now")  # replaces the old fact
    assert s.memory.search("standup") == ["my standup is at 11 now"]
    assert "my standup is at 11 now" in s.memory.prompt()
    assert say(r, "forget about my standup") == "Okay, I've forgotten that your standup is at 11 now."
    assert second_person("I'm sure my car is mine") == "you're sure your car is yours"


class Spoken:
    def __init__(self):
        self.said = []

    def say(self, s):
        self.said.append(s)
        return False

    def feed(self, s):
        self.said.append(s)

    def wait(self):
        return False


class Model:
    model = "test-model"

    def __init__(self):
        self.calls = []

    def chat_stream(self, messages, tools=None, on_text=None):
        self.calls.append((messages, tools))
        on_text and on_text("Hello there.")
        return {"role": "assistant", "content": "Hello there."}


def test_unrecognised_voice_gets_no_personal_answers(skills):
    s, r, store, *_ = skills
    say(r, "remind me to call mum at 5")
    model = Model()
    bot = Assistant(model, Spoken(), lambda: "", DEFAULTS)
    bot.router, bot.memory, bot.owner_name = r, s.memory, "Saint"
    s.memory.remember("my pin is in the drawer")
    stranger = {"owner": False, "score": 0.2}
    assert bot.respond("what's on my schedule today", speaking_as=stranger).startswith("Sorry, I only share that")
    bot.respond("tell me a joke", speaking_as=stranger)
    messages, tools = model.calls[-1]
    context = " ".join(m["content"] for m in messages if m["role"] == "system")
    assert tools is None and "NOT the owner" in context and "drawer" not in context
    bot.respond("tell me a joke", speaking_as={"owner": True, "score": 0.8})
    messages, tools = model.calls[-1]
    context = " ".join(m["content"] for m in messages if m["role"] == "system")
    assert tools and "Saint" in context and "drawer" in context


def fake_embedder(audio):
    # The "voice" is the clip's pitch, in 40 Hz bands: same band, same person.
    spectrum = np.abs(np.fft.rfft(audio[:16000]))[:4000]
    return spectrum.reshape(100, 40).sum(axis=1)


def tone(hz, seconds=1.5):
    t = np.arange(int(16000 * seconds)) / 16000
    return (0.3 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def test_voice_id_enrol_and_identify(tmp_path):
    v = VoiceID("unused.onnx", tmp_path / "voiceprint.json", threshold=0.6, embedder=fake_embedder)
    assert not v.enrolled and v.identify(tone(200)) == (False, 0.0)
    quality = v.enroll([tone(210), tone(212), tone(208)], "Saint")
    assert quality > 0.6
    assert v.identify(tone(211))[0] is True
    assert v.identify(tone(330))[0] is False
    again = VoiceID("unused.onnx", tmp_path / "voiceprint.json", embedder=fake_embedder)
    assert again.enrolled and again.name == "Saint"
    with pytest.raises(ValueError):
        v.embed(tone(200, seconds=0.3))  # too short
    v.forget()
    assert not v.enrolled and not (tmp_path / "voiceprint.json").exists()


def test_decode_pcm16():
    pcm = (np.array([0, 16384, -32768], dtype="<i2")).tobytes()
    assert decode_pcm16(base64.b64encode(pcm).decode()).tolist() == [0.0, 0.5, -1.0]


def pcm_b64(audio):
    return base64.b64encode((audio * 32767).astype("<i2").tobytes()).decode()


def test_web_recognises_the_owner_and_lists_reminders(tmp_path, skills):
    import urllib.request

    from jarvis.server import WebSession, serve

    s, r, store, *_ = skills
    session = WebSession(Model(), DEFAULTS)
    session.bot.router.routes += r.routes
    session.attach_skills(s)
    session.voice = VoiceID("unused.onnx", tmp_path / "vp.json", embedder=fake_embedder)
    httpd, _ = serve(session, "127.0.0.1", 0, open_browser=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    try:
        assert post("/api/voice/enroll", {"name": "Saint", "clips": [pcm_b64(tone(210)), pcm_b64(tone(214))]})["name"] == "Saint"
        assert session.bot.owner_name == "Saint"
        assert session.check_voice(pcm_b64(tone(212)))["owner"] is True
        assert session.check_voice(pcm_b64(tone(340)))["owner"] is False
        say(r, "remind me to call mum at 5")
        items = json.loads(urllib.request.urlopen(base + "/api/reminders", timeout=5).read())["reminders"]
        assert [i["text"] for i in items] == ["Call mum"]
        post("/api/reminders/delete", {"id": items[0]["id"]})
        assert store.upcoming() == []
        status = json.loads(urllib.request.urlopen(base + "/api/status", timeout=5).read())
        assert status["owner"] == "Saint" and status["voice"]["enrolled"] and status["reminders"]
    finally:
        httpd.shutdown()
