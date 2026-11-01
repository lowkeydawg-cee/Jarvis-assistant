"""Smart nudges: forgotten work, Downloads clutter, the evening wrap-up."""

import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from jarvis.watchers import DailyNote, DownloadsWatcher, StaleRepoWatcher, build_watchers

NOW = datetime(2026, 11, 1, 21, 5)


def fake_git(dirty: str, last_commit: datetime):
    def run(cmd, **kw):
        out = dirty if "status" in cmd else str(int(last_commit.timestamp()))
        return SimpleNamespace(stdout=out)
    return run


def test_stale_repo_nudges_once_a_day():
    w = StaleRepoWatcher(["~/catalog-agent"], days=3, run=fake_git(" M a.py\n M b.py", NOW - timedelta(days=4)))
    assert w.check(NOW) == ["catalog-agent has 2 uncommitted changes and no commit for 4 days. "
                            "Worth committing or tidying up?"]
    assert w.check(NOW) == []  # not again today
    fresh = StaleRepoWatcher(["~/x"], days=3, run=fake_git(" M a.py", NOW - timedelta(days=1)))
    assert fresh.check(NOW) == []
    clean = StaleRepoWatcher(["~/x"], days=3, run=fake_git("", NOW - timedelta(days=30)))
    assert clean.check(NOW) == []


def test_downloads_clutter_nudges_weekly(tmp_path):
    old = time.mktime((NOW - timedelta(days=60)).timetuple())
    for name in ("setup.exe", "photos.zip"):
        p = tmp_path / name
        p.write_bytes(b"0" * 1000)
        os.utime(p, (old, old))
    (tmp_path / "new_installer.exe").write_bytes(b"0" * 5000)  # recent: not counted
    w = DownloadsWatcher(str(tmp_path), min_gb=1000 / 1024 ** 3)
    msg = w.check(NOW)
    assert len(msg) == 1 and "2 old installers and archives" in msg[0]
    assert w.check(NOW + timedelta(days=2)) == []  # weekly at most
    assert w.check(NOW + timedelta(days=8))


def test_daily_note_fires_once_inside_its_window():
    made = []
    w = DailyNote("21:00", lambda: made.append(1) or "Evening wrap-up.")
    assert w.check(NOW.replace(hour=20, minute=59)) == []
    assert w.check(NOW) == ["Evening wrap-up."]
    assert w.check(NOW + timedelta(minutes=30)) == []
    assert w.check(NOW.replace(hour=23, minute=30) + timedelta(days=1)) == []  # missed the window: skipped


def test_build_watchers_includes_the_nudges():
    names = {type(w).__name__ for w in build_watchers({
        "git_repos": ["~/x"], "stale_repo_days": 3, "downloads_nudge": True,
        "evening_at": "21:00", "evening_wrap": lambda: "hi", "battery": False, "disk": False})}
    assert {"StaleRepoWatcher", "DownloadsWatcher", "DailyNote"} <= names


def test_evening_wrap_uses_schedule_weather_and_today(tmp_path):
    from jarvis.cli import evening_wrap
    from jarvis.journal import Journal

    j = Journal(tmp_path)
    j.add("let's plan the catalog agent launch", "Sure.")
    bot = SimpleNamespace(
        skills=SimpleNamespace(schedule=lambda when: "You have 1 thing tomorrow: at 10 AM, team standup."),
        online=SimpleNamespace(city="Kigali", weather_text=lambda when="today": "Tomorrow in Kigali: sunny."),
        journal=j)
    text = evening_wrap(bot)
    assert text == ("Evening wrap-up. You have 1 thing tomorrow - at 10 AM, team standup. Tomorrow in Kigali: sunny. "
                    "Today we talked about: let's plan the catalog agent launch. Good night.")
