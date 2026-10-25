"""Reminders and a simple schedule: "remind me to call mum at 5", "every weekday at 8".

Everything here works without the language model, so it's instant even on a slow
laptop: a small parser understands the ways people say times out loud, a JSON
file keeps the reminders across restarts, and a background thread fires them.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WORD_NUMS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60, "a couple of": 2, "a few": 3,
}
DEFAULT_HOUR = {"morning": 9, "afternoon": 14, "evening": 18, "tonight": 20, "night": 20}

_NUM = r"(\d+|a couple of|a few|forty five|" + "|".join(sorted(WORD_NUMS, key=len, reverse=True)) + r")"
_CLOCK = r"(\d{1,2})(?::(\d{2}))?(?:\s*(am|pm))?|noon|midday|midnight"
_DAY = r"today|tonight|tomorrow|(?:on |next |this )?(?:" + "|".join(DAYS) + r")"
_PART = r"(?:in the |this )?(?:morning|afternoon|evening)"

# Ordered: longer, more specific forms first. Each must be found as whole words.
_WHEN = [
    ("relative", rf"in (?:(half an hour)|{_NUM} (minute|minutes|min|mins|hour|hours|hr|hrs|day|days))"),
    ("repeat", rf"every (day|morning|evening|weekday|{'|'.join(DAYS)})(?: (?:at|by) (?:{_CLOCK}))?"),
    ("repeat_daily", rf"daily(?: (?:at|by) (?:{_CLOCK}))?"),
    ("day_clock", rf"({_DAY})(?: ({_PART}))?(?: (?:at|by|around) (?:{_CLOCK}))?"),
    ("clock_day", rf"(?:at|by|around) (?:{_CLOCK})(?: ({_DAY}))?"),
    ("part", rf"({_PART})"),
]


@dataclass
class Reminder:
    text: str
    due: str  # ISO local time
    repeat: str = ""  # "", "daily", "weekdays", or a day name for weekly
    kind: str = "reminder"  # reminder or event
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.due)


# ---- parsing ---------------------------------------------------------------


def _num(word: str) -> int:
    return int(word) if word.isdigit() else WORD_NUMS[word]


def _clock(h: str | None, m: str | None, ampm: str | None, word: str, now: datetime,
           day: datetime | None) -> datetime:
    """Turn a spoken clock time into a datetime on `day`, or the next sensible time from now."""
    if word in ("noon", "midday"):
        h, m, ampm = "12", "0", "pm"
    elif word == "midnight":
        h, m, ampm = "12", "0", "am"
    hour, minute = int(h), int(m or 0)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    base = (day or now).replace(second=0, microsecond=0)
    if ampm or hour >= 13 or hour == 0:
        options = [base.replace(hour=hour % 24, minute=minute)]
    else:  # "at 5": 5 am or 5 pm, whichever comes next
        options = [base.replace(hour=hour, minute=minute), base.replace(hour=(hour + 12) % 24, minute=minute)]
        if day is not None and day.date() != now.date():
            # On another day, "at 5" with no am or pm: 7 to 11 are mornings, 1 to 6 afternoons.
            return options[0] if hour >= 7 else options[1]
    future = [o for o in options if o > now]
    if future:
        return min(future)
    return options[0] + timedelta(days=1)


def _day(word: str, now: datetime) -> datetime:
    word = re.sub(r"^(on|next|this) ", "", word.strip())
    if word in ("today", "tonight"):
        return now
    if word == "tomorrow":
        return now + timedelta(days=1)
    ahead = (DAYS.index(word) - now.weekday()) % 7 or 7  # "on Monday" said on a Monday means next week
    return now + timedelta(days=ahead)


def _part_hour(part: str | None) -> int | None:
    for key, hour in DEFAULT_HOUR.items():
        if part and key in part:
            return hour
    return None


def _named_clock(text: str) -> str:
    return next((w for w in ("noon", "midday", "midnight") if re.search(rf"\b{w}\b", text)), "")


def _next_valid(due: datetime, repeat: str, now: datetime) -> datetime:
    """Move a reminder forward to its next allowed time after now (weekdays only, or one weekday)."""
    while (due <= now or repeat == "weekdays" and due.weekday() >= 5
           or repeat in DAYS and due.weekday() != DAYS.index(repeat)):
        due += timedelta(days=1)
    return due


def _resolve(kind: str, m: re.Match, now: datetime) -> tuple[datetime, str]:
    g, said = m.groups(), m.group(0)
    named = _named_clock(said)
    if kind == "relative":
        if g[0]:
            return now.replace(second=0, microsecond=0) + timedelta(minutes=30), ""
        n, unit = _num(g[1]), g[2]
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit[0]]
        return (now + delta).replace(second=0, microsecond=0), ""
    if kind in ("repeat", "repeat_daily"):
        what = g[0] if kind == "repeat" else "day"
        h, mi, ap = g[1:4] if kind == "repeat" else g[0:3]
        repeat = {"day": "daily", "morning": "daily", "evening": "daily", "weekday": "weekdays"}.get(what, what)
        if h or named:
            # Read the time as on another day, so "every weekday at 8" means 8 AM even in the afternoon.
            t = _clock(h, mi, ap, named, now, now + timedelta(days=1))
            hour, minute = t.hour, t.minute
        else:
            hour, minute = DEFAULT_HOUR.get(what, 9), 0
        return _next_valid(now.replace(hour=hour, minute=minute, second=0, microsecond=0), repeat, now), repeat
    if kind == "day_clock":
        day_word, part, h, mi, ap = g[0], g[1], g[2], g[3], g[4]
        day = _day(day_word, now)
        if h or named:
            if not ap and h and int(h) < 12 and ((_part_hour(part) or 0) >= 12 or day_word == "tonight"):
                ap = "pm"
            return _clock(h, mi, ap, named, now, day), ""
        hour = _part_hour(part) or (20 if day_word == "tonight" else 9)
        due = day.replace(hour=hour, minute=0, second=0, microsecond=0)
        if due <= now:  # "today" with the default hour already gone: soon instead
            due = now.replace(second=0, microsecond=0) + timedelta(hours=1)
        return due, ""
    if kind == "clock_day":
        h, mi, ap, day_word = g[0], g[1], g[2], g[3]
        if not ap and day_word == "tonight" and h and int(h) < 12:
            ap = "pm"
        return _clock(h, mi, ap, named, now, _day(day_word, now) if day_word else None), ""
    hour = _part_hour(g[0]) or 9  # part of the day on its own: "this evening"
    due = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return (due if due > now else due + timedelta(days=1)), ""


def find_when(text: str, now: datetime) -> tuple[datetime, str, tuple[int, int]] | None:
    """Find a spoken time in `text`. Returns (due, repeat, span of the matched words).

    Every pattern is tried and the longest match wins, so "at 5 pm tomorrow" is read
    as one time rather than "tomorrow" alone.
    """
    t = text.lower()
    best = None
    for kind, pattern in _WHEN:
        for m in re.finditer(rf"\b(?:{pattern})\b", t):
            if best is None or m.end() - m.start() > best[1].end() - best[1].start():
                best = (kind, m)
    if best is None:
        return None
    due, repeat = _resolve(best[0], best[1], now)
    return due, repeat, best[1].span()


_LEAD = re.compile(
    r"^(?:please )?(?:can you |could you |will you )?(?:remind me|set (?:a |an )?(?:reminder|alarm)|"
    r"add (?:a )?reminder|make (?:a )?reminder|schedule|add|put|book)\b\s*", re.I)
_FILL = re.compile(r"^(?:(?:to|that|about|for|me|i need to|i have to|i must)\s+)+|\s+(?:to my (?:schedule|calendar))$", re.I)


def clean_what(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip(" ,.?!")
    t = _FILL.sub("", t).strip(" ,.?!")
    t = re.sub(r"\s+(?:(?:to|on|in) my (?:schedule|calendar)|by|on|at|for)$", "", t, flags=re.I)
    return t[:1].upper() + t[1:] if t else t


def parse_reminder(text: str, now: datetime | None = None) -> tuple[str, datetime | None, str]:
    """'remind me to call mum at 5 pm' -> ('Call mum', <today 17:00>, '')."""
    now = now or datetime.now()
    t = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", text, flags=re.I)  # "5 p.m." from speech recognition
    t = re.sub(r"(\d)(am|pm)\b", r"\1 \2", t, flags=re.I)
    t = _LEAD.sub("", t.strip())
    found = find_when(t, now)
    if not found:
        return clean_what(t), None, ""
    due, repeat, (a, b) = found
    what = clean_what((t[:a] + " " + t[b:]))
    return what, due, repeat


# ---- speaking times ---------------------------------------------------------


def clock_text(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h} {'AM' if dt.hour < 12 else 'PM'}" if dt.minute == 0 else f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def when_text(dt: datetime, now: datetime | None = None, repeat: str = "") -> str:
    now = now or datetime.now()
    if repeat:
        label = {"daily": "every day", "weekdays": "every weekday"}.get(repeat, f"every {repeat.title()}")
        return f"{label} at {clock_text(dt)}"
    secs = (dt - now).total_seconds()
    mins = max(1, round(secs / 60))
    if 0 < secs and mins < 60:
        return f"in {mins} minute{'s' if mins != 1 else ''}"
    if dt.date() == now.date():
        return f"at {clock_text(dt)}"
    if dt.date() == (now + timedelta(days=1)).date():
        return f"tomorrow at {clock_text(dt)}"
    if (dt.date() - now.date()).days < 7:
        return f"on {dt:%A} at {clock_text(dt)}"
    return f"on {dt:%A} {dt.day} {dt:%B} at {clock_text(dt)}"


# ---- storage and firing -----------------------------------------------------


class ReminderStore:
    """Reminders kept in a small JSON file, safe to use from several threads."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.lock = threading.Lock()
        self.items: list[Reminder] = []
        self.on_change: Callable[[], None] | None = None  # e.g. refresh the list in the browser
        if self.path.is_file():
            try:
                self.items = [Reminder(**r) for r in json.loads(self.path.read_text(encoding="utf-8"))]
            except (ValueError, TypeError):
                self.items = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(r) for r in self.items], indent=1), encoding="utf-8")
        tmp.replace(self.path)
        if self.on_change:
            self.on_change()

    def add(self, text: str, due: datetime, repeat: str = "", kind: str = "reminder") -> Reminder:
        r = Reminder(text=text, due=due.isoformat(timespec="minutes"), repeat=repeat, kind=kind)
        with self.lock:
            self.items.append(r)
            self._save()
        return r

    def upcoming(self, until: datetime | None = None) -> list[Reminder]:
        with self.lock:
            items = sorted(self.items, key=lambda r: r.due)
        return [r for r in items if until is None or r.when <= until]

    def remove(self, reminder_id: str) -> Reminder | None:
        with self.lock:
            for r in self.items:
                if r.id == reminder_id:
                    self.items.remove(r)
                    self._save()
                    return r
        return None

    def find(self, query: str) -> list[Reminder]:
        """Reminders whose text shares the most words with `query`."""
        words = set(re.findall(r"[a-z0-9]+", query.lower())) - {"the", "a", "my", "to", "about", "reminder", "for"}
        scored = []
        for r in self.upcoming():
            overlap = len(words & set(re.findall(r"[a-z0-9]+", r.text.lower())))
            if overlap:
                scored.append((overlap, r))
        best = max((s for s, _ in scored), default=0)
        return [r for s, r in scored if s == best]

    def due_now(self, now: datetime) -> list[Reminder]:
        """Pop reminders that are due; repeating ones move to their next time."""
        fired = []
        with self.lock:
            for r in list(self.items):
                if r.when > now:
                    continue
                fired.append(Reminder(**asdict(r)))
                if r.repeat:
                    step = timedelta(days=7) if r.repeat in DAYS else timedelta(days=1)
                    nxt = r.when
                    while nxt <= now:
                        nxt += step
                    r.due = _next_valid(nxt, r.repeat, now).isoformat(timespec="minutes")
                else:
                    self.items.remove(r)
            if fired:
                self._save()
        return fired


class ReminderService:
    """Fires due reminders through `announce`, and remembers the last one for snoozing."""

    def __init__(self, store: ReminderStore, announce: Callable[[Reminder, bool], None],
                 now: Callable[[], datetime] = datetime.now, interval: float = 5):
        self.store = store
        self.announce = announce
        self.now = now
        self.interval = interval
        self.last_fired: Reminder | None = None
        self.started = now()

    def tick(self) -> None:
        now = self.now()
        for r in self.store.due_now(now):
            late = (now - r.when) > timedelta(minutes=2)
            self.last_fired = r
            self.announce(r, late)

    def start(self) -> None:
        def loop():
            while True:
                try:
                    self.tick()
                except Exception as exc:
                    print(f"  [reminders] {exc}")
                time.sleep(self.interval)

        threading.Thread(target=loop, daemon=True, name="reminders").start()


def announcement(r: Reminder, late: bool) -> str:
    what = r.text[:1].lower() + r.text[1:] if r.text else "something"
    if r.kind == "event":
        return f"Heads up: {what} is starting now." if not late else f"You had {what} at {clock_text(r.when)}."
    if late:
        return f"While I was off, you asked me to remind you to {what}, at {clock_text(r.when)}."
    return f"Reminder: {what}."
