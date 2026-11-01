"""Your real calendar in Jarvis, without giving it your Google password.

Reading: Google Calendar (and Outlook, iCloud) give every calendar a private
"secret address in iCal format". Paste it once in Settings and Jarvis reads your
events, including repeating ones, for "what's on today", the briefing and the
"meeting in 10 minutes" alert. The address only allows reading.

Adding: "put dentist on my Google Calendar Friday at 3" opens Google Calendar's
own new-event page, filled in, and you press Save. Jarvis never changes your
calendar by itself.
"""

from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

CACHE_SECONDS = 600


class CalendarFeed:
    def __init__(self, settings_path: str | Path, default_source: str = "", fetch=None):
        self.settings_path = Path(settings_path).expanduser()
        self.default_source = default_source
        self._fetch = fetch
        self._cache: tuple[float, str, bytes] | None = None
        self._lock = threading.Lock()
        self.last_error = ""

    # ---- where the calendar comes from ---------------------------------------

    @property
    def source(self) -> str:
        try:
            saved = json.loads(self.settings_path.read_text(encoding="utf-8")).get("source", "")
        except (OSError, ValueError):
            saved = ""
        return saved or self.default_source

    def set_source(self, source: str) -> int:
        """Save the calendar address after checking it works. Returns how many events it holds."""
        source = source.strip()
        if source.startswith("webcal://"):
            source = "https://" + source[len("webcal://"):]
        old = self.source
        self._write(source)
        self._cache = None
        try:
            count = len(self._calendar().walk("VEVENT")) if source else 0
        except Exception as exc:
            self._write(old)
            raise ValueError(f"That calendar address didn't work: {exc}") from exc
        return count

    def _write(self, source: str) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps({"source": source}), encoding="utf-8")

    # ---- reading -------------------------------------------------------------

    def _raw(self) -> bytes:
        src = self.source
        if not src:
            return b""
        with self._lock:
            if self._cache and self._cache[1] == src and time.monotonic() - self._cache[0] < CACHE_SECONDS:
                return self._cache[2]
        if src.startswith(("http://", "https://")):
            if self._fetch is not None:
                data = self._fetch(src)
            else:
                import requests

                resp = requests.get(src, timeout=15, headers={"User-Agent": "Jarvis calendar"})
                resp.raise_for_status()
                data = resp.content
        else:
            path = Path(os.path.expanduser(src))
            data = path.read_bytes() if path.is_file() else b""
        with self._lock:
            self._cache = (time.monotonic(), src, data)
        return data

    def _calendar(self):
        from icalendar import Calendar

        raw = self._raw()
        return Calendar.from_ical(raw) if raw else Calendar()

    def events(self, day: date) -> list[tuple[datetime, str]]:
        """(start, title) for every event on `day`, repeating ones included, in local time, sorted."""
        try:
            cal = self._calendar()
        except Exception as exc:
            self.last_error = str(exc)[:200]
            print(f"  [calendar] couldn't read the calendar: {exc}")
            return []
        self.last_error = ""
        try:
            import recurring_ical_events

            items = recurring_ical_events.of(cal).at(day)
        except ImportError:  # without the helper, one-off events still work
            items = cal.walk("VEVENT")
        out = []
        for ev in items:
            start = ev.get("DTSTART")
            if start is None:
                continue
            start = start.dt
            if not isinstance(start, datetime):  # all-day event: show it at midnight
                start = datetime.combine(start, datetime.min.time())
            if start.tzinfo:
                start = start.astimezone().replace(tzinfo=None)
            if start.date() == day:
                out.append((start, str(ev.get("SUMMARY", "an event"))))
        return sorted(out)

    def status(self) -> dict:
        src = self.source
        info = {"connected": bool(src), "kind": "link" if src.startswith("http") else "file" if src else ""}
        if src:
            today = date.today()
            info["today"] = len(self.events(today))
            info["week"] = sum(len(self.events(today + timedelta(days=i))) for i in range(7))
            info["error"] = self.last_error
        return info


# ---- adding an event through Google Calendar's own page ---------------------------


def google_event_url(title: str, start: datetime, minutes: int = 60, details: str = "") -> str:
    end = start + timedelta(minutes=minutes)
    fmt = "%Y%m%dT%H%M%S"  # no Z: Google reads it in your calendar's own time zone
    url = (f"https://calendar.google.com/calendar/render?action=TEMPLATE&text={quote(title)}"
           f"&dates={start.strftime(fmt)}/{end.strftime(fmt)}")
    return url + (f"&details={quote(details)}" if details else "")


def add_to_google(text: str, open_url=webbrowser.open, now: datetime | None = None) -> str:
    """'dentist Friday at 3' -> open a filled-in Google Calendar event for the user to save."""
    from jarvis.reminders import clean_what, find_when, when_text

    now = now or datetime.now()
    found = find_when(text, now)
    if not found:
        return "When is it? Say something like: put dentist on my Google Calendar Friday at 3."
    due, _repeat, (a, b) = found
    import re

    title = re.sub(r"(?i)^(the|a|an|my)\s+", "", clean_what(text[:a] + " " + text[b:]))  # "the team dinner"
    title = (title[:1].upper() + title[1:]) if title else "Event"
    open_url(google_event_url(title, due))
    return f"I've opened Google Calendar with {title} {when_text(due, now)}. Press Save to add it."


def calendar_routes(router, feed: CalendarFeed, open_url=webbrowser.open) -> None:
    import re

    def add(m, raw):
        t = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", raw, flags=re.I)
        t = re.sub(r"(?i)^\s*(please )?(can you |could you )?(add|put|schedule|create)\s+", "", t)
        t = re.sub(r"(?i)\s+(on|in|to|into)\s+(my\s+)?(google\s+)?calendar\b", " ", t)
        return add_to_google(t.strip(), open_url)

    router.add("calendar", r"(please )?(can you |could you )?(add|put|schedule|create) .+ (on|in|to|into) (my )?"
                           r"(google )?calendar( .+)?", add, raw=True)


def calendar_tool(feed: CalendarFeed, open_url=webbrowser.open):
    from jarvis.tools import Tool

    def events(a):
        day = date.today() + timedelta(days=int(a.get("days_from_today") or 0))
        evs = feed.events(day)
        if not feed.source:
            return "No calendar is connected yet. Settings > Your calendar."
        return "\n".join(f"{s:%H:%M} {t}" for s, t in evs) or f"No events on {day:%A %d %B}."

    return [
        Tool(name="calendar_events", description="Events in the user's real calendar on a day "
             "(0 today, 1 tomorrow, and so on). Use for meetings, classes and appointments.",
             parameters={"type": "object", "properties": {"days_from_today": {"type": "integer"}}},
             handler=events),
        Tool(name="add_to_google_calendar", description="Open a filled-in Google Calendar event for the user to "
             "save. 'what' is the title, 'when' in plain words like 'Friday at 3 pm'.",
             parameters={"type": "object", "properties": {"what": {"type": "string"}, "when": {"type": "string"}},
                         "required": ["what", "when"]},
             handler=lambda a: add_to_google(f"{a.get('what', '')} {a.get('when', '')}", open_url)),
    ]
