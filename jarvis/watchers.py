"""Proactive Jarvis: background checks that speak up only when something matters.

Each watcher is a small class with a check() method that returns a list of
messages (usually empty). The Scheduler runs them on their own intervals in
one background thread and posts anything they find to Jarvis's inbox, where it
is spoken between conversations. Quiet hours and per-watcher cooldowns keep it
from getting annoying.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable


class Watcher:
    interval = 60  # seconds between checks

    def check(self, now: datetime) -> list[str]:
        raise NotImplementedError


class BatteryWatcher(Watcher):
    interval = 120

    def __init__(self, low_percent: int = 20, read=None):
        self.low = low_percent
        self.read = read or self._read
        self.warned = False

    @staticmethod
    def _read():
        import psutil

        b = psutil.sensors_battery()
        return (b.percent, b.power_plugged) if b else None

    def check(self, now):
        state = self.read()
        if not state:
            return []
        percent, plugged = state
        if plugged or percent > self.low + 5:
            self.warned = False
            return []
        if percent <= self.low and not self.warned:
            self.warned = True
            return [f"Heads up, your battery is at {percent:.0f} percent. You might want to plug in."]
        return []


class DiskWatcher(Watcher):
    interval = 1800

    def __init__(self, path: str = "~", min_free_gb: float = 10, usage=None):
        self.path = os.path.expanduser(path)
        self.min_free = min_free_gb
        self.usage = usage or shutil.disk_usage
        self.warned = False

    def check(self, now):
        free_gb = self.usage(self.path).free / 1024 ** 3
        if free_gb >= self.min_free:
            self.warned = False
            return []
        if self.warned:
            return []
        self.warned = True
        return [f"You're down to {free_gb:.0f} gigabytes of free disk space."]


class LogWatcher(Watcher):
    """Tails a log file (a build, test run, or server log) and reports failures."""

    interval = 10

    def __init__(self, path: str, pattern: str = r"(?i)\b(error|failed|failure|exception)\b", name: str = ""):
        self.path = Path(os.path.expanduser(path))
        self.regex = re.compile(pattern)
        self.name = name or self.path.stem
        self.pos = self.path.stat().st_size if self.path.exists() else 0

    def check(self, now):
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.pos:  # file was truncated or rotated
            self.pos = 0
        if size == self.pos:
            return []
        with self.path.open("r", encoding="utf-8", errors="ignore") as fh:
            fh.seek(self.pos)
            new = fh.read()
            self.pos = fh.tell()
        hits = [line.strip() for line in new.splitlines() if self.regex.search(line)]
        if not hits:
            return []
        return [f"Something failed in {self.name}. The first error says: {hits[0][:160]}"]


def load_events(ics_path: str | Path, day: date) -> list[tuple[datetime, str]]:
    """(start, title) for events on `day` in a local .ics calendar file, sorted."""
    path = Path(os.path.expanduser(str(ics_path)))
    if not path.exists():
        return []
    from icalendar import Calendar

    cal = Calendar.from_ical(path.read_bytes())
    events = []
    for ev in cal.walk("VEVENT"):
        start = ev.get("DTSTART")
        if start is None:
            continue
        start = start.dt
        if not isinstance(start, datetime):  # all-day event
            start = datetime.combine(start, datetime.min.time())
        if start.tzinfo:
            start = start.astimezone().replace(tzinfo=None)
        if start.date() == day:
            events.append((start, str(ev.get("SUMMARY", "an event"))))
    return sorted(events)


class MeetingWatcher(Watcher):
    interval = 60

    def __init__(self, ics_path: str, lead_minutes: int = 10):
        self.ics_path = ics_path
        self.lead = timedelta(minutes=lead_minutes)
        self.announced: set[tuple[datetime, str]] = set()

    def check(self, now):
        out = []
        for start, title in load_events(self.ics_path, now.date()):
            if now <= start <= now + self.lead and (start, title) not in self.announced:
                self.announced.add((start, title))
                minutes = max(1, round((start - now).total_seconds() / 60))
                out.append(f"{title} starts in {minutes} minutes.")
        return out


def git_summary(repos: list[str]) -> list[str]:
    lines = []
    for repo in repos:
        path = os.path.expanduser(repo)
        try:
            status = subprocess.run(
                ["git", "-C", path, "status", "--porcelain", "--branch"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if status.returncode != 0:
            continue
        rows = status.stdout.splitlines()
        branch = rows[0].removeprefix("## ").split("...")[0] if rows else "unknown"
        dirty = len(rows) - 1
        ahead = re.search(r"ahead (\d+)", rows[0]) if rows else None
        note = f"{Path(path).name} on {branch}: {dirty} uncommitted changes"
        if ahead:
            note += f", {ahead.group(1)} commits not pushed"
        lines.append(note)
    return lines


def briefing_facts(cfg: dict, now: datetime | None = None) -> str:
    """Collect raw facts for the morning briefing. The LLM turns them into speech."""
    now = now or datetime.now()
    facts = [f"Today is {now:%A %d %B}, it's {now:%H:%M}."]
    if cfg.get("calendar_ics"):
        events = load_events(cfg["calendar_ics"], now.date())
        facts.append(
            "Meetings today: " + "; ".join(f"{s:%H:%M} {t}" for s, t in events) if events else "No meetings today."
        )
    repos = cfg.get("git_repos") or []
    if repos:
        facts += git_summary(repos) or ["Git repos: nothing to report."]
    free_gb = shutil.disk_usage(os.path.expanduser("~")).free / 1024 ** 3
    facts.append(f"Disk: {free_gb:.0f} GB free.")
    return "\n".join(facts)


class Scheduler:
    def __init__(
        self,
        watchers: list[Watcher],
        post: Callable[[str], None],
        quiet_hours: tuple[int, int] | None = (22, 7),
        briefing_at: str | None = None,
        on_briefing: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.watchers = watchers
        self.post = post
        self.quiet = quiet_hours
        self.briefing_at = briefing_at
        self.on_briefing = on_briefing
        self.clock = clock
        self.next_run = {id(w): 0.0 for w in watchers}
        self.last_briefing: date | None = None
        self.stop_event = threading.Event()

    def is_quiet(self, now: datetime) -> bool:
        if not self.quiet:
            return False
        start, end = self.quiet
        h = now.hour
        return (start <= h or h < end) if start > end else (start <= h < end)

    def tick(self, mono: float | None = None) -> None:
        now = self.clock()
        mono = time.monotonic() if mono is None else mono
        quiet = self.is_quiet(now)
        for w in self.watchers:
            if mono < self.next_run[id(w)]:
                continue
            self.next_run[id(w)] = mono + w.interval
            try:
                messages = w.check(now)
            except Exception as exc:
                print(f"  [watch] {type(w).__name__} failed: {exc}")
                continue
            if not quiet:
                for m in messages:
                    self.post(m)
        if self.briefing_at and self.on_briefing and self.last_briefing != now.date():
            hh, mm = (int(x) for x in self.briefing_at.split(":"))
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            # Only within three hours of the set time, so starting Jarvis at night doesn't brief you.
            if due <= now <= due + timedelta(hours=3) and not quiet:
                self.last_briefing = now.date()
                self.on_briefing()

    def start(self) -> None:
        def loop():
            while not self.stop_event.wait(5):
                self.tick()

        threading.Thread(target=loop, daemon=True, name="watchers").start()


def build_watchers(cfg: dict) -> list[Watcher]:
    watchers: list[Watcher] = []
    if cfg.get("battery", True):
        watchers.append(BatteryWatcher(int(cfg.get("battery_low_percent", 20))))
    if cfg.get("disk", True):
        watchers.append(DiskWatcher(min_free_gb=float(cfg.get("disk_min_free_gb", 10))))
    if cfg.get("calendar_ics"):
        watchers.append(MeetingWatcher(cfg["calendar_ics"], int(cfg.get("meeting_lead_minutes", 10))))
    for log in cfg.get("logs") or []:
        watchers.append(LogWatcher(log["path"], log.get("pattern", LogWatcher.__init__.__defaults__[0]),
                                   log.get("name", "")))
    return watchers
