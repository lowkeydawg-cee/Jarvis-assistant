"""The developer copilot, against a real throwaway git repo."""

import inspect
import subprocess
import sys

import pytest

import jarvis.devtools as dev_mod
from jarvis.devtools import DevTools, add_dev_routes, dev_tools, find_repos, summarize_tests
from jarvis.router import Router


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@e.st", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "home" / "catalog-agent"
    r.mkdir(parents=True)
    git(r, "init", "-q")
    (r / "app.py").write_text("print('one')\n")
    (r / ".env").write_text("GROQ_API_KEY=gsk_secret\n")
    git(r, "add", "app.py", ".env")
    git(r, "commit", "-q", "-m", "First version")
    (r / "app.py").write_text("print('two')\n")
    (r / ".env").write_text("GROQ_API_KEY=gsk_changed\n")
    return r


@pytest.fixture
def dev(repo, tmp_path):
    return DevTools([str(tmp_path / "home")])


def test_finds_repos_but_not_inside_dependencies(repo, tmp_path):
    (tmp_path / "home" / "node_modules" / "pkg" / ".git").mkdir(parents=True)
    assert find_repos([str(tmp_path / "home")]) == [str(repo)]


def test_resolves_spoken_names(dev, repo):
    assert dev.resolve("catalog agent") == str(repo) and dev.resolve("catalog") == str(repo)
    assert dev.resolve("nonexistent") is None


def test_activity_status_and_diff(dev):
    assert "1 commit in 7 days" in dev.activity("catalog agent") and "First version" in dev.activity("catalog agent")
    assert "app.py" in dev.status("catalog-agent")
    assert "print('two')" in dev.diff("catalog-agent")


def test_diffs_never_include_secret_files(dev):
    assert "gsk_" not in dev.diff("catalog-agent") and "gsk_" not in dev.status("catalog-agent")
    assert dev.diff("catalog-agent", ".env").startswith("No uncommitted changes")  # even when asked for by name


def test_unknown_project_lists_the_real_ones(dev):
    assert dev.activity("nope") == "I couldn't tell which project you mean. Your projects: catalog-agent."


def test_instant_answers(dev):
    r = Router(routes=False)
    add_dev_routes(r, dev)
    assert r.answer("what changed in catalog agent this week")[1].startswith("catalog-agent: 1 commit in 7 days")
    assert r.answer("show my projects")[1] == "Your projects: catalog-agent."
    assert r.answer("what changed in the weather") is None


def test_running_tests_asks_first_and_reports_failures(dev, repo):
    tools = {t.name: t for t in dev_tools(dev)}
    assert tools["run_project_tests"].confirm is True
    assert not any(t.confirm for n, t in tools.items() if n != "run_project_tests")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_bad():\n    assert 1 == 2\n")
    assert dev.test_command(str(repo))[0] == sys.executable
    out = dev.run_tests("catalog agent")
    assert out.startswith("Tests in catalog-agent failed") and "test_bad" in out


def test_summary_of_passing_tests():
    assert summarize_tests("x", 0, "3 passed").startswith("Tests in x passed.")


def test_git_is_only_ever_read():
    source = inspect.getsource(dev_mod)
    for word in ('"commit"', '"push"', '"reset"', '"checkout"', '"clean"', '"rebase"', '"merge"'):
        assert word not in source
