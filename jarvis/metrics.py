"""How fast is Jarvis, really? Every turn is timed, so slow parts show up as numbers.

Each turn records where the answer came from, the time to the first word, the
total time, how many tools ran, and a rough token count for the prompt. Only
timings are stored: never what was said. The last KEEP turns live in a small JSON
lines file, and `summary` turns them into medians and percentiles for the
dashboard in the browser app.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

KEEP = 500


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))]


class Metrics:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.lock = threading.Lock()
        self.turns: list[dict] = []
        if self.path.is_file():
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines()[-KEEP:]:
                    self.turns.append(json.loads(line))
            except (OSError, ValueError):
                self.turns = []

    def record(self, source: str, first_word_ms: float, total_ms: float, tools: int = 0, tokens: int = 0,
               model: str = "", channel: str = "app") -> None:
        turn = {"at": round(time.time()), "source": source or "other", "first_ms": round(first_word_ms),
                "total_ms": round(total_ms), "tools": tools, "tokens": tokens, "model": model, "channel": channel}
        with self.lock:
            self.turns.append(turn)
            self.turns = self.turns[-KEEP:]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if len(self.turns) == KEEP:  # keep the file from growing forever
                    self.path.write_text("\n".join(json.dumps(t) for t in self.turns) + "\n", encoding="utf-8")
                else:
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(turn) + "\n")
            except OSError as exc:
                print(f"  [metrics] couldn't save: {exc}")

    def summary(self, recent: int = 40) -> dict:
        with self.lock:
            turns = list(self.turns)
        by: dict[str, dict] = {}
        for source in sorted({t["source"] for t in turns}):
            times = [t["first_ms"] for t in turns if t["source"] == source]
            by[source] = {"count": len(times), "p50_ms": round(percentile(times, 50)), "p95_ms": round(percentile(times, 95))}
        tokens = [t["tokens"] for t in turns if t.get("tokens")]
        hour_ago = time.time() - 3600
        return {
            "turns": len(turns), "by_source": by,
            "recent": [{"source": t["source"], "first_ms": t["first_ms"]} for t in turns[-recent:]],
            "avg_tokens": round(sum(tokens) / len(tokens)) if tokens else 0,
            "last_hour": sum(t["at"] >= hour_ago for t in turns),
        }
