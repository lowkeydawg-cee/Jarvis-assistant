"""Phone alerts: Jarvis keeps you updated even when the page is closed.

Alerts go through ntfy (https://ntfy.sh), a free push service with apps for
Android and iPhone. No account or key: Jarvis makes a private, hard-to-guess
topic name, you subscribe to it once in the ntfy app, and every alert lands on
your phone's lock screen. Tapping one opens Jarvis.

What gets sent is up to you (notify.send in config.yaml):
  reminders  when a reminder fires
  alerts     watcher warnings: battery, disk, meetings, failed builds
  briefing   the morning briefing
  away       Jarvis's reply when you asked and then left the page
  problems   the cloud key was rejected, the brain is down, and so on
  status     Jarvis came online

Only the standard library is used. Sending happens on a background thread so
a slow network never holds up the conversation.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

KINDS = ("reminders", "alerts", "briefing", "away", "problems", "status")

# ntfy priorities: 5 urgent, 4 high, 3 default, 2 low. Reminders buzz; the rest arrive quietly.
PRIORITY = {"reminders": 4, "problems": 4, "alerts": 3, "away": 3, "briefing": 2, "status": 2}
TAGS = {"reminders": "alarm_clock", "alerts": "warning", "briefing": "sunrise", "away": "speech_balloon",
        "problems": "rotating_light", "status": "white_check_mark"}

SAME_TEXT_GAP = 600   # the same alert at most once every ten minutes
HOURLY_CAP = 30       # never more than this many alerts an hour


class Notifier:
    def __init__(self, cfg: dict, data_dir: str | Path, post: Callable | None = None,
                 clock: Callable[[], float] = time.time, background: bool = True):
        self.cfg = cfg
        self.server = str(cfg.get("server") or "https://ntfy.sh").rstrip("/")
        self.kinds = set(cfg.get("send") or KINDS)
        self.state_file = Path(data_dir).expanduser() / "notify.json"
        state = self._load()
        self.topic = str(cfg.get("topic") or state.get("topic") or "")
        # Config wins when it says yes; otherwise the switch in Settings decides.
        self.enabled = bool(cfg.get("enabled")) or bool(state.get("enabled"))
        if self.enabled and not self.topic:
            self.topic = new_topic()
            self._save()
        self.click_url = ""  # set to the phone address so tapping an alert opens Jarvis
        self._post = post or _http_post
        self._clock = clock
        self._background = background
        self._recent: dict[str, float] = {}
        self._sent_times: list[float] = []
        self._lock = threading.Lock()
        self.last_error = ""

    # ---- settings ----------------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"enabled": self.enabled, "topic": self.topic}), encoding="utf-8")

    def turn_on(self) -> None:
        self.enabled = True
        self.topic = self.topic or new_topic()
        self._save()

    def turn_off(self) -> None:
        self.enabled = False
        self._save()

    def new_topic(self) -> None:
        """Start over with a fresh topic, for example if the old name leaked."""
        self.topic = new_topic()
        self._save()

    @property
    def subscribe_url(self) -> str:
        return f"{self.server}/{self.topic}" if self.topic else ""

    def info(self) -> dict:
        return {"enabled": self.enabled, "server": self.server, "topic": self.topic,
                "subscribe_url": self.subscribe_url, "kinds": sorted(self.kinds), "last_error": self.last_error}

    # ---- sending -----------------------------------------------------------

    def wants(self, kind: str) -> bool:
        return self.enabled and bool(self.topic) and kind in self.kinds

    def send(self, kind: str, message: str, title: str = "", force: bool = False) -> bool:
        """Queue an alert. Returns False when it was skipped (off, not wanted, or a repeat)."""
        message = " ".join((message or "").split())
        if not message or not (self.enabled and self.topic) or not (force or kind in self.kinds):
            return False
        now = self._clock()
        with self._lock:
            key = f"{kind}:{message}"
            if not force and now - self._recent.get(key, -1e9) < SAME_TEXT_GAP:
                return False
            self._sent_times = [t for t in self._sent_times if now - t < 3600]
            if not force and len(self._sent_times) >= HOURLY_CAP:
                return False
            self._recent[key] = now
            self._sent_times.append(now)
        headers = {
            "Title": title or default_title(kind),
            "Priority": str(PRIORITY.get(kind, 3)),
            "Tags": TAGS.get(kind, "robot"),
        }
        if self.click_url:
            headers["Click"] = self.click_url
        job = (f"{self.server}/{self.topic}", message[:3000], headers)
        if self._background:
            threading.Thread(target=self._deliver, args=job, daemon=True, name="notify").start()
        else:
            self._deliver(*job)
        return True

    def _deliver(self, url: str, message: str, headers: dict) -> None:
        try:
            self._post(url, message, headers)
            self.last_error = ""
        except Exception as exc:  # an alert that can't be delivered must never break Jarvis
            self.last_error = str(exc)[:200]
            print(f"  [notify] couldn't reach {self.server}: {exc}")


def new_topic() -> str:
    return "jarvis-" + secrets.token_hex(8)


def default_title(kind: str) -> str:
    return {"reminders": "Reminder", "alerts": "Heads up", "briefing": "Your briefing", "away": "Jarvis replied",
            "problems": "Jarvis needs a hand", "status": "Jarvis"}.get(kind, "Jarvis")


def _http_post(url: str, message: str, headers: dict) -> None:
    # HTTP headers must be plain ASCII; ntfy reads RFC 2047 encoded titles for anything else.
    safe = {k: (v if v.isascii() else "=?UTF-8?B?" + _b64(v) + "?=") for k, v in headers.items()}
    req = urllib.request.Request(url, data=message.encode("utf-8"), headers=safe, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def _b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode("utf-8")).decode("ascii")
