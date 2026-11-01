"""Memory of past conversations: "what did we talk about yesterday about the job search?"

Every exchange is appended to a local journal (JSON lines, one per turn). Once a
day, each finished day gets a short summary written by the model. Questions about
earlier chats are answered from the summaries and, for details, from the journal
itself. Turns from voices Jarvis didn't recognise are never written down.

Everything stays in the data folder on this computer. Summaries are written by
whichever brain is active (the cloud unless Private mode is on).
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "at", "for", "is", "are", "was", "were", "i", "you",
        "my", "me", "it", "that", "this", "what", "about", "did", "we", "do", "talk", "talked", "say", "said", "with"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in STOP and len(w) > 1}


class Journal:
    def __init__(self, folder: str | Path, summarize: Callable[[str], str] | None = None,
                 today: Callable[[], date] = date.today):
        self.folder = Path(folder).expanduser()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.log_path = self.folder / "journal.jsonl"
        self.summary_path = self.folder / "journal_summaries.json"
        self.summarize = summarize
        self.today = today
        self.lock = threading.Lock()

    # ---- writing ---------------------------------------------------------------

    def add(self, said: str, reply: str, source: str = "", channel: str = "app") -> None:
        if not said.strip() or not reply.strip():
            return
        entry = {"at": datetime.now().isoformat(timespec="seconds"), "said": said.strip()[:2000],
                 "reply": reply.strip()[:2000], "source": source, "channel": channel}
        with self.lock, self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self, day: date | None = None) -> list[dict]:
        if not self.log_path.is_file():
            return []
        out = []
        with self.lock, self.log_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if day is None or e["at"][:10] == day.isoformat():
                    out.append(e)
        return out

    # ---- daily summaries -------------------------------------------------------

    def summaries(self) -> dict[str, str]:
        try:
            return json.loads(self.summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_summaries(self, data: dict[str, str]) -> None:
        tmp = self.summary_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.summary_path)

    def summarize_day(self, day: date) -> str:
        entries = [e for e in self.entries(day) if e.get("source") != "instant" or len(e["said"]) > 25]
        if not entries:
            return ""
        transcript = "\n".join(f"User: {e['said']}\nJarvis: {e['reply'][:400]}" for e in entries[-60:])
        text = ""
        if self.summarize is not None:
            try:
                text = self.summarize(
                    "Summarize this day's conversations between the user and their assistant Jarvis in at most five "
                    "short sentences. Keep names, decisions, plans, problems and anything to follow up. Plain text, no "
                    "lists.\n\n" + transcript)
            except Exception as exc:
                print(f"  [journal] couldn't summarize {day}: {exc}")
        if not text:  # no model available: keep the user's own words as a rough summary
            text = "You asked about: " + "; ".join(e["said"][:80] for e in entries[:8]) + "."
        data = self.summaries()
        data[day.isoformat()] = text.strip()
        self._save_summaries(data)
        return text

    def catch_up(self) -> int:
        """Summarize every finished day that has a journal but no summary yet."""
        done = self.summaries()
        days = sorted({e["at"][:10] for e in self.entries()} - set(done) - {self.today().isoformat()})
        for d in days:
            self.summarize_day(date.fromisoformat(d))
        return len(days)

    def start(self, every_hours: float = 3) -> None:
        def loop():
            while True:
                try:
                    self.catch_up()
                except Exception as exc:
                    print(f"  [journal] {exc}")
                time.sleep(every_hours * 3600)

        threading.Thread(target=loop, daemon=True, name="journal").start()

    # ---- recalling -------------------------------------------------------------

    def recall_day(self, day: date) -> str:
        label = {self.today(): "today", self.today() - timedelta(days=1): "yesterday"}.get(day, f"on {day:%A %d %B}")
        summary = self.summaries().get(day.isoformat())
        if summary:
            return f"{label[0].upper() + label[1:]}: {summary}"
        entries = self.entries(day)
        if not entries:
            return f"We didn't talk {label}."
        topics = "; ".join(e["said"][:70] for e in entries[-6:])
        return f"{label[0].upper() + label[1:]} you asked about: {topics}."

    def search(self, query: str, days: int = 60, limit: int = 6) -> str:
        """Past exchanges and day summaries that mention the query's words, newest first."""
        words = _words(query)
        if not words:
            return "Tell me what the conversation was about."
        since = (self.today() - timedelta(days=days)).isoformat()
        hits = []
        for d, s in self.summaries().items():
            score = len(words & _words(s))
            if d >= since and score:
                hits.append((score + 0.5, d, f"{d} summary: {s}"))
        for e in self.entries():
            score = len(words & _words(e["said"] + " " + e["reply"]))
            if e["at"][:10] >= since and score:
                hits.append((score, e["at"], f"{e['at'][:16].replace('T', ' ')} you said: {e['said'][:200]} | "
                                             f"Jarvis: {e['reply'][:300]}"))
        if not hits:
            return f"I don't remember us talking about {query}."
        best = sorted(hits, key=lambda h: (h[0], h[1]), reverse=True)[:limit]  # best match, then newest
        return "\n".join(h[2] for h in best)


def parse_day(word: str, today: date) -> date | None:
    word = word.strip().lower()
    if word in ("today", "this morning", "earlier", "earlier today"):
        return today
    if word in ("yesterday", "last night"):
        return today - timedelta(days=1)
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    w = word.replace("on ", "").replace("last ", "")
    if w in days:
        back = (today.weekday() - days.index(w)) % 7 or 7
        return today - timedelta(days=back)
    return None


def add_journal_routes(router, journal: Journal) -> None:
    def day_route(m):
        g = m.groupdict()
        day = parse_day(g.get("when") or "", journal.today())
        topic = (g.get("topic") or "").strip()
        if topic:
            return journal.search(topic, limit=3)
        return journal.recall_day(day) if day else None

    router.add("journal", r"what (did|have) (we|i) (talk|talked|chat|chatted|discuss|discussed) about"
                          r"(?P<topic> .+?)??( (?P<when>today|yesterday|last night|this morning|earlier( today)?|"
                          r"(on |last )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)))?",
               day_route)
    router.add("journal", r"(remind me )?what (did|have) i (say|tell you|ask) (you )?about (?P<topic>.+)",
               lambda m: journal.search(m.group("topic"), limit=3))


def journal_tool(journal: Journal):
    from jarvis.tools import Tool

    return Tool(
        name="recall_conversations",
        description="Search past conversations with the user (their words, your replies, daily summaries). Use when "
                    "they refer to something discussed before: 'like I told you', 'what did we decide about...', "
                    "'last week we talked about...'.",
        parameters={"type": "object", "properties": {"query": {"type": "string"},
                                                     "days": {"type": "integer", "description": "How far back, default 60."}},
                    "required": ["query"]},
        handler=lambda a: journal.search(str(a.get("query", "")), int(a.get("days") or 60)),
    )
