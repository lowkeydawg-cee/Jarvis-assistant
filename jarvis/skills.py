"""Reminders, schedule and memory, wired into Jarvis two ways.

1. Instant routes for the common phrasings ("remind me to call mum at 5",
   "what's on my schedule today", "remember that...") so they work in well under
   a second, without the model.
2. Tools for the model, so unusual phrasings still work when a model is there
   to interpret them ("ping me about the oven after lunch").
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from jarvis.memory import Memory, second_person
from jarvis.reminders import (
    DAYS, ReminderService, ReminderStore, clock_text, find_when, parse_reminder, when_text,
)
from jarvis.router import Router
from jarvis.tools import Tool

PENDING_SECONDS = 120
_NUMS = {"five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "an": 60, "a": 60}


def _confirm(what: str, due: datetime, repeat: str, kind: str, now: datetime) -> str:
    what = what[:1].lower() + what[1:]
    if kind == "event":
        return f"Done. {what[:1].upper() + what[1:]}, {when_text(due, now, repeat)}."
    return f"Okay, I'll remind you to {second_person(what)} {when_text(due, now, repeat)}."


def agenda(store: ReminderStore, day: datetime, label: str, now: datetime) -> str:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    items = [r for r in store.upcoming() if start <= r.when < start + timedelta(days=1)]
    if not items:
        return f"Nothing on your schedule {label}."
    parts = [f"at {clock_text(r.when)}, {r.text[:1].lower() + r.text[1:]}" for r in items[:6]]
    head = f"You have {len(items)} thing{'s' if len(items) != 1 else ''} {label}"
    return f"{head}: " + "; ".join(parts) + "."


class Skills:
    def __init__(self, store: ReminderStore, memory: Memory, service: ReminderService | None = None,
                 now=datetime.now):
        self.store = store
        self.memory = memory
        self.service = service
        self.now = now
        self.pending: tuple[str, str, float] | None = None  # (what, kind, asked at)

    # ---- actions shared by routes and tools ------------------------------

    def add(self, text: str) -> str:
        now = self.now()
        kind = "event" if re.match(r"^\s*(?:please )?(schedule|add|put)\b", text, re.I) else "reminder"
        what, due, repeat = parse_reminder(text, now)
        if not what:
            return "What should I remind you about?"
        if due is None:
            self.pending = (what, kind, time.monotonic())
            return f"Sure. When should I remind you to {second_person(what[:1].lower() + what[1:])}?"
        self.store.add(what, due, repeat, kind)
        self.pending = None
        return _confirm(what, due, repeat, kind, now)

    def complete(self, text: str) -> str | None:
        """Answer to "when should I remind you?" ("in ten minutes")."""
        if not self.pending or time.monotonic() - self.pending[2] > PENDING_SECONDS:
            return None
        now = self.now()
        found = find_when(re.sub(r"\b([ap])\.\s?m\.?", r"\1m", text, flags=re.I), now)
        if not found:
            return None
        what, kind, _ = self.pending
        due, repeat, _ = found
        self.store.add(what, due, repeat, kind)
        self.pending = None
        return _confirm(what, due, repeat, kind, now)

    def schedule(self, when: str = "today") -> str:
        now = self.now()
        if "tomorrow" in when:
            return agenda(self.store, now + timedelta(days=1), "tomorrow", now)
        if "week" in when or "all" in when or "reminders" in when:
            items = self.store.upcoming(now + timedelta(days=7))
            if not items:
                return "You have no reminders coming up."
            parts = [f"{r.text[:1].lower() + r.text[1:]} {when_text(r.when, now, r.repeat)}" for r in items[:6]]
            more = f", and {len(items) - 6} more" if len(items) > 6 else ""
            return "Coming up: " + "; ".join(parts) + more + "."
        for i, d in enumerate(DAYS):
            if d in when:
                day = now + timedelta(days=(i - now.weekday()) % 7)
                return agenda(self.store, day, f"on {d.title()}", now)
        return agenda(self.store, now, "today", now)

    def cancel(self, query: str) -> str:
        if re.search(r"\ball\b", query):
            n = len(self.store.upcoming())
            for r in self.store.upcoming():
                self.store.remove(r.id)
            return f"Cleared all {n} reminders." if n else "You don't have any reminders."
        matches = self.store.find(query) if query.strip() else []
        if not matches:
            return "I couldn't find a reminder like that."
        if len(matches) > 1:
            names = " or ".join(r.text for r in matches[:3])
            return f"Which one: {names}?"
        r = self.store.remove(matches[0].id)
        return f"Cancelled: {r.text[:1].lower() + r.text[1:]}."

    def snooze(self, minutes: int = 10) -> str | None:
        last = self.service.last_fired if self.service else None
        if not last:
            return None
        due = self.now().replace(second=0, microsecond=0) + timedelta(minutes=minutes)
        self.store.add(last.text, due, "", last.kind)
        self.service.last_fired = None
        return f"Okay, I'll remind you again in {minutes} minutes."

    def remember(self, fact: str) -> str:
        fact = self.memory.remember(fact)
        return f"Got it. I'll remember that {second_person(fact)}."

    def recall(self, query: str = "") -> str:
        facts = self.memory.search(query)
        if not facts:
            return f"You haven't told me anything about {query}." if query else "You haven't asked me to remember anything yet."
        said = "; ".join(second_person(f) for f in facts[-5:])
        return f"You told me {said}."

    def forget(self, query: str) -> str:
        gone = self.memory.forget(query)
        return f"Okay, I've forgotten that {second_person(gone[0])}." if gone else "I don't have anything like that saved."

    # ---- wiring ------------------------------------------------------------

    def add_routes(self, router: Router) -> None:
        """Instant phrasings. Order matters: the first route that answers wins.

        Whether the speaker may use these (voice recognition) is decided by the Assistant.
        """
        r = router
        r.add("snooze", r"snooze( it)?( for (?P<n>\d+|five|ten|fifteen|twenty|thirty) (minutes?|mins?))?|"
                        r"remind me again( in (?P<n2>\d+|five|ten|fifteen|twenty|thirty) (minutes?|mins?))?",
              lambda m: self.snooze(_minutes(m.group("n") or m.group("n2"))))
        r.add("reminder", r"(please )?(can you |could you )?(remind me|set (a |an )?(reminder|alarm)|"
                          r"add (a )?reminder|make (a )?reminder|schedule) .+|add .+ to my (schedule|calendar).*",
              lambda m, raw: self.add(raw), raw=True)
        r.add("reminder", r"(in|at|by|around|tomorrow|tonight|today|on|next|every|this) .+|noon|tomorrow|tonight|"
                          r"midnight|this (morning|afternoon|evening)",
              lambda m, raw: self.complete(raw), raw=True)
        r.add("schedule", r"((what are|whats|what is|show|list|read|tell)( me)? )?(my |all my |the )?(reminders|schedule|"
                          r"agenda|plans|calendar)( for| on)?(?P<w> today| tomorrow| this week| for the week| "
                          + "| ".join(DAYS) + r")?|whats on (my (schedule|calendar|agenda) )?(?P<w2>today|tomorrow|this week)|"
                          r"whats on my (schedule|calendar|agenda)|"
                          r"what do i have( on| planned| scheduled| going on)?(?P<w3> today| tomorrow| this week)?|"
                          r"do i have anything( planned| on)?(?P<w4> today| tomorrow)?",
              self._schedule_route)
        r.add("cancel", r"(cancel|delete|remove|clear|forget) (all )?(of )?(my |the )?(reminders?|alarms?)"
                        r"( (about|to|for|called) (?P<q>.+))?|(cancel|delete|remove) (the |my )?(?P<q2>.+) reminder",
              self._cancel_route)
        r.add("remember", r"remember (that )?(?!to )(?P<f>.+)",
              lambda m, raw: self.remember(re.sub(r"^\s*(?:hey |ok |okay )?(?:jarvis,? )?(?:please )?remember (?:that )?",
                                                  "", raw, flags=re.I)), raw=True)
        r.add("recall", r"what do you (remember|know)( about (?P<q>.+))?|what have i told you( about (?P<q2>.+))?|"
                        r"do you remember (?P<q3>.+)",
              lambda m: self.recall(m.group("q") or m.group("q2") or m.group("q3") or ""))
        r.add("forget", r"forget (that |about )?(?P<q>.+)", lambda m: self.forget(m.group("q")))

    def _schedule_route(self, m) -> str:
        when = next((v.strip() for v in m.groupdict().values() if v), "")
        if not when:  # "what are my reminders" means all of them; "what's my schedule" means today
            when = "all" if "reminders" in m.group(0) else "today"
        return self.schedule(when)

    def _cancel_route(self, m) -> str:
        query = (m.group("q") or m.group("q2") or "").strip()
        said = m.group(0)
        if re.match(r"\w+ all\b", said) or (not query and re.search(r"reminders|alarms", said)):
            return self.cancel("all")
        upcoming = self.store.upcoming()
        if not query and len(upcoming) == 1:  # "cancel the reminder" when there's only one
            return self.cancel(upcoming[0].text)
        return self.cancel(query)

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="add_reminder",
                description="Set a reminder or put something on the user's schedule. Use for 'remind me', "
                            "'don't let me forget', 'schedule', 'ping me'.",
                parameters={"type": "object", "properties": {
                    "what": {"type": "string", "description": "What to remind them about, e.g. 'call mum'."},
                    "when": {"type": "string", "description": "When, in plain words: 'in 20 minutes', "
                                                              "'tomorrow at 9 am', 'every weekday at 8'."},
                }, "required": ["what", "when"]},
                handler=lambda a: self.add(f"remind me to {a.get('what', '')} {a.get('when', '')}"),
            ),
            Tool(
                name="list_reminders",
                description="Read the user's reminders and schedule. 'when' is today, tomorrow, a weekday, or week.",
                parameters={"type": "object", "properties": {"when": {"type": "string"}}},
                handler=lambda a: self.schedule(str(a.get("when") or "today").lower()),
            ),
            Tool(
                name="cancel_reminder",
                description="Cancel a reminder by describing it, e.g. 'gym'.",
                parameters={"type": "object", "properties": {"which": {"type": "string"}}, "required": ["which"]},
                handler=lambda a: self.cancel(str(a.get("which", ""))),
            ),
            Tool(
                name="remember_fact",
                description="Save a fact about the user for later conversations, when they say 'remember that...'.",
                parameters={"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]},
                handler=lambda a: self.remember(str(a.get("fact", ""))),
            ),
        ]


def _minutes(word: str | None) -> int:
    if not word:
        return 10
    return int(word) if word.isdigit() else _NUMS.get(word, 10)
