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
import time
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
        self.clients: dict[str, int] = {}  # open tabs by id, counted in case one reconnects twice

    def claim(self, client: str) -> None:
        """Make `client` the one tab that listens and speaks; the others go quiet."""
        self.active = client
        self.publish({"type": "active", "client": client})

    def subscribe(self, client: str = "") -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
            if client:
                self.clients[client] = self.clients.get(client, 0) + 1
        return q

    def unsubscribe(self, q: queue.Queue, client: str = "") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)
            if client and client in self.clients:
                self.clients[client] -= 1
                if self.clients[client] <= 0:
                    del self.clients[client]

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
        self.said: list[str] = []  # this turn's sentences, for a phone alert if nobody is watching

    def feed(self, sentence: str) -> None:
        if not self._interrupted.is_set():
            self.said.append(sentence)
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
        self.phone = None  # jarvis.phone.Phone: lets a paired phone use Jarvis
        self.port = 0
        self.visible: dict[str, bool] = {}  # is each tab on screen? (reported by the page)
        self._asked = False  # the person asked something since the worker last looked
        self._worker = threading.Thread(target=self._work, daemon=True, name="jarvis-web")
        self._health: tuple[float, dict] | None = None

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

    @property
    def notifier(self):
        return getattr(self.bot, "notifier", None)

    def _on_error(self, exc: Exception) -> None:
        from jarvis.errors import explain

        self.hub.publish({"type": "error", "text": explain(exc), "detail": str(exc)[:300]})
        self.speaker.say(explain(exc))
        if self.notifier is not None:
            self.notifier.send("problems", explain(exc))

    def watching(self) -> bool:
        """Is the person looking at Jarvis right now, on the laptop or the phone?"""
        active = self.hub.active
        return bool(active) and active in self.hub.clients and self.visible.get(active, True)

    def set_visible(self, client: str, visible: bool) -> None:
        self.visible[client[:64]] = visible
        if len(self.visible) > 50:  # forget tabs that closed long ago
            for k in list(self.visible)[:-20]:
                self.visible.pop(k, None)

    def health(self, fresh: bool = False) -> dict:
        """Health checks for the dashboard, cached for a minute (they make network calls)."""
        from jarvis.doctor import run_checks, summary

        now = time.monotonic()
        if fresh or self._health is None or now - self._health[0] > 60:
            checks = run_checks(self.cfg)
            self._health = (now, {"summary": summary(checks), "checks": [c.to_dict() for c in checks]})
        return self._health[1]

    def _work(self) -> None:
        while True:
            self.bot.attention.wait()
            self.hub.publish({"type": "state", "state": "thinking"})
            self.bot.last_source = ""  # stays empty for announcements such as reminders
            asked, self._asked = self._asked, False
            self.speaker.said = []
            self.bot.drain_inbox()
            self.hub.publish({"type": "state", "state": "idle", "provider": self.bot.last_source})
            # You asked, then put the phone down or closed the tab: the answer follows you.
            if asked and self.speaker.said and self.notifier is not None and not self.watching():
                self.notifier.send("away", " ".join(self.speaker.said))

    # ---- actions from the browser ----------------------------------------

    def chat(self, text: str, audio_b64: str = "") -> None:
        text = text.strip()
        if not text:
            return
        if self.confirming.is_set():
            self.answers.put(text)  # "yes" or "no" to a pending question
            return
        self._asked = True
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
            "phone": bool(self.phone and self.phone.enabled),
            "notify": self.notifier.info() if self.notifier is not None else None,
        }

    # ---- phone and alerts (changed only from the laptop itself) ---------------

    def phone_info(self, refresh: bool = False) -> dict:
        if self.phone is None:
            return {"enabled": False, "available": False}
        info = self.phone.info(self.port, refresh)
        if self.notifier is not None:
            self.notifier.click_url = info["url"]
        return {"available": True, **info}

    def phone_settings(self, body: dict) -> dict:
        if self.phone is not None:
            if body.get("phone") is True:
                self.phone.turn_on()
            elif body.get("phone") is False:
                self.phone.turn_off()
            if body.get("reset_key"):
                self.phone.reset_key()
        n = self.notifier
        if n is not None:
            if body.get("alerts") is True:
                n.turn_on()
            elif body.get("alerts") is False:
                n.turn_off()
            if body.get("new_topic"):
                n.new_topic()
        return {"phone": self.phone_info(refresh=True), "notify": n.info() if n is not None else None}

    def test_alert(self) -> bool:
        n = self.notifier
        if n is None or not n.enabled:
            return False
        return n.send("status", "This is a test from Jarvis. Phone alerts are working.", title="Jarvis", force=True)

    def set_private(self, value: bool) -> None:
        if hasattr(self.llm, "private"):
            self.llm.private = value


LOGIN_PAGE = b"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jarvis</title>
<body style="background:#121211;color:#edebe5;font:16px/1.5 system-ui,sans-serif;padding:32px;max-width:520px">
<h1 style="font-size:22px">Pair this phone first</h1>
<p>On your laptop, open Jarvis, go to Settings, then Your phone, and scan the code with this phone's camera.</p>
</body></html>"""


def _loopback(addr: str) -> bool:
    import ipaddress

    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_loopback or bool(ip.ipv4_mapped and ip.ipv4_mapped.is_loopback)


def page() -> bytes:
    return resources.files("jarvis").joinpath("webui/index.html").read_bytes()


def make_handler(session: WebSession, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"Jarvis/{__version__}"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        # Only answer requests addressed to this computer, from this page. That
        # stops other websites from talking to Jarvis behind your back. A paired
        # phone is the one exception, and it must show the phone key.
        def _access(self) -> str:
            """local (this laptop's browser), phone (paired), login (needs the key) or forbidden."""
            from jarvis.phone import cookie_value

            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if origin is not None and urlsplit(origin).netloc.lower() != host.lower():
                return "forbidden"
            # Tailscale serve relays the phone from 127.0.0.1, so a relayed request is never "local".
            relayed = any(h.lower().startswith(("x-forwarded-", "tailscale-")) for h in self.headers.keys())
            if host in allowed_hosts and _loopback(self.client_address[0]) and not relayed:
                return "local"
            phone = session.phone
            if phone is None or not phone.host_allowed(host):
                return "forbidden"
            if phone.key_ok(cookie_value(self.headers.get("Cookie", ""))):
                return "phone"
            return "login"

        def _pair(self, key: str, access: str):
            """The phone opens /pair?key=... once (from the QR code) and keeps the key in a cookie."""
            from jarvis.phone import COOKIE

            phone = session.phone
            if access == "local" or phone is None or not phone.key_ok(key):
                return self._send(HTTPStatus.UNAUTHORIZED, LOGIN_PAGE, "text/html; charset=utf-8")
            secure = ".ts.net" in self.headers.get("Host", "") or self.headers.get("X-Forwarded-Proto") == "https"
            self.send_response(HTTPStatus.SEE_OTHER)
            # A year, so the installed app stays signed in. Resetting the key on the laptop signs phones out.
            self.send_header("Set-Cookie", f"{COOKIE}={key}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"
                                           + ("; Secure" if secure else ""))
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

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
            from jarvis.pwa import asset

            access = self._access()
            if access == "forbidden":
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            url = urlsplit(self.path)
            path = url.path
            app_file = asset(path)
            if app_file:  # icons and the manifest hold nothing private; phones fetch them without the key
                return self._send(200, *app_file)
            if path == "/pair":
                return self._pair(parse_qs(url.query).get("key", [""])[0], access)
            if access == "login":
                return self._send(HTTPStatus.UNAUTHORIZED, LOGIN_PAGE, "text/html; charset=utf-8")
            if path == "/":
                return self._send(200, page(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json({**session.status(), "remote": access == "phone"})
            if path == "/api/phone" and access == "local":  # it holds the key, so never shown on a phone
                return self._json(session.phone_info(refresh="fresh" in url.query))
            if path == "/api/telegram" and access == "local":  # holds the pairing code
                t = session.bot.telegram
                return self._json(t.status() if t else {"enabled": False})
            if path == "/api/reminders":
                return self._json({"reminders": session.reminders()})
            if path == "/api/today":
                return self._json(session.today())
            if path == "/api/system":
                from jarvis.vitals import vitals

                return self._json(vitals())
            if path == "/api/health":
                return self._json(session.health(fresh="fresh" in urlsplit(self.path).query))
            if path == "/api/events":
                return self._events()
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            access = self._access()
            if access == "forbidden":
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            if access == "login":
                return self._json({"error": "Scan the code on your laptop first."}, HTTPStatus.UNAUTHORIZED)
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
            elif path == "/api/presence":
                session.set_visible(str(body.get("client", "")), bool(body.get("visible", True)))
                return self._json({"ok": True})
            elif path == "/api/phone":
                if access != "local":
                    return self._json({"error": "Change phone settings on the laptop."}, HTTPStatus.FORBIDDEN)
                return self._json(session.phone_settings(body))
            elif path == "/api/telegram/unlink":
                if access != "local":
                    return self._json({"error": "Change phone settings on the laptop."}, HTTPStatus.FORBIDDEN)
                if session.bot.telegram:
                    session.bot.telegram.unlink()
                t = session.bot.telegram
                return self._json(t.status() if t else {"enabled": False})
            elif path == "/api/notify/test":
                return self._json({"ok": session.test_alert()})
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
            client = parse_qs(urlsplit(self.path).query).get("client", [""])[0][:64]
            q = session.hub.subscribe(client)
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
                session.hub.unsubscribe(q, client)

    return Handler


def serve(session: WebSession, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True):
    names = {host, "localhost", "127.0.0.1"} if host in {"127.0.0.1", "localhost", "0.0.0.0"} else {host}
    allowed: set[str] = set()
    httpd = ThreadingHTTPServer((host, port), make_handler(session, allowed))
    httpd.daemon_threads = True
    port = httpd.server_port  # the real port, in case 0 asked for any free one
    allowed.update(f"{n}:{port}" for n in names)
    session.port = port
    session.start()
    url = f"http://{'localhost' if host in {'127.0.0.1', '0.0.0.0'} else host}:{port}"
    print(f"  Jarvis is live at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    return httpd, url
