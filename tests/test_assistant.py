from jarvis.assistant import Assistant, is_exit, is_yes
from jarvis.config import DEFAULTS
from jarvis.persona import PERSONA, speakable


class FakeLLM:
    """Replays scripted assistant messages, streaming their text in small pieces."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []
        self.tools_seen = []

    def chat_stream(self, messages, tools=None, on_text=None):
        self.seen.append(messages)
        self.tools_seen.append([t["function"]["name"] for t in tools or []])
        msg = self.replies.pop(0)
        text = msg.get("content", "")
        for i in range(0, len(text), 5):
            on_text and on_text(text[i : i + 5])
        return msg


class FakeSpeaker:
    def __init__(self, interrupt_after=None):
        self.fed = []
        self.interrupt_after = interrupt_after

    def feed(self, s):
        self.fed.append(s)

    def say(self, s):
        self.fed.append(s)
        return False

    def wait(self):
        return self.interrupt_after is not None and len(self.fed) >= self.interrupt_after


def make(replies, heard=(), speaker=None):
    heard = list(heard)
    speaker = speaker or FakeSpeaker()
    llm = FakeLLM(replies)
    bot = Assistant(llm, speaker, lambda: heard.pop(0) if heard else "", DEFAULTS)
    return bot, llm, speaker


def test_persona_carries_the_voice_rules():
    assert "Never use markdown" in PERSONA
    assert "2 to 3 sentences" in PERSONA
    assert "before executing" in PERSONA


def test_speakable_strips_markdown():
    raw = "## Status\n- **Disk** is *fine*\n- Run `git pull` now\nSee [docs](http://x.y)"
    assert speakable(raw) == "Status. Disk is fine. Run git pull now. See docs."
    assert speakable("Here:\n```py\nprint(1)\n```") == "Here: I've left the code on screen."


def test_reply_streams_sentence_by_sentence():
    bot, llm, speaker = make([{"role": "assistant", "content": "**All good.** Nothing to report today."}])
    assert bot.respond("how are things") == "All good. Nothing to report today."
    assert speaker.fed == ["All good.", "Nothing to report today."]
    assert llm.seen[0][0]["role"] == "system"


def test_tool_call_announces_confirms_and_feeds_result_back():
    replies = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "run_command",
                          "arguments": {"command": "python3 --version", "reason": "Checking your Python version."}}}
        ]},
        {"role": "assistant", "content": "You're on Python three."},
    ]
    bot, llm, speaker = make(replies, heard=["yes go ahead"])
    assert bot.respond("what python do I have") == "You're on Python three."
    assert speaker.fed == ["Checking your Python version.", "Should I go ahead?", "You're on Python three."]
    tool_msgs = [m for m in llm.seen[1] if m["role"] == "tool"]
    assert "exit code 0" in tool_msgs[0]["content"]


def test_interruption_stops_the_turn_and_is_remembered():
    replies = [{"role": "assistant", "content": "First sentence here. Second one never matters."}]
    bot, _, _ = make(replies, speaker=FakeSpeaker(interrupt_after=1))
    bot.respond("talk to me")
    assert bot.history[-1] == {"role": "user", "content": "(I interrupted you.)"}


def test_inbox_messages_are_spoken_and_screen_hotkey_uses_vision():
    class FakeScreen:
        def look(self, q):
            return "A terminal showing ModuleNotFoundError: No module named requests."

    replies = [{"role": "assistant", "content": "You're missing requests. Run pip install requests."}]
    bot, llm, speaker = make(replies)
    bot.screen = FakeScreen()
    bot.post("say", "Your battery is at 15 percent.")
    bot.post("screen")
    assert bot.attention.is_set()
    bot.drain_inbox()
    assert speaker.fed[0] == "Your battery is at 15 percent."
    assert speaker.fed[1] == "Taking a look."
    assert "ModuleNotFoundError" in llm.seen[0][-1]["content"]
    assert not bot.attention.is_set()


def test_yes_no_and_exit_parsing():
    assert is_yes("yeah do it")
    assert not is_yes("no, wait")
    assert not is_yes("hmm")
    assert is_exit("goodbye Jarvis")
    assert not is_exit("say goodbye to my bug")


def test_trading_hint_only_when_the_trading_bot_is_connected():
    from jarvis.assistant import TRADING_HINT
    from jarvis.tools import Tool

    bot, llm, _ = make([{"content": "Fine."}, {"content": "Calm today."}])
    bot.respond("how does the market look")
    assert all(TRADING_HINT not in m["content"] for m in llm.seen[0] if m["role"] == "system")
    bot.toolbox.register(Tool(name="trading__get_market_brief", description="market", handler=lambda a: "calm"))
    bot.respond("how does the market look")
    assert any(TRADING_HINT in m["content"] for m in llm.seen[1] if m["role"] == "system")
    assert "trading__get_market_brief" in llm.tools_seen[1]
