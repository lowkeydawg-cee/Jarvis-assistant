"""The real calendar: a secret iCal address, repeating events, adding through Google Calendar."""

from datetime import date, datetime, timedelta

import pytest

from jarvis.calendar_feed import CalendarFeed, add_to_google, calendar_routes, google_event_url
from jarvis.reminders import ReminderStore
from jarvis.router import Router
from jarvis.skills import agenda

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:standup
DTSTART:20261102T100000
DTEND:20261102T101500
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
SUMMARY:Team standup
END:VEVENT
BEGIN:VEVENT
UID:dentist
DTSTART:20261104T150000
DTEND:20261104T160000
SUMMARY:Dentist
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def feed(tmp_path):
    fetched = []
    f = CalendarFeed(tmp_path / "calendar.json", fetch=lambda url: fetched.append(url) or ICS)
    f.fetched = fetched
    return f


def test_connect_reads_repeating_events_and_caches(feed):
    assert feed.set_source("webcal://calendar.google.com/calendar/ical/x/private-abc/basic.ics") == 2
    assert feed.source.startswith("https://calendar.google.com")  # webcal links are accepted too
    assert feed.events(date(2026, 11, 4)) == [(datetime(2026, 11, 4, 10, 0), "Team standup"),
                                              (datetime(2026, 11, 4, 15, 0), "Dentist")]
    assert feed.events(date(2026, 11, 7)) == []  # Saturday: no standup
    assert feed.events(date(2026, 11, 9)) == [(datetime(2026, 11, 9, 10, 0), "Team standup")]  # repeats weekly
    assert len(feed.fetched) == 1  # cached for ten minutes


def test_a_broken_address_is_refused_and_the_old_one_kept(tmp_path):
    f = CalendarFeed(tmp_path / "c.json", fetch=lambda url: (_ for _ in ()).throw(ConnectionError("404")))
    with pytest.raises(ValueError):
        f.set_source("https://example.com/nope.ics")
    assert f.source == ""


def test_agenda_merges_calendar_and_reminders(feed, tmp_path):
    feed.set_source("https://calendar.google.com/x.ics")
    store = ReminderStore(tmp_path / "r.json")
    store.add("Call mum", datetime(2026, 11, 4, 12, 30))
    text = agenda(store, datetime(2026, 11, 4, 8, 0), "today", datetime(2026, 11, 4, 8, 0), calendar=feed)
    assert text == "You have 3 things today: at 10 AM, team standup; at 12:30 PM, call mum; at 3 PM, dentist."


def test_adding_opens_google_calendar_filled_in():
    opened = []
    now = datetime(2026, 11, 1, 16, 0)  # Sunday
    reply = add_to_google("dentist Friday at 3 pm", opened.append, now=now)
    assert reply == "I've opened Google Calendar with Dentist on Friday at 3 PM. Press Save to add it."
    assert opened == [google_event_url("Dentist", datetime(2026, 11, 6, 15, 0))]
    assert "dates=20261106T150000/20261106T160000" in opened[0] and "text=Dentist" in opened[0]


def test_route_understands_put_it_on_my_calendar(feed):
    opened = []
    r = Router(routes=False)
    calendar_routes(r, feed, open_url=opened.append)
    name, reply = r.answer("Put the team dinner on my Google Calendar tomorrow at 7 p.m.")
    assert name == "calendar" and "Team dinner" in reply and opened
