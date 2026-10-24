from collections import namedtuple
from datetime import datetime, timedelta

from jarvis.watchers import (
    BatteryWatcher, DiskWatcher, LogWatcher, MeetingWatcher, Scheduler, load_events,
)

NOW = datetime(2026, 10, 24, 9, 50)


def test_battery_warns_once_until_plugged_in():
    state = {"v": (50, False)}
    w = BatteryWatcher(20, read=lambda: state["v"])
    assert w.check(NOW) == []
    state["v"] = (18, False)
    assert "18 percent" in w.check(NOW)[0]
    assert w.check(NOW) == []  # no nagging
    state["v"] = (80, True)
    w.check(NOW)
    state["v"] = (15, False)
    assert w.check(NOW)


def test_disk_warns_when_low():
    Usage = namedtuple("Usage", "total used free")
    w = DiskWatcher(min_free_gb=10, usage=lambda p: Usage(0, 0, 4 * 1024 ** 3))
    assert "4 gigabytes" in w.check(NOW)[0]
    assert w.check(NOW) == []


def test_log_watcher_reports_only_new_failures(tmp_path):
    log = tmp_path / "build.log"
    log.write_text("old error from yesterday\n")
    w = LogWatcher(str(log), name="the build")
    assert w.check(NOW) == []
    with log.open("a") as fh:
        fh.write("compiling...\nFAILED tests/test_api.py::test_login\n")
    msg = w.check(NOW)
    assert "the build" in msg[0] and "test_login" in msg[0]
    assert w.check(NOW) == []


def write_ics(path, start, title):
    path.write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:test\nBEGIN:VEVENT\nUID:1\n"
        f"DTSTART:{start:%Y%m%dT%H%M%S}\nDTEND:{start + timedelta(hours=1):%Y%m%dT%H%M%S}\n"
        f"SUMMARY:{title}\nEND:VEVENT\nEND:VCALENDAR\n"
    )


def test_meeting_reminder_fires_once_in_the_lead_window(tmp_path):
    ics = tmp_path / "cal.ics"
    write_ics(ics, NOW + timedelta(minutes=8), "Standup")
    assert load_events(ics, NOW.date())[0][1] == "Standup"
    w = MeetingWatcher(str(ics), lead_minutes=10)
    assert w.check(NOW) == ["Standup starts in 8 minutes."]
    assert w.check(NOW + timedelta(minutes=1)) == []
    assert MeetingWatcher(str(ics), 5).check(NOW) == []


def test_scheduler_respects_quiet_hours_and_runs_briefing_once():
    class Always:
        interval = 0

        def check(self, now):
            return ["ping"]

    posted, briefed = [], []
    clock = {"t": datetime(2026, 10, 24, 23, 0)}
    s = Scheduler([Always()], posted.append, quiet_hours=(22, 7), briefing_at="08:30",
                  on_briefing=lambda: briefed.append(1), clock=lambda: clock["t"])
    s.tick(mono=0)
    assert posted == [] and briefed == []  # quiet at 11pm, briefing not due
    clock["t"] = datetime(2026, 10, 25, 8, 45)
    s.tick(mono=1)
    s.tick(mono=2)
    assert posted == ["ping", "ping"] and briefed == [1]
