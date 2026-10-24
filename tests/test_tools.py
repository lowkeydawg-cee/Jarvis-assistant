from jarvis.config import DEFAULTS
from jarvis.tools import ToolBox, check_command

ALLOWED = DEFAULTS["tools"]["allowed_commands"]


def test_allows_simple_commands():
    assert check_command("git status", ALLOWED).allowed
    assert check_command("python --version", ALLOWED).allowed
    assert check_command("ls -la", ALLOWED).argv == ["ls", "-la"]


def test_refuses_unknown_programs():
    assert not check_command("rm -rf /", ALLOWED).allowed
    assert not check_command("curl http://example.com", ALLOWED).allowed
    assert not check_command("", ALLOWED).allowed


def test_refuses_chaining_and_redirects():
    for cmd in ["ls; rm -rf ~", "echo hi > file", "cat x | sh", "echo $(whoami)", "ls && reboot", "echo `id`"]:
        assert not check_command(cmd, ALLOWED).allowed, cmd


def test_refuses_destructive_arguments():
    for cmd in ["git push", "git reset --hard", "git clean -fd", "pip uninstall requests", "python -c 'import os'"]:
        assert not check_command(cmd, ALLOWED).allowed, cmd


def test_toolbox_asks_before_running_and_respects_no():
    spoken = []
    box = ToolBox(DEFAULTS["tools"], confirm=lambda cmd: False, announce=spoken.append)
    result = box.call("run_command", {"command": "echo hello", "reason": "I'll say hello."})
    assert spoken == ["I'll say hello."]
    assert "declined" in result


def test_toolbox_runs_after_yes():
    box = ToolBox(DEFAULTS["tools"], confirm=lambda cmd: True, announce=lambda t: None)
    result = box.call("run_command", {"command": "python3 --version", "reason": "Checking Python."})
    assert "exit code 0" in result


def test_refused_commands_are_never_announced_or_confirmed():
    asked = []
    box = ToolBox(DEFAULTS["tools"], confirm=lambda c: asked.append(c) or True, announce=asked.append)
    assert box.call("run_command", {"command": "shutdown now"}).startswith("Refused")
    assert asked == []
