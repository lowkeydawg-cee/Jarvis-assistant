"""Agent mode: plan, act, check, report, inside budgets."""

import json

import pytest

from jarvis import agent as agent_mod
from jarvis.agent import TaskAgent, parse_plan, wants_agent
from jarvis.assistant import Assistant
from jarvis.config import DEFAULTS
from jarvis.tools import Tool


class Script:
    """Replays model replies in order and records what it was shown."""

    model = "test"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def chat_stream(self, messages, tools=None, on_text=None):
        self.seen.append((messages, tools))
        msg = self.replies.pop(0)
        return {"role": "assistant", "content": "", **msg}


class Quiet:
    def __init__(self):
        self.said, self.stop = [], False

    def say(self, s):
        self.said.append(s)
        return False

    def feed(self, s):
        self.said.append(s)

    def wait(self):
        return self.stop


def call(name, **args):
    return {"content": "", "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def bot_with(replies):
    bot = Assistant(Script(replies), Quiet(), lambda: "", DEFAULTS)
    bot.router = None
    return bot


@pytest.mark.parametrize("text", [
    "task: tidy my downloads",
    "find my CV, compare it to the job email and then draft a cover letter",
    "read the report and then write a summary of it for my team",
])
def test_multi_step_requests_use_the_agent(text):
    assert wants_agent(text)


@pytest.mark.parametrize("text", ["what is the capital of Rwanda", "find my CV", "tell me a joke about cats and dogs",
                                  "should I read the book and write a review?"])
def test_ordinary_requests_do_not(text):
    assert not wants_agent(text)


def test_parse_plan_accepts_json_and_falls_back_to_the_goal():
    assert parse_plan('Sure!\n["One", "Two"]', "goal") == ["One", "Two"]
    assert parse_plan("no list here", "goal") == ["goal"]
    assert len(parse_plan(json.dumps([str(i) for i in range(20)]), "goal")) == agent_mod.MAX_STEPS


def test_runs_each_step_with_earlier_results_in_view():
    bot = bot_with([
        {"content": '["Find the CV", "Summarise it"]'},
        call("find_thing", q="cv"),
        {"content": "DONE: the CV is at D:/cv.docx"},
        {"content": "DONE: it shows five years of support work"},
        {"content": "I found your CV and summarised it."},
    ])
    used = []
    bot.toolbox.register(Tool("find_thing", "find", lambda a: used.append(a) or "D:/cv.docx"))
    updates = []
    bot.agent.on_update = updates.append
    assert bot.respond("task: find my CV and summarise it") == "I found your CV and summarised it."
    assert used == [{"q": "cv"}] and bot.last_source == "agent"
    second_step = bot.llm.seen[3][0][0]["content"]
    assert "the CV is at D:/cv.docx" in second_step  # step 2 can see step 1's result
    assert updates[-1]["state"] == "finished" and [s["status"] for s in updates[-1]["steps"]] == ["done", "done"]


def test_a_failed_step_is_retried_once_then_reported():
    bot = bot_with([
        {"content": '["Do the thing"]'},
        {"content": "FAILED: no such file"},
        {"content": "FAILED: still no file"},
        {"content": "I couldn't do it because the file is missing."},
    ])
    updates = []
    bot.agent.on_update = updates.append
    reply = bot.agent.run("task: do the thing")
    assert "couldn't" in reply
    assert updates[-1]["state"] == "failed" and updates[-1]["steps"][0]["note"] == "still no file"
    assert "the first try failed: no such file" in bot.llm.seen[2][0][1]["content"]


def test_stop_skips_the_remaining_steps():
    bot = bot_with([{"content": '["One", "Two"]'}])
    bot.speaker.stop = True
    updates = []
    bot.agent.on_update = updates.append
    assert bot.agent.run("task: two things") == "Okay, I stopped the task."
    assert updates[-1]["state"] == "stopped" and {s["status"] for s in updates[-1]["steps"]} == {"skipped"}


def test_tool_budget_stops_a_runaway_model(monkeypatch):
    monkeypatch.setattr(agent_mod, "MAX_TOOL_CALLS", 2)
    bot = bot_with([{"content": '["Loop"]'}] + [call("spin")] * 12 + [{"content": "Stopped."}])
    count = []
    bot.toolbox.register(Tool("spin", "spin", lambda a: count.append(1) or "again"))
    bot.agent.run("task: loop forever")
    assert len(count) == 2


def test_tools_needing_confirmation_still_ask():
    asked = []
    bot = Assistant(Script([{"content": '["Delete it"]'}, call("danger"), {"content": "DONE: declined"},
                            {"content": "You said no, so nothing happened."}]), Quiet(), lambda: "no", DEFAULTS)
    bot.toolbox.confirm = lambda what: asked.append(what) or False
    ran = []
    bot.toolbox.register(Tool("danger", "danger", lambda a: ran.append(1) or "gone", confirm=True))
    bot.agent.run("task: delete it")
    assert asked and not ran


def test_strangers_never_get_agent_mode():
    bot = bot_with([{"content": "Hello."}])
    assert bot.respond("task: read my files and then send them", speaking_as={"owner": False, "score": 0.1}) is not None
    assert bot.llm.seen[0][1] is None and len(bot.llm.seen) == 1
