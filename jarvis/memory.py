"""Long-term memory: things you ask Jarvis to remember, kept across restarts.

"Remember that my standup is at ten" is stored as written. Every conversation
starts with these facts in front of the model, and "what do you remember" or
"forget that..." work instantly without it.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path

_SWAP = [
    (r"\bi am\b", "you are"), (r"\bi'm\b", "you're"), (r"\bim\b", "you're"), (r"\bi was\b", "you were"),
    (r"\bmy\b", "your"), (r"\bmine\b", "yours"), (r"\bme\b", "you"), (r"\bmyself\b", "yourself"), (r"\bi\b", "you"),
]


def second_person(text: str) -> str:
    """'my standup is at ten' -> 'your standup is at ten', for saying it back."""
    out = text
    for pattern, repl in _SWAP:
        out = re.sub(pattern, repl, out, flags=re.I)
    return out


def _subject(text: str) -> str:
    """'my standup is at 10:30' -> 'my standup': what the fact is about."""
    parts = re.split(r"\b(?:is|are|was|were|has|have|lives|works)\b", text.lower(), maxsplit=1)
    return parts[0].strip() if len(parts) == 2 else ""


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - {
        "the", "a", "an", "my", "is", "are", "at", "to", "that", "about", "of", "and", "i", "you", "your", "what", "do"}


class Memory:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.lock = threading.Lock()
        self.facts: list[dict] = []
        if self.path.is_file():
            try:
                self.facts = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                self.facts = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.facts, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def remember(self, fact: str) -> str:
        fact = re.sub(r"\s+", " ", fact).strip(" .,!?")
        with self.lock:
            # A new fact about the same thing replaces the old one ("my standup is at 11 now").
            new, subject = _words(fact), _subject(fact)
            self.facts = [f for f in self.facts
                          if not (subject and _subject(f["text"]) == subject)
                          and len(new & _words(f["text"])) < max(2, len(new) * 0.6)]
            self.facts.append({"text": fact, "at": datetime.now().isoformat(timespec="minutes")})
            self._save()
        return fact

    def search(self, query: str) -> list[str]:
        q = _words(query)
        if not q:
            return [f["text"] for f in self.facts]
        return [f["text"] for f in self.facts if q & _words(f["text"])]

    def forget(self, query: str) -> list[str]:
        gone = self.search(query) if _words(query) else []
        with self.lock:
            self.facts = [f for f in self.facts if f["text"] not in gone]
            self._save()
        return gone

    def prompt(self) -> str:
        """Facts for the model's context, or '' when there are none."""
        if not self.facts:
            return ""
        lines = "\n".join(f"- {f['text']}" for f in self.facts[-40:])
        return f"Things the user asked you to remember (in their words, 'I' and 'my' mean the user):\n{lines}"
