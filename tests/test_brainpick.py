"""The right brain per question, and no brain for repeats."""

import pytest

from jarvis.assistant import Assistant
from jarvis.brainpick import AnswerCache, cache_key, cacheable, difficulty
from jarvis.cloud import CloudError, HybridLLM
from jarvis.config import DEFAULTS
from jarvis.tools import Tool


def tool(name):
    return {"type": "function", "function": {"name": name}}


def user(text):
    return [{"role": "system", "content": "persona"}, {"role": "user", "content": text}]


@pytest.mark.parametrize("text, tools, want", [
    ("what is the boiling point of water", None, "easy"),
    ("tell me a joke", None, "easy"),
    ("why is the sky blue", None, "hard"),
    ("compare python and rust for web servers", None, "hard"),
    ("write a poem", None, "hard"),
    ("what is the boiling point of water", [tool("find_files")], "hard"),
    ("what is the boiling point of water", [tool("look_up")], "easy"),  # sent with every question, so no signal
    ("word " * 20, None, "hard"),
])
def test_difficulty(text, tools, want):
    assert difficulty(user(text), tools) == want


class Brain:
    def __init__(self, model, fail=False):
        self.model, self.fail, self.calls = model, fail, 0

    def chat_stream(self, messages, tools=None, on_text=None):
        self.calls += 1
        if self.fail:
            raise CloudError("429")
        return {"role": "assistant", "content": self.model}


def test_easy_goes_to_the_fast_model_and_hard_to_the_big_one():
    big, fast, local = Brain("big"), Brain("fast"), Brain("local")
    llm = HybridLLM(local, big, log=lambda m: None, fast=fast)
    assert llm.chat_stream(user("what is the capital of Peru"))["content"] == "fast"
    assert llm.chat_stream(user("explain how vaccines work"))["content"] == "big"
    assert llm.chat_stream(user("what is the capital of Peru"), tools=[tool("find_files")])["content"] == "big"
    assert local.calls == 0 and llm.last_model == "big"


def test_busy_fast_model_falls_through_to_the_big_one():
    big, fast = Brain("big"), Brain("fast", fail=True)
    llm = HybridLLM(Brain("local"), big, log=lambda m: None, fast=fast)
    assert llm.chat_stream(user("what is the capital of Peru"))["content"] == "big"


def test_private_mode_uses_the_bigger_local_model_for_hard_questions():
    small, large = Brain("small"), Brain("large")
    llm = HybridLLM(small, Brain("cloud"), private=True, log=lambda m: None, local_hard=large)
    assert llm.chat_stream(user("what is the capital of Peru"))["content"] == "small"
    assert llm.chat_stream(user("explain how vaccines work"))["content"] == "large"


@pytest.mark.parametrize("text", ["what is my name", "who am I", "what is it called", "what is the weather like",
                                  "what is the latest news", "who is the president of France", "bitcoin price right now",
                                  "and what about that one", "yes"])
def test_personal_timely_or_contextual_questions_are_never_cached(text):
    assert not cacheable(text)


def test_the_same_question_in_other_words_hits(tmp_path):
    c = AnswerCache(tmp_path / "a.json")
    c.put("what is the boiling point of water", "100 degrees Celsius.")
    assert c.get("boiling point of water, please") == "100 degrees Celsius."
    assert cache_key("the boiling point of water") == cache_key("boiling point of water")
    assert AnswerCache(tmp_path / "a.json").get("boiling point of water please") == "100 degrees Celsius."


def test_entries_expire_and_the_size_is_limited(tmp_path):
    now = [1000.0]
    c = AnswerCache(tmp_path / "a.json", days=1, limit=3, clock=lambda: now[0])
    c.put("capital city of Peru", "Lima.")
    now[0] += 90000
    assert c.get("capital city of Peru") is None
    for i in range(6):
        now[0] += 1
        c.put(f"capital city of country number {i}", "Somewhere.")
    assert len(c.items) == 3


class Quiet:
    def __init__(self):
        self.said = []

    def say(self, s):
        self.said.append(s)
        return False

    def feed(self, s):
        self.said.append(s)

    def wait(self):
        return False


class Once:
    model = "m"
    last_provider = "cloud"

    def __init__(self, reply=None):
        self.calls, self.reply = 0, reply

    def chat_stream(self, messages, tools=None, on_text=None):
        self.calls += 1
        if self.reply:
            return self.reply.pop(0)
        on_text and on_text("Lima is the capital. ")
        return {"role": "assistant", "content": "Lima is the capital."}


def make(tmp_path, llm):
    bot = Assistant(llm, Quiet(), lambda: "", DEFAULTS)
    bot.router = None
    bot.answers = AnswerCache(tmp_path / "a.json")
    return bot


def test_second_ask_skips_the_model(tmp_path):
    llm = Once()
    bot = make(tmp_path, llm)
    bot.respond("what is the capital of Peru")
    assert bot.respond("what is the capital of Peru") == "Lima is the capital."
    assert llm.calls == 1 and bot.last_source == "cached"


def test_answers_that_used_tools_are_not_cached(tmp_path):
    llm = Once([{"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "t", "arguments": "{}"}}]},
                {"role": "assistant", "content": "Done."}])
    bot = make(tmp_path, llm)
    bot.toolbox.register(Tool("t", "t", lambda a: "x"))
    bot.respond("capital city of Peru")
    assert bot.answers.items == {}


class Busy(Brain):
    """Rate limited the first `times` calls."""

    def __init__(self, model, error, times=99):
        super().__init__(model)
        self.error, self.times = error, times

    def chat_stream(self, messages, tools=None, on_text=None):
        self.calls += 1
        if self.calls <= self.times:
            raise CloudError(self.error)
        return {"role": "assistant", "content": self.model}


LIMIT = "Cloud model returned 429: Rate limit reached. Please try again in 7.5s."


def test_a_short_rate_limit_is_waited_out_instead_of_using_the_slow_local_model():
    naps, local = [], Brain("local")
    llm = HybridLLM(local, Busy("big", LIMIT, times=1), log=lambda m: None)
    llm.sleep = naps.append
    assert llm.chat_stream(user("explain how vaccines work"))["content"] == "big"
    assert naps == [7.7] and local.calls == 0


@pytest.mark.parametrize("error", ["Cloud model returned 429: Please try again in 2m10s.", "Cloud model returned 413: too large"])
def test_long_waits_and_other_errors_go_straight_to_local(error):
    naps, local = [], Brain("local")
    llm = HybridLLM(local, Busy("big", error), log=lambda m: None)
    llm.sleep = naps.append
    assert llm.chat_stream(user("explain how vaccines work"))["content"] == "local"
    assert naps == []


def test_the_local_fallback_reads_a_short_version():
    seen = []

    class Local(Brain):
        def chat_stream(self, messages, tools=None, on_text=None):
            seen.extend(messages)
            return {"role": "assistant", "content": "ok"}

    llm = HybridLLM(Local("local"), Busy("big", "Cloud model returned 413: too large"), log=lambda m: None)
    long = [{"role": "user", "content": "x" * 9000} for _ in range(20)]
    llm.chat_stream([{"role": "system", "content": "persona"}, *long])
    assert len(seen) == 7 and max(len(m["content"]) for m in seen[1:]) == 1500


def test_a_huge_conversation_is_cut_to_fit_the_cloud_limit():
    bot = Assistant(Once(), Quiet(), lambda: "", DEFAULTS)
    for i in range(8):
        bot.history += [{"role": "user", "content": f"question {i}"},
                        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
                        {"role": "tool", "content": "y" * 6000},
                        {"role": "assistant", "content": "z" * 5000}]
    bot.history.append({"role": "user", "content": "latest"})
    sent = bot._messages()
    assert sum(len(str(m.get("content") or "")) for m in sent) < 26000
    assert sent[-1]["content"] == "latest"
    assert sum(len(m["content"]) for m in sent if m["role"] == "tool") <= 303 * 8  # old file reads shrink


@pytest.mark.parametrize("text", ["how much profit did the trading bot make", "explain how the pairs bot decides when to trade",
                                  "where does the bot log its daily autopsy", "what changed in the catalog project"])
def test_questions_about_the_users_own_things_are_never_cached(text):
    assert not cacheable(text)


def test_a_cant_help_answer_is_never_saved_or_served(tmp_path):
    c = AnswerCache(tmp_path / "a.json")
    c.put("capital city of Peru", "I don't have access to that.")
    assert c.items == {}
    c.items["capital city peru"] = {"reply": "Sorry, I can't say.", "at": c.clock()}  # saved by an older version
    assert c.get("capital city of Peru") is None
