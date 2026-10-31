"""The Telegram bridge: pairing, chat, voice notes, and alerts through the Notifier."""

import json
import threading

from jarvis.assistant import Assistant
from jarvis.config import DEFAULTS
from jarvis.notify import Notifier
from jarvis.telegram import TelegramBridge


class FakeTelegram:
    """Records what the bot sends; answers getFile/getMe like Telegram."""

    def __init__(self):
        self.sent = []
        self.got = threading.Event()

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        if method == "sendMessage":
            self.sent.append((json["chat_id"], json["text"]))
            self.got.set()
        result = {"getMe": {"username": "jarvis_test_bot"}, "getFile": {"file_path": "voice/file_1.oga"}}.get(method, True)
        return Resp({"ok": True, "result": result})

    def get(self, url, timeout=None):
        return Resp(None, content=b"OggS fake audio")


class Resp:
    def __init__(self, data, content=b""):
        self.data, self.content, self.status_code = data, content, 200

    def json(self):
        return self.data


class Model:
    model = "test"

    def chat_stream(self, messages, tools=None, on_text=None):
        on_text and on_text("Hello from Jarvis.")
        return {"role": "assistant", "content": "Hello from Jarvis."}


def bridge(tmp_path, transcribe=None):
    http = FakeTelegram()
    b = TelegramBridge("123:abc", tmp_path / "telegram.json",
                       make_assistant=lambda speaker, listen: Assistant(Model(), speaker, listen, DEFAULTS),
                       transcribe=transcribe, http=http)
    return b, http


def msg(chat, text=None, **extra):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "from": {"first_name": "Saint"},
                                        **({"text": text} if text else {}), **extra}}


def test_pairing_needs_the_code_and_then_locks_to_one_chat(tmp_path):
    b, http = bridge(tmp_path)
    wrong = "000000" if b.code != "000000" else "999999"
    b.handle(msg(111, f"/start {wrong}"))
    assert b.owner_chat is None and "6-digit code" in http.sent[-1][1]
    b.handle(msg(111, f"/start {b.code}"))
    assert b.owner_chat == 111 and http.sent[-1][1].startswith("Linked! Hi Saint")
    assert json.loads((tmp_path / "telegram.json").read_text())["chat_id"] == 111
    b.handle(msg(222, "hello"))
    assert http.sent[-1] == (222, "Sorry, this is a private assistant.")
    again, _ = bridge(tmp_path)
    assert again.owner_chat == 111  # stays linked across restarts
    again.unlink()
    assert again.owner_chat is None and not (tmp_path / "telegram.json").exists()


def test_owner_messages_get_answers(tmp_path):
    b, http = bridge(tmp_path)
    b._pair(111, "Saint")
    assert b.ask("what time is it").startswith("It's ")  # instant answer, no model needed
    assert b.ask("tell me something nice") == "Hello from Jarvis."


def test_voice_notes_are_transcribed(tmp_path):
    heard = []
    b, http = bridge(tmp_path, transcribe=lambda audio, name: heard.append((audio, name)) or "what time is it")
    b._pair(111, "Saint")
    b.handle(msg(111, voice={"file_id": "abc"}))
    assert heard == [(b"OggS fake audio", "file_1.oga")]
    assert http.sent[-1] == (111, "🎙 “what time is it”")


def test_alerts_reach_telegram(tmp_path):
    b, http = bridge(tmp_path)
    b.notify("reminders", "Call mum.", "Reminder")  # not linked yet: nowhere to send
    assert http.sent == []
    b._pair(111, "Saint")
    b.notify("reminders", "Call mum.", "Reminder")
    assert http.sent[-1] == (111, "⏰ Reminder: Call mum.")
    b.notify("briefing", "Today is Saturday. Disk: 12 GB free.", "Your briefing")
    assert http.sent[-1] == (111, "☀️ Hello from Jarvis.")  # the model wrote the briefing


def test_notifier_sends_to_telegram_even_with_ntfy_off(tmp_path):
    n = Notifier(DEFAULTS["notify"], tmp_path, background=False)
    assert not n.ntfy_on and n.send("reminders", "Call mum.") is False  # nothing connected yet
    got, done = [], threading.Event()
    n.channels.append(lambda kind, message, title: (got.append((kind, message, title)), done.set()))
    assert n.send("reminders", "Call mum.", title="Reminder") is True
    assert done.wait(2) and got == [("reminders", "Call mum.", "Reminder")]
    assert n.send("reminders", "Call mum.") is False  # the same alert again within ten minutes is skipped


def test_switched_off_kinds_skip_telegram_too(tmp_path):
    cfg = {**DEFAULTS["notify"], "send": ["reminders"]}
    n = Notifier(cfg, tmp_path, background=False)
    got = []
    n.channels.append(lambda *a: got.append(a))
    assert n.send("briefing", "Morning.") is False and got == []
