"""Jarvis on your phone through a private Telegram bot.

Why Telegram: it reaches you anywhere, on any phone, with real push
notifications, and the laptop only makes outgoing requests (long polling), so
nothing on the laptop is exposed to the internet and no router setup is needed.

- Chat with Jarvis by text, or send a voice note (transcribed by Groq Whisper,
  or the local Whisper model without a key).
- Jarvis messages you first: reminders, the morning briefing, and alerts such as
  low battery, low disk or a failed build.
- Only one Telegram account can use it: the first one to send /start with the
  pairing code that Jarvis shows on the laptop. Everyone else is ignored.

Alerts arrive through the same Notifier as ntfy, so the per-kind switches
(notify.send), the repeat filter and the hourly cap apply to both.

Setup: create a bot with @BotFather, save its token as TELEGRAM_BOT_TOKEN
(docs/PHONE.md, part 3), start Jarvis, then send your bot "/start <code>".
"""

from __future__ import annotations

import json
import os
import queue
import random
import threading
import time
from pathlib import Path
from typing import Callable

import requests

API = "https://api.telegram.org"


class TextSpeaker:
    """Collects what Jarvis would say, so it can be sent as one message."""

    def __init__(self):
        self.parts: list[str] = []

    def feed(self, sentence: str) -> None:
        self.parts.append(sentence)

    def say(self, sentence: str) -> bool:
        self.parts.append(sentence)
        return False

    def wait(self) -> bool:
        return False

    def interrupt(self) -> None:
        pass

    def take(self) -> str:
        text, self.parts = " ".join(self.parts).strip(), []
        return text


class TelegramBridge:
    def __init__(self, token: str, state_path: str | Path, make_assistant: Callable, transcribe: Callable | None = None,
                 http=None, poll_timeout: int = 30):
        self.token = token
        self.base = f"{API}/bot{token}"
        self.state_path = Path(state_path).expanduser()
        self.http = http or requests
        self.poll_timeout = poll_timeout
        self.transcribe = transcribe
        self.offset = 0
        self.answers: queue.Queue = queue.Queue()
        self.waiting_for_answer = threading.Event()
        self.lock = threading.Lock()  # one conversation turn at a time
        state = {}
        if self.state_path.is_file():
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except ValueError:
                state = {}
        self.owner_chat: int | None = state.get("chat_id")
        self.owner_name: str = state.get("name", "")
        self.code = f"{random.randint(0, 999999):06d}"
        self.speaker = TextSpeaker()
        self.bot = make_assistant(self.speaker, self._listen)
        self.username = ""

    # ---- Telegram API ---------------------------------------------------------

    def _call(self, method: str, timeout: float = 15, **params):
        resp = self.http.post(f"{self.base}/{method}", json=params, timeout=timeout)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description', resp.status_code)}")
        return data["result"]

    def send(self, text: str, chat_id: int | None = None) -> None:
        chat = chat_id or self.owner_chat
        if not chat or not text:
            return
        for i in range(0, len(text), 4000):  # Telegram's limit is 4096 characters per message
            self._call("sendMessage", chat_id=chat, text=text[i:i + 4000])

    ICONS = {"reminders": "⏰", "alerts": "⚠️", "briefing": "☀️", "away": "💬", "problems": "🚨", "status": "🤖"}

    def notify(self, kind: str, text: str, title: str = "") -> None:
        """A Notifier channel: push an alert to the owner's phone. Safe from any thread; never raises."""
        if not self.owner_chat:
            return
        icon = self.ICONS.get(kind, "🤖")
        try:
            if kind == "briefing":  # turn raw facts into a friendly message with the model
                text = self.ask("Write my morning briefing as a short, friendly message of three to five "
                                "sentences, using these facts:\n" + text)
            elif title and kind in ("reminders", "alerts", "problems"):
                text = f"{title}: {text}"
            self.send(f"{icon} {text}")
        except Exception as exc:
            print(f"  [telegram] couldn't send: {exc}")

    # ---- conversation -----------------------------------------------------------

    def _listen(self) -> str:
        """The Assistant asks 'should I go ahead?' mid-turn: wait for the next message."""
        self.send(self.speaker.take() or "Should I go ahead? Reply yes or no.")
        self.waiting_for_answer.set()
        try:
            return self.answers.get(timeout=120)
        except queue.Empty:
            return ""
        finally:
            self.waiting_for_answer.clear()

    def ask(self, text: str) -> str:
        with self.lock:
            self.speaker.take()
            self.bot.respond(text)
            return self.speaker.take() or "Done."

    def handle(self, update: dict) -> None:
        msg = update.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat:
            return
        if self.owner_chat is None:
            if text.startswith("/start") and text.split()[-1] == self.code:
                self._pair(chat, (msg.get("from") or {}).get("first_name", ""))
            else:
                self.send("Hi! To link this chat to Jarvis, send /start followed by the 6-digit code "
                          "shown on your laptop.", chat)
            return
        if chat != self.owner_chat:
            self.send("Sorry, this is a private assistant.", chat)
            return
        if msg.get("voice") or msg.get("audio"):
            text = self._voice_to_text(msg.get("voice") or msg.get("audio"))
            if not text:
                self.send("I couldn't make out that voice note. Could you try again, or type it?")
                return
            self.send(f"🎙 “{text}”")
        if not text or text == "/start":
            self.send("I'm here. Ask me anything, or say remind me to... ")
            return
        if self.waiting_for_answer.is_set():
            self.answers.put(text)
            return
        threading.Thread(target=self._reply, args=(text,), daemon=True).start()

    def _reply(self, text: str) -> None:
        try:
            self._call("sendChatAction", chat_id=self.owner_chat, action="typing")
        except Exception:
            pass
        try:
            self.send(self.ask(text))
        except Exception as exc:
            from jarvis.errors import explain

            self.send(explain(exc))

    def _pair(self, chat: int, name: str) -> None:
        self.owner_chat, self.owner_name = chat, name
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"chat_id": chat, "name": name}), encoding="utf-8")
        print(f"  [telegram] linked to {name or 'your account'}")
        self.send(f"Linked! Hi{' ' + name if name else ''}, I'm Jarvis. Message me or send a voice note any time. "
                  "I'll also send you reminders, your morning briefing and any alerts here.")

    def unlink(self) -> None:
        self.owner_chat, self.owner_name = None, ""
        self.code = f"{random.randint(0, 999999):06d}"
        self.state_path.unlink(missing_ok=True)

    def _voice_to_text(self, voice: dict) -> str:
        if self.transcribe is None:
            return ""
        try:
            info = self._call("getFile", file_id=voice["file_id"])
            audio = self.http.get(f"{API}/file/bot{self.token}/{info['file_path']}", timeout=30).content
            return self.transcribe(audio, Path(info["file_path"]).name)
        except Exception as exc:
            print(f"  [telegram] voice note failed: {exc}")
            return ""

    # ---- running -----------------------------------------------------------------

    def poll_once(self) -> None:
        for u in self._get_updates():
            self.offset = max(self.offset, u["update_id"] + 1)
            try:
                self.handle(u)
            except Exception as exc:
                print(f"  [telegram] {exc}")

    def _get_updates(self) -> list:
        resp = self.http.post(f"{self.base}/getUpdates",
                              json={"offset": self.offset, "timeout": self.poll_timeout, "allowed_updates": ["message"]},
                              timeout=self.poll_timeout + 10)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "getUpdates failed"))
        return data["result"]

    def start(self) -> None:
        me = self._call("getMe")
        self.username = me.get("username", "")
        if self.owner_chat:
            print(f"  [telegram] @{self.username} is linked to your phone")
        else:
            print(f"  [telegram] to link your phone, send @{self.username} this message:  /start {self.code}")

        def loop():
            backoff = 2
            while True:
                try:
                    self.poll_once()
                    backoff = 2
                except Exception as exc:
                    print(f"  [telegram] connection problem, retrying: {exc}")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)

        threading.Thread(target=loop, daemon=True, name="telegram").start()

    def status(self) -> dict:
        return {"enabled": True, "bot": self.username, "linked": bool(self.owner_chat),
                "name": self.owner_name, "code": None if self.owner_chat else self.code}


def transcriber_for(stt_cfg: dict) -> Callable[[bytes, str], str]:
    """Voice notes (OGG/Opus): Groq Whisper when a key is set, else local faster-whisper."""
    key = os.environ.get((stt_cfg.get("cloud") or {}).get("api_key_env", "GROQ_API_KEY"), "")
    local = {}

    def transcribe(audio: bytes, filename: str) -> str:
        if key:
            c = stt_cfg.get("cloud") or {}
            resp = requests.post(c.get("base_url", "https://api.groq.com/openai/v1").rstrip("/") + "/audio/transcriptions",
                                 headers={"Authorization": f"Bearer {key}"},
                                 data={"model": c.get("model", "whisper-large-v3-turbo"), "response_format": "json"},
                                 files={"file": (filename, audio)}, timeout=30)
            if resp.ok:
                return str(resp.json().get("text", "")).strip()
        import io

        from faster_whisper import WhisperModel

        if "model" not in local:
            local["model"] = WhisperModel(stt_cfg.get("model", "base.en"), device="cpu", compute_type="int8")
        segments, _ = local["model"].transcribe(io.BytesIO(audio), beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    return transcribe
