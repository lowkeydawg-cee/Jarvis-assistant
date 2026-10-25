"""The browser version of Jarvis: a tiny local web server around the same Assistant.

The browser does the ears and the voice (the Web Speech API, which on Edge
includes Microsoft's natural neural voices), so the laptop never loads Whisper
or Piper. This server only does the thinking and the tools.

Everything Jarvis says reaches the browser through one Server-Sent Events
stream at /api/events: replies, tool announcements, confirmation questions,
watcher alerts and the briefing. The browser posts what you said to
/api/chat, which lands in the Assistant's inbox like any other request.

Only the standard library is used, so there is nothing extra to install.
"""

from __future__ import annotations

import json
import queue
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlsplit

from jarvis import __version__
from jarvis.assistant import Assistant

CONFIRM_TIMEOUT = 60
MAX_BODY = 4_000_000  # about 40 seconds of 16 kHz audio, base64-encoded


class EventHub:
    """Fans events out to every open browser tab.

    Only one tab is "active": the newest one to connect. Every tab speaks what it
    receives, so two open tabs (or a forgotten one) meant every reply was heard
    twice in two different voices. Older tabs are told to stand down instead.
    """

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.active: str | None = None

    def claim(self, client: str) -> None:
        """Make `client` the one tab that listens and speaks; the others go quiet."""
        self.active = client
        self.publish({"type": "active", "client": client})

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            q.put(event)


class WebSpeaker:
    """Speaker stand-in: sentences go to the browser, which speaks them."""

    def __init__(self, hub: EventHub):
        self.hub = hub
        self._interrupted = threading.Event()

    def feed(self, sentence: str) -> None:
        if not self._interrupted.is_set():
            self.hub.publish({"type": "sentence", "text": sentence})

    def say(self, sentence: str) -> bool:
        self.feed(sentence)
        return False

    def wait(self) -> bool:
        """Called at the end of each model round. True if the user cut Jarvis off."""
        hit = self._interrupted.is_set()
        self._interrupted.clear()
        return hit

    def interrupt(self) -> None:
        self._interrupted.set()


class WebSession:
    """One Assistant, its worker thread, and the plumbing for confirmations."""

    def __init__(self, llm, cfg: dict):
        self.cfg = cfg
        self.llm = llm
        self.hub = EventHub()
        self.speaker = WebSpeaker(self.hub)
        self.answers: queue.Queue = queue.Queue()
        self.confirming = threading.Event()
        self.bot = Assistant(llm, self.speaker, self._listen, cfg)
        self.bot.on_error = self._on_error
        self.voice = None  # jarvis.voiceid.VoiceID when voice recognition is set up
        self.skills = None  # jarvis.skills.Skills when reminders are on
        self._worker = threading.Thread(target=self._work, daemon=True, name="jarvis-web")

    def attach_skills(self, skills) -> None:
        """Show reminders in the page: refresh the list on changes, and pop a notification when one fires."""
        self.skills = skills
        skills.store.on_change = lambda: self.hub.publish({"type": "reminders"})
        self.bot.on_reminder = lambda r, text: self.hub.publish({"type": "reminder", "title": r.text, "text": text})

    def start(self) -> None:
        self._worker.start()

    # The Assistant only listens mid-turn to ask "should I go ahead?".
    def _listen(self) -> str:
        while not self.answers.empty():
            self.answers.get_nowait()
        self.confirming.set()
        self.hub.publish({"type": "confirm"})
        try:
            return self.answers.get(timeout=CONFIRM_TIMEOUT)
        except queue.Empty:
            return ""
        finally:
            self.confirming.clear()
            self.hub.publish({"type": "confirm_done"})

    def _on_error(self, exc: Exception) -> None:
        self.hub.publish({"type": "error", "text": str(exc)})
        self.speaker.say("Something went wrong on my end. The details are on screen.")

    def _work(self) -> None:
        while True:
            self.bot.attention.wait()
            self.hub.publish({"type": "state", "state": "thinking"})
            self.bot.last_source = ""  # stays empty for announcements such as reminders
            self.bot.drain_inbox()
            self.hub.publish({"type": "state", "state": "idle", "provider": self.bot.last_source})

    # ---- actions from the browser ----------------------------------------

    def chat(self, text: str, audio_b64: str = "") -> None:
        text = text.strip()
        if not text:
            return
        if self.confirming.is_set():
            self.answers.put(text)  # "yes" or "no" to a pending question
            return
        self.bot.post("ask", text, self.check_voice(audio_b64))

    def check_voice(self, audio_b64: str) -> dict | None:
        """Who said it, from the recording of the spoken message. None when it can't be checked."""
        if not audio_b64 or self.voice is None or not self.voice.enrolled:
            return None
        from jarvis.voiceid import decode_pcm16

        try:
            owner, score = self.voice.identify(decode_pcm16(audio_b64))
        except Exception as exc:  # too short, model missing: answer normally rather than fail
            print(f"  [voice] couldn't check the voice: {exc}")
            return None
        result = {"owner": owner, "score": round(score, 2), "name": self.voice.name if owner else ""}
        self.hub.publish({"type": "speaker", **result})
        return result

    def enroll(self, name: str, clips: list[str]) -> dict:
        from jarvis.voiceid import decode_pcm16

        quality = self.voice.enroll([decode_pcm16(c) for c in clips], name)
        self.bot.owner_name = self.voice.name
        return {"ok": True, "name": self.voice.name, "quality": round(quality, 2)}

    def reminders(self) -> list[dict]:
        if self.skills is None:
            return []
        from jarvis.reminders import when_text

        return [{"id": r.id, "text": r.text, "due": r.due, "kind": r.kind, "repeat": r.repeat,
                 "when": when_text(r.when, repeat=r.repeat)} for r in self.skills.store.upcoming()[:20]]

    def today(self) -> dict:
        """Everything for the Today panel. Each part is optional, so one slow service never blanks the rest."""
        import shutil
        from pathlib import Path

        out: dict = {"weather": None, "city": "", "headlines": [], "battery": None,
                     "disk_free_gb": round(shutil.disk_usage(Path.home()).free / 1024 ** 3)}
        try:
            import psutil

            b = psutil.sensors_battery()
            out["battery"] = {"percent": round(b.percent), "plugged": bool(b.power_plugged)} if b else None
        except Exception:
            pass
        online = self.bot.online
        if online is not None:
            out["city"] = online.city
            try:
                out["weather"] = online.weather() if online.city else None
            except Exception:
                pass
            try:
                out["headlines"] = online.headlines(4)
            except Exception:
                pass
        return out

    def set_city(self, city: str) -> str:
        if self.bot.online is None:
            raise ValueError("Online features are turned off in config.yaml.")
        try:
            return self.bot.online.set_city(city)
        except Exception as exc:
            raise ValueError(str(exc)) from exc

    def confirm(self, answer: str) -> None:
        self.answers.put(answer)

    def status(self) -> dict:
        llm = self.llm
        return {
            "version": __version__,
            "brain": llm.describe() if hasattr(llm, "describe") else f"local ({llm.model})",
            "cloud": bool(getattr(llm, "cloud", None)),
            "private": bool(getattr(llm, "private", True)),
            "tools": sorted(self.bot.toolbox.tools),
            "owner": self.bot.owner_name,
            "voice": {"available": self.voice is not None,
                      "enrolled": bool(self.voice and self.voice.enrolled)},
            "reminders": self.skills is not None,
            "online": self.bot.online is not None,
            "city": self.bot.online.city if self.bot.online is not None else "",
        }

    def set_private(self, value: bool) -> None:
        if hasattr(self.llm, "private"):
            self.llm.private = value


def page() -> bytes:
    return resources.files("jarvis").joinpath("webui/index.html").read_bytes()


def make_handler(session: WebSession, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"Jarvis/{__version__}"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        # Only answer requests addressed to this computer, from this page. That
        # stops other websites from talking to Jarvis behind your back.
        def _trusted(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or urlsplit(origin).netloc in allowed_hosts

        def _send(self, status: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: dict, status: int = 200) -> None:
            self._send(status, json.dumps(data).encode())

        def do_GET(self):
            if not self._trusted():
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            path = urlsplit(self.path).path
            if path == "/":
                return self._send(200, page(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(session.status())
            if path == "/api/reminders":
                return self._json({"reminders": session.reminders()})
            if path == "/api/today":
                return self._json(session.today())
            if path == "/api/events":
                return self._events()
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if not self._trusted():
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:  # spoken messages carry a few seconds of audio; enrolment a few more
                    return self._json({"error": "too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._json({"error": "bad json"}, HTTPStatus.BAD_REQUEST)
            path = urlsplit(self.path).path
            if path == "/api/chat":
                session.chat(str(body.get("text", "")), str(body.get("audio") or ""))
            elif path == "/api/voice/enroll":
                if session.voice is None:
                    return self._json({"error": "Voice recognition isn't set up. Run "
                                                "python scripts/download_speaker_model.py"}, HTTPStatus.CONFLICT)
                try:
                    return self._json(session.enroll(str(body.get("name", "")), list(body.get("clips") or [])))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            elif path == "/api/voice/forget":
                if session.voice is not None:
                    session.voice.forget()
                    session.bot.owner_name = session.cfg["user"].get("name", "")
            elif path == "/api/reminders/delete":
                if session.skills is not None:
                    session.skills.store.remove(str(body.get("id", "")))
            elif path == "/api/confirm":
                session.confirm(str(body.get("answer", "")))
            elif path == "/api/interrupt":
                session.speaker.interrupt()
            elif path == "/api/settings":
                if "private" in body:
                    session.set_private(bool(body["private"]))
                if body.get("city"):
                    try:
                        session.set_city(str(body["city"]))
                    except ValueError as exc:
                        return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            elif path == "/api/reset":
                session.bot.history.clear()
            else:
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            self._json({"ok": True, **session.status()})

        def _events(self):
            q = session.hub.subscribe()
            client = parse_qs(urlsplit(self.path).query).get("client", [""])[0][:64]
            if client:
                session.hub.claim(client)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")  # keeps proxies and the browser from timing out
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                session.hub.unsubscribe(q)

    return Handler


def serve(session: WebSession, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True):
    names = {host, "localhost", "127.0.0.1"} if host in {"127.0.0.1", "localhost", "0.0.0.0"} else {host}
    allowed: set[str] = set()
    httpd = ThreadingHTTPServer((host, port), make_handler(session, allowed))
    httpd.daemon_threads = True
    port = httpd.server_port  # the real port, in case 0 asked for any free one
    allowed.update(f"{n}:{port}" for n in names)
    session.start()
    url = f"http://{'localhost' if host in {'127.0.0.1', '0.0.0.0'} else host}:{port}"
    print(f"  Jarvis is live at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    return httpd, url
