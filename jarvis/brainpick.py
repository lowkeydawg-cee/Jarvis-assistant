"""Choosing how to answer: the right brain per question, and no brain at all for repeats.

Difficulty: a short plain question doesn't need the biggest model. Easy questions go
to a small fast cloud model; anything needing specific tools, reasoning, code or a long prompt
goes to the big one. Groq limits each model separately, so this also spares the big
model's quota for when it matters.

Answer cache: a repeated general question is answered from a local file, with no
cloud call at all. Only answers that can't go stale or depend on you are kept: nothing
personal ("my", "I"), nothing about now ("today", "latest", "weather"), nothing that
leans on the conversation ("it", "that"), and only answers that used no tools.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

HARD_WORDS = re.compile(
    r"\b(why|how (do|does|can|should|would)|explain|compare|difference|analy[sz]e|debug|fix|plan|strategy|design|"
    r"write|draft|essay|code|function|script|algorithm|prove|calculate|step by step|pros and cons|should i|"
    r"recommend|review|translate|summari[sz]e)\b", re.I)
EASY_MAX_WORDS = 14

PERSONAL = re.compile(r"\b(i|i'm|i've|i'd|me|my|mine|we|our|us|you|your|jarvis)\b", re.I)
CONTEXTUAL = re.compile(r"\b(it|its|that|this|these|those|they|them|he|she|him|her|there|then|again|else|more|another|"
                        r"yes|no|ok|okay|continue|same)\b", re.I)
TIMELY = re.compile(r"\b(now|today|tonight|tomorrow|yesterday|week|month|year|latest|current|currently|recent|recently|"
                    r"news|weather|price|prices|stock|stocks|bitcoin|crypto|score|scores|time|date|live|update|updates|"
                    r"president|prime minister|ceo)\b", re.I)
COMMAND = re.compile(r"^\s*(open|close|launch|start|stop|turn|set|mute|unmute|lock|play|pause|resume|skip|shut|restart|"
                     r"take|search|go to|cancel|find|run|send|add|remove|delete)\b", re.I)
NO_ANSWER = re.compile(r"\b(don.?t have|do not have|can.?t|cannot|couldn.?t|unable|not sure|no access|sorry)\b", re.I)
STOP = {"the", "a", "an", "of", "is", "are", "was", "were", "what", "whats", "who", "whos", "please", "tell", "about", "in", "on"}


def _specific(tool: dict) -> bool:
    """Did this question trigger the tool? Tools with no group go out with every question, so they say nothing."""
    from jarvis.toolpick import group_of

    return group_of(str((tool.get("function") or {}).get("name", ""))) is not None


def difficulty(messages: list[dict], tools) -> str:
    """'easy' or 'hard', from the latest user message."""
    if any(_specific(t) for t in tools or []):
        return "hard"
    text = next((str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"), "")
    if len(text.split()) > EASY_MAX_WORDS or HARD_WORDS.search(text):
        return "hard"
    return "easy"


def cache_key(text: str) -> str:
    """The same question in different words and order gets the same key."""
    words = re.findall(r"[a-z0-9']+", text.lower())
    return " ".join(sorted(set(w for w in words if w not in STOP)))


def cacheable(text: str) -> bool:
    from jarvis.toolpick import wanted

    words = text.split()
    return (3 <= len(words) <= 25 and not (PERSONAL.search(text) or CONTEXTUAL.search(text) or TIMELY.search(text)
                                           or COMMAND.search(text))
            and not wanted(text))  # a question tools could answer ("the trading bot") isn't general knowledge


class AnswerCache:
    def __init__(self, path: str | Path, days: float = 7, limit: int = 500, clock=time.time):
        self.path = Path(path).expanduser()
        self.ttl = days * 86400
        self.limit = limit
        self.clock = clock
        self.lock = threading.Lock()
        try:
            self.items: dict[str, dict] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.items = {}

    def get(self, text: str) -> str | None:
        if not cacheable(text):
            return None
        with self.lock:
            hit = self.items.get(cache_key(text))
        if hit and self.clock() - hit["at"] < self.ttl and not NO_ANSWER.search(hit["reply"]):
            return hit["reply"]
        return None

    def put(self, text: str, reply: str) -> None:
        if not cacheable(text) or not reply.strip() or len(reply) > 1500 or NO_ANSWER.search(reply):
            return
        with self.lock:
            self.items[cache_key(text)] = {"reply": reply, "at": self.clock()}
            if len(self.items) > self.limit:  # drop the oldest
                for k in sorted(self.items, key=lambda k: self.items[k]["at"])[: len(self.items) - self.limit]:
                    del self.items[k]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.items, indent=0, ensure_ascii=False), encoding="utf-8")
            except OSError as exc:
                print(f"  [cache] couldn't save: {exc}")

    def clear(self) -> int:
        with self.lock:
            n = len(self.items)
            self.items = {}
            self.path.unlink(missing_ok=True)
        return n
