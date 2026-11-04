"""Laptop control, with Windows replaced by a recorder: nothing here touches the real machine."""

from types import SimpleNamespace

import pytest

from jarvis.assistant import Assistant
from jarvis.brainpick import cacheable
from jarvis.config import DEFAULTS
from jarvis.control import VK, Control, add_control_routes, control_tools
from jarvis.router import Router
from jarvis.toolpick import pick


@pytest.fixture
def lap(tmp_path):
    log = SimpleNamespace(run=[], press=[], launch=[], browse=[], later=[])

    def run(argv, timeout=15):
        log.run.append(argv)
        return SimpleNamespace(stdout="", returncode=0)

    c = Control(tmp_path / "apps.json", run=run, press=lambda k, n=1: log.press.append((k, n)), launch=log.launch.append,
                browse=log.browse.append, later=lambda s, fn: log.later.append(s),
                processes=lambda: ["chrome.exe", "Spotify.exe", "explorer.exe", "python.exe"],
                close_windows=lambda name: 1 if name == "calculator" else 0)
    c.apps = {"google chrome": "Chrome", "spotify": "Spotify.App", "calculator": "Calc!App", "microsoft teams": "Teams",
              "visual studio code": "Code"}
    r = Router(routes=False)
    add_control_routes(r, c)
    return c, r, log


def say(r, text):
    hit = r.answer(text)
    return hit[1] if hit else None


@pytest.mark.parametrize("said, target", [
    ("open spotify", "shell:AppsFolder\\Spotify.App"),
    ("open chrome", "shell:AppsFolder\\Chrome"),
    ("launch teams", "shell:AppsFolder\\Teams"),
    ("start the calculator app", "shell:AppsFolder\\Calc!App"),
    ("open vs code", "shell:AppsFolder\\Code"),
    ("Please open notepad.", "notepad.exe"),
])
def test_opens_apps(lap, said, target):
    c, r, log = lap
    assert say(r, said).startswith("Opening")
    assert log.launch == [target]


@pytest.mark.parametrize("said", ["open the report", "open my cv", "start the build", "open the window", "start a timer"])
def test_things_that_are_not_apps_fall_through(lap, said):
    c, r, log = lap
    assert say(r, said) is None and log.launch == [] and log.browse == []


def test_websites_and_searches(lap):
    c, r, log = lap
    assert say(r, "open youtube")
    assert say(r, "go to bbc.com")
    assert say(r, "search youtube for lo-fi beats")
    assert say(r, "play burna boy on youtube")
    assert log.browse == ["https://www.youtube.com", "https://bbc.com",
                          "https://www.youtube.com/results?search_query=lo+fi+beats",
                          "https://www.youtube.com/results?search_query=burna+boy"]


def test_tool_only_opens_web_addresses(lap):
    c, r, log = lap
    assert "only open" in c.open_url("file:///C:/Windows/System32/cmd.exe")
    assert log.browse == []


@pytest.mark.parametrize("said, key", [("volume up", "volume_up"), ("turn it down", "volume_down"), ("mute", "volume_mute"),
                                       ("pause the music", "play_pause"), ("next song", "next"), ("skip", "next"),
                                       ("previous track", "previous")])
def test_sound_and_media(lap, said, key):
    c, r, log = lap
    assert say(r, said)
    assert log.press[0][0] == VK[key]


def test_exact_volume_goes_to_zero_then_up(lap):
    c, r, log = lap
    assert say(r, "set the volume to 40") == "Volume set to 40 percent."
    assert log.press == [(VK["volume_down"], 50), (VK["volume_up"], 20)]


def test_brightness_never_goes_black(lap):
    c, r, log = lap
    assert say(r, "set brightness to 0") == "Brightness set to 10 percent."
    assert "Brightness=10" in log.run[0][-1]


def test_closing_is_gentle_and_protects_what_must_keep_running(lap):
    c, r, log = lap
    assert "Asked" in c.close_app("chrome")
    assert log.run == [["taskkill", "/IM", "chrome.exe"]]  # no /F
    log.run.clear()
    assert "won't" in c.close_app("explorer") and "won't" in c.close_app("python") and "won't" in c.close_app("jarvis")
    assert "isn't running" in c.close_app("word")
    assert log.run == []


def test_store_apps_close_by_their_window(lap):
    c, r, log = lap
    assert "Asked" in c.close_app("calculator")
    assert log.run == []


def test_shutdown_leaves_time_to_cancel(lap):
    c, r, log = lap
    assert "60 seconds" in c.power("shutdown")
    assert log.run == [["shutdown", "/s", "/t", "60"]]
    assert say(r, "cancel the shutdown") == "Cancelled. Your laptop will stay on."
    assert log.run[-1] == ["shutdown", "/a"]


@pytest.mark.parametrize("said", ["shut down my laptop", "restart the computer", "go to sleep", "close chrome"])
def test_risky_actions_have_no_instant_route(lap, said):
    c, r, log = lap
    assert say(r, said) is None and log.run == []


def test_closing_and_power_ask_first_and_a_no_means_no(lap):
    c, r, log = lap
    tools = {t.name: t for t in control_tools(c)}
    assert tools["close_app"].confirm and tools["laptop_power"].confirm
    assert not tools["laptop_control"].confirm and not tools["open_app_or_site"].confirm


class Quiet:
    def say(self, s):
        pass

    def feed(self, s):
        pass

    def wait(self):
        return False


class NoModel:
    model = "m"

    def chat_stream(self, messages, tools=None, on_text=None):
        return {"role": "assistant", "content": "I can't help with that."}


def test_a_declined_shutdown_does_nothing(lap):
    c, r, log = lap
    bot = Assistant(NoModel(), Quiet(), lambda: "no", DEFAULTS)
    for t in control_tools(c):
        bot.toolbox.register(t)
    assert "declined" in bot.toolbox.call("laptop_power", {"action": "shutdown"})
    assert log.run == []


def test_strangers_cant_control_the_laptop(lap):
    c, r, log = lap
    bot = Assistant(NoModel(), Quiet(), lambda: "", DEFAULTS)
    bot.router = r
    reply = bot.respond("set the volume to 40", speaking_as={"owner": False})
    assert log.press == [] and "recognise" in reply
    bot.respond("set the volume to 40", speaking_as={"owner": True})
    assert log.press  # the owner still can


def test_control_tools_are_sent_only_when_relevant():
    names = ["laptop_control", "laptop_power", "trading__get_profit", "trading_control__stop_everything", "look_up"]
    assert pick(names, "what is the capital of Peru") == ["look_up"]
    assert "laptop_power" in pick(names, "shut down the laptop")
    assert "trading__get_profit" in pick(names, "how much profit did the bot make")
    assert "trading_control__stop_everything" in pick(names, "stop everything")


@pytest.mark.parametrize("text", ["open the big red door", "turn the volume up high", "set a timer for pasta"])
def test_commands_are_never_answered_from_the_cache(text):
    assert not cacheable(text)


def test_app_list_is_saved_and_skips_uninstallers(tmp_path):
    out = '[{"Name":"Spotify","AppID":"S!A"},{"Name":"Uninstall Foo","AppID":"U"},{"Name":"Calculator","AppID":"C!A"}]'
    c = Control(tmp_path / "apps.json", run=lambda argv, timeout=15: SimpleNamespace(stdout=out, returncode=0))
    assert c.refresh_apps() == 2
    assert Control(tmp_path / "apps.json", run=None).apps == {"spotify": "S!A", "calculator": "C!A"}


def test_shut_down_means_jarvis_unless_you_name_the_laptop():
    from jarvis.assistant import is_exit

    assert is_exit("shut down") and is_exit("goodbye")
    assert not is_exit("shut down my laptop") and not is_exit("shut down the computer")
