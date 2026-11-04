"""Plugins: one file adds a skill, and a broken one can't hurt Jarvis."""

from jarvis import toolpick
from jarvis.assistant import Assistant
from jarvis.config import DEFAULTS
from jarvis.plugins import load_plugins

GOOD = '''
def roll(args):
    return "You rolled a 4."

def register(jarvis):
    jarvis.tool("roll_dice", "Roll a dice.", roll, read_only=True, triggers=r"dice|roll")
    jarvis.tool("delete_things", "Delete things.", lambda a: "deleted")
    jarvis.route(r"roll (a )?dice", lambda m: roll({}))
'''


class Quiet:
    def say(self, s):
        return False

    def feed(self, s):
        pass

    def wait(self):
        return False


class Model:
    model = "m"

    def chat_stream(self, messages, tools=None, on_text=None):
        return {"role": "assistant", "content": "Hi."}


def bot():
    return Assistant(Model(), Quiet(), lambda: "", DEFAULTS)


def write(tmp_path, name, code):
    (tmp_path / name).write_text(code, encoding="utf-8")


def test_a_plugin_adds_tools_and_instant_answers(tmp_path):
    write(tmp_path, "dice.py", GOOD)
    b = bot()
    result = load_plugins(tmp_path, b, log=lambda m: None)
    assert result == [{"name": "dice", "ok": True, "tools": ["dice__roll_dice", "dice__delete_things"], "routes": 1, "jobs": 0}]
    assert b.toolbox.call("dice__roll_dice", {}) == "You rolled a 4."
    assert b.respond("roll a dice") == "You rolled a 4."


def test_plugin_tools_ask_first_unless_read_only(tmp_path):
    write(tmp_path, "dice.py", GOOD)
    b = bot()
    load_plugins(tmp_path, b, log=lambda m: None)
    assert b.toolbox.tools["dice__roll_dice"].confirm is False
    assert b.toolbox.tools["dice__delete_things"].confirm is True
    b.toolbox.confirm = lambda what: False
    assert "declined" in b.toolbox.call("dice__delete_things", {})


def test_broken_plugins_are_reported_and_the_rest_still_load(tmp_path):
    write(tmp_path, "a_syntax.py", "def register(jarvis:\n")
    write(tmp_path, "b_exits.py", "import sys\nsys.exit(1)\n")
    write(tmp_path, "c_none.py", "x = 1\n")
    write(tmp_path, "d_good.py", GOOD)
    write(tmp_path, "_off.py", "raise RuntimeError('should be ignored')\n")
    results = {r["name"]: r for r in load_plugins(tmp_path, bot(), log=lambda m: None)}
    assert set(results) == {"a_syntax", "b_exits", "c_none", "d_good"}
    assert not results["a_syntax"]["ok"] and "SyntaxError" in results["a_syntax"]["error"]
    assert not results["b_exits"]["ok"] and "no register" in results["c_none"]["error"]
    assert results["d_good"]["ok"]


def test_triggers_keep_plugin_tools_out_of_unrelated_questions(tmp_path):
    write(tmp_path, "dice.py", GOOD)
    b = bot()
    load_plugins(tmp_path, b, log=lambda m: None)
    names = list(b.toolbox.tools)
    assert "dice__roll_dice" not in toolpick.pick(names, "what is a black hole")
    assert "dice__roll_dice" in toolpick.pick(names, "roll a twenty sided dice")


def test_strangers_cannot_use_plugin_shortcuts(tmp_path):
    write(tmp_path, "dice.py", GOOD)
    b = bot()
    load_plugins(tmp_path, b, log=lambda m: None)
    assert b.respond("roll a dice", speaking_as={"owner": False, "score": 0.1}).startswith("Sorry")


def test_no_folder_is_fine(tmp_path):
    assert load_plugins(tmp_path / "missing", bot()) == []
