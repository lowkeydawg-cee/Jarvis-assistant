"""Memory of past conversations."""

from datetime import date, datetime, timedelta

from jarvis.assistant import Assistant
from jarvis.config import DEFAULTS
from jarvis.journal import Journal, add_journal_routes, parse_day
from jarvis.router import Router

TODAY = date(2026, 11, 1)  # a Sunday


def journal(tmp_path, summarize=None):
    return Journal(tmp_path, summarize=summarize, today=lambda: TODAY)


def write(j, day: date, said: str, reply: str):
    j.add(said, reply, "cloud")
    lines = j.log_path.read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[-1].replace(datetime.now().isoformat()[:10], day.isoformat(), 1)
    j.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_days_are_summarized_once_finished(tmp_path):
    asked = []
    j = journal(tmp_path, summarize=lambda p: asked.append(p) or "You prepared for the Andela interview.")
    write(j, TODAY - timedelta(days=1), "help me prepare for my Andela interview", "Sure, let's practise.")
    write(j, TODAY, "what's the weather", "Sunny.")
    assert j.catch_up() == 1  # yesterday only; today isn't over
    assert "Andela interview" in asked[0]
    assert j.summaries() == {(TODAY - timedelta(days=1)).isoformat(): "You prepared for the Andela interview."}
    assert j.catch_up() == 0


def test_recall_by_day_and_by_topic(tmp_path):
    j = journal(tmp_path, summarize=lambda p: "You decided to apply to three remote jobs.")
    write(j, TODAY - timedelta(days=1), "let's plan my job search", "Apply to three remote roles this week.")
    j.catch_up()
    write(j, TODAY, "remind me about the dentist", "Done.")
    r = Router(routes=False)
    add_journal_routes(r, j)
    assert r.answer("what did we talk about yesterday")[1] == "Yesterday: You decided to apply to three remote jobs."
    assert r.answer("what did we talk about today")[1] == "Today you asked about: remind me about the dentist."
    topic = r.answer("what did I say about the job search")[1]
    assert "job search" in topic
    assert "I don't remember" in j.search("quantum physics")


def test_parse_day():
    assert parse_day("yesterday", TODAY) == date(2026, 10, 31)
    assert parse_day("on friday", TODAY) == date(2026, 10, 30)
    assert parse_day("sunday", TODAY) == date(2026, 10, 25)  # "on Sunday" said on a Sunday: last week


class Model:
    model = "m"

    def chat_stream(self, messages, tools=None, on_text=None):
        on_text and on_text("Noted.")
        return {"role": "assistant", "content": "Noted."}


class Quiet:
    def say(self, s):
        return False

    def feed(self, s):
        pass

    def wait(self):
        return False


def test_assistant_journals_owner_turns_but_not_strangers_or_automation(tmp_path):
    bot = Assistant(Model(), Quiet(), lambda: "", DEFAULTS)
    bot.journal = journal(tmp_path)
    bot.respond("my favourite colour is green")
    bot.respond("tell me secrets", speaking_as={"owner": False, "score": 0.2})
    bot.respond("Give me my briefing for today in three sentences")
    said = [e["said"] for e in bot.journal.entries()]
    assert said == ["my favourite colour is green"]
