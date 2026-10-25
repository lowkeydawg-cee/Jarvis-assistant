"""Instant answers: common requests handled in code, before the language model.

On a laptop without a GPU the model needs a minute or more per reply, most of it
spent reading its long prompt. Asking the time shouldn't cost that. The router
matches short, plain requests and answers them directly in well under a second.
Anything it doesn't recognise, or any handler that returns None, goes to the
model exactly as before.

Patterns match the whole (normalised) request, so "what time is it" is handled
here but "what time is it in Tokyo" still goes to the model.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

Handler = Callable[[re.Match], "str | None"]

_FILLER = re.compile(
    r"^(hey |ok |okay |yo |so |jarvis |please |can you |could you |would you |tell me |quickly )+"
)


def normalise(text: str) -> str:
    """Lowercase, drop apostrophes and punctuation, and strip polite openers."""
    t = text.lower().replace("'", "").replace("’", "")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = _FILLER.sub("", t + " ").strip()
    return re.sub(r"( please| for me| now| right now| jarvis)+$", "", t).strip()


@dataclass
class Route:
    name: str
    pattern: re.Pattern
    handler: Handler


class Router:
    def __init__(self, routes: bool = True):
        self.routes: list[Route] = []
        if routes:
            add_builtin_routes(self)

    def add(self, name: str, pattern: str, handler: Handler) -> None:
        """Register a route. `pattern` must match the entire normalised request."""
        self.routes.append(Route(name, re.compile(rf"^(?:{pattern})$"), handler))

    def answer(self, text: str) -> tuple[str, str] | None:
        """Return (route name, spoken reply), or None to let the model answer."""
        t = normalise(text)
        if not t:
            return None
        for route in self.routes:
            m = route.pattern.match(t)
            if not m:
                continue
            try:
                reply = route.handler(m)
            except Exception as exc:  # a broken shortcut must never break the conversation
                print(f"  [instant] {route.name} failed, asking the model instead: {exc}")
                return None
            if reply:
                return route.name, reply
        return None


# ---- built-in routes ------------------------------------------------------


def _time(now: Callable[[], datetime]) -> Handler:
    def handler(m):
        n = now()
        return f"It's {n.hour % 12 or 12}:{n.minute:02d} {'AM' if n.hour < 12 else 'PM'}."
    return handler


def _date(now: Callable[[], datetime]) -> Handler:
    def handler(m):
        n = now()
        return f"It's {n:%A}, {n.day} {n:%B}."
    return handler


def _battery(read: Callable[[], tuple[float, bool] | None]) -> Handler:
    def handler(m):
        state = read()
        if state is None:
            return "I can't find a battery on this computer."
        percent, plugged = state
        return f"Your battery is at {percent:.0f} percent{' and charging' if plugged else ''}."
    return handler


def _read_battery():
    try:
        import psutil
    except ImportError:
        return None
    b = psutil.sensors_battery()
    return (b.percent, b.power_plugged) if b else None


def _disk(usage: Callable[[str], object]) -> Handler:
    def handler(m):
        free = usage(os.path.expanduser("~")).free / 1024 ** 3
        return f"You have {free:.0f} gigabytes free on your main drive."
    return handler


HELP = (
    "I can tell you the time, the date, your battery and disk space, give you a briefing, "
    "and search your notes. For anything else, just ask and I'll think it through."
)


def add_builtin_routes(router: Router, now: Callable[[], datetime] = datetime.now,
                       battery=_read_battery, disk_usage=shutil.disk_usage) -> None:
    router.add("time", r"((whats|what is) )?the time|what time (is it|it is)|time( is it)?", _time(now))
    router.add("date", r"(whats|what is) (the date|todays date)( today)?|what day is (it|today)|whats today|"
                       r"what is today|(the |todays )?date", _date(now))
    router.add("battery", r"((hows|how is|whats|what is|check) )?(my |the )?battery( level| life| status| percentage)?"
                          r"( like| at)?", _battery(battery))
    router.add("disk", r"((hows|how is|whats|what is|check|how much) )?(my |the )?(free )?(disk|storage|drive)"
                       r"( space)?( free| left| like)?( do i have)?( left)?", _disk(disk_usage))
    router.add("help", r"help|what can you do|what are you able to do", lambda m: HELP)


# ---- routes that need other parts of Jarvis -------------------------------


def briefing_route(facts: Callable[[], str]) -> Handler:
    """Speak the briefing facts directly instead of having the model reword them."""
    def handler(m):
        lines = [ln.strip().rstrip(".") for ln in facts().splitlines() if ln.strip()]
        return ". ".join(lines) + "." if lines else None
    return handler


BRIEFING_PATTERN = r"brief me|(give me )?(my |the |todays |a )?(morning )?briefing|whats my day( look)?( like)?|" \
                   r"what does my day look like"


def notes_route(search: Callable[[str, int], list], min_score: float = 0.3, max_chars: int = 220) -> Handler:
    """Read out the best matching note. Returns None (ask the model) when nothing fits."""
    def handler(m):
        groups = m.groupdict()
        query = (groups.get("q") or groups.get("q2") or "").strip()
        if not query:
            return None
        hits = [h for h in search(query, 1) if h[0] >= min_score]
        if not hits:
            return f"I couldn't find anything about {query} in your notes."
        _, path, text = hits[0]
        from jarvis.persona import speakable

        snippet = re.sub(r"\s+", " ", speakable(text)).strip()
        if len(snippet) > max_chars:
            snippet = snippet[:max_chars].rsplit(" ", 1)[0] + "..."
        return f"The best match is in {Path(path).name}. It says: {snippet}"
    return handler


NOTES_PATTERN = (
    r"(search|check|look (in|through)|find in) my notes (for|about|on) (?P<q>.+)|"
    r"where did i (write|save|put|note|keep) ((down|about|on|the|my) )*(?P<q2>.+)"
)


def add_notes_route(router: Router, search: Callable[[str, int], list]) -> None:
    router.add("notes", NOTES_PATTERN, notes_route(search))
