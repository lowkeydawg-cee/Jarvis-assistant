"""Instant answers for simple requests, so they never wait on a language model.

On a slow laptop the model takes over a minute just to read its instructions,
and even the cloud adds a second. "What time is it" deserves neither. The
router matches a handful of everyday requests exactly and answers them in
code. Anything it isn't sure about goes to the model as usual, so a false
match costs nothing worse than a normal reply.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from typing import Callable

_FILLER = re.compile(
    r"\b(hey|hi|ok|okay|yo|jarvis|please|can you|could you|tell me|quickly|real quick|right now|now|for me)\b"
)
_PUNCT = re.compile(r"[^\w\s]")  # "what's" becomes "what s"


def normalize(text: str) -> str:
    text = _PUNCT.sub(" ", text.lower())
    text = _FILLER.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _spoken_time(now: datetime) -> str:
    hour = now.hour % 12 or 12
    suffix = "in the morning" if now.hour < 12 else "in the afternoon" if now.hour < 18 else "in the evening"
    if now.minute == 0:
        return f"It's {hour} o'clock {suffix}."
    minute = f"oh {now.minute}" if now.minute < 10 else str(now.minute)
    return f"It's {hour} {minute} {suffix}."


def _spoken_date(now: datetime) -> str:
    return f"It's {now:%A}, the {_ordinal(now.day)} of {now:%B}."


def _disk(path: str | None = None) -> str:
    total, _, free = shutil.disk_usage(path or os.path.expanduser("~"))
    gb = 1024 ** 3
    pct = round(100 * free / total)
    tone = " That's getting tight." if free / gb < 10 else ""
    return f"You've got about {free / gb:.0f} gigabytes free out of {total / gb:.0f}, so {pct} percent.{tone}"


def _battery() -> str:
    try:
        import psutil
    except ImportError:
        return "I can't read the battery yet. Install the proactive extras to enable that."
    b = psutil.sensors_battery()
    if b is None:
        return "I don't see a battery on this machine."
    state = "and charging" if b.power_plugged else "on battery"
    return f"You're at {b.percent:.0f} percent, {state}."


_HELP = (
    "I can answer questions, check your disk and battery, search your notes, look at your screen, "
    "run safe commands after you say yes, and brief you on your day. Just ask."
)

# (pattern on the normalized text, answer). Patterns are anchored so only short,
# unambiguous requests match: "what time is the meeting" still goes to the model.
_TIME = r"(what s|what is|whats)? ?(the )?time( is it)?|what time is it|got the time|time check"
_DATE = (r"(what s|what is|whats) (the date|today s date|todays date)( today)?|what day is (it|today)"
         r"|what s today|what is today|today s date|the date")
_DISK = (r"(how much )?(free )?(disk|storage|drive) ?(space)?( do i have| is left| left| free)?"
         r"|how s my (disk|storage|drive)|how is my (disk|storage|drive)|how much space (do i have|is left)( left)?|free space")
_BATTERY = (r"(how s|how is|what s|what is|check) (my |the )?battery( level| life| status)?|battery( level| status| life)?"
            r"|how much battery (do i have|is left)( left)?")
_HELP_RE = r"what can you do|help|what are you able to do"


class QuickRouter:
    def __init__(self, clock: Callable[[], datetime] = datetime.now, disk: Callable[[], str] = _disk,
                 battery: Callable[[], str] = _battery):
        self.routes: list[tuple[str, re.Pattern, Callable[[], str]]] = [
            ("time", re.compile(rf"^(?:{_TIME})$"), lambda: _spoken_time(clock())),
            ("date", re.compile(rf"^(?:{_DATE})$"), lambda: _spoken_date(clock())),
            ("disk", re.compile(rf"^(?:{_DISK})$"), disk),
            ("battery", re.compile(rf"^(?:{_BATTERY})$"), battery),
            ("help", re.compile(rf"^(?:{_HELP_RE})$"), lambda: _HELP),
        ]

    def match(self, text: str) -> tuple[str, Callable[[], str]] | None:
        norm = normalize(text)
        if not norm or len(norm.split()) > 8:
            return None
        for name, pattern, answer in self.routes:
            if pattern.match(norm):
                return name, answer
        return None

    def answer(self, text: str) -> str | None:
        """The instant answer, or None to let the model handle it."""
        hit = self.match(text)
        if hit is None:
            return None
        try:
            return hit[1]()
        except Exception:
            return None  # a broken quick answer should fall through to the model
