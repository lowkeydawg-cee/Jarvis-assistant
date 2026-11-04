"""Laptop control: apps, websites, sound, brightness, media, lock, and power.

Harmless, easily undone actions run straight away, usually with no model involved:
"open spotify", "volume up", "pause the music", "lock my laptop". Anything that could
lose work (closing an app, sleep, restart, shut down) is only a tool with confirm=True,
so it goes through the same "should I go ahead?" as shell commands, from every channel.

What it deliberately can't do: type or click for you, change Windows settings, or
delete anything. Guests never get any of this. Turn it off with control.enabled: false.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

VK = {"volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
      "play_pause": 0xB3}
VOLUME_STEP = 2  # percent per key press on Windows
BRIGHTNESS_STEP = 20
MIN_BRIGHTNESS = 10  # never black out the screen
SHUTDOWN_DELAY = 60  # seconds, so "cancel the shutdown" still works

SITES = {
    "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com", "google": "https://www.google.com",
    "github": "https://github.com", "netflix": "https://www.netflix.com", "linkedin": "https://www.linkedin.com",
    "facebook": "https://www.facebook.com", "instagram": "https://www.instagram.com", "twitter": "https://x.com",
    "whatsapp": "https://web.whatsapp.com", "google calendar": "https://calendar.google.com",
    "google drive": "https://drive.google.com", "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai",
}
SEARCH = {"google": "https://www.google.com/search?q=", "youtube": "https://www.youtube.com/results?search_query="}
# What people say -> the name in the Start menu, then the process name for closing it.
APP_ALIASES = {"chrome": "google chrome", "edge": "microsoft edge", "vs code": "visual studio code",
               "vscode": "visual studio code", "files": "file explorer", "explorer": "file explorer"}
BUILT_IN = {"file explorer": "explorer.exe", "task manager": "taskmgr.exe", "notepad": "notepad.exe",
            "settings": "ms-settings:", "paint": "mspaint.exe"}
PROCESS_ALIASES = {"word": "winword", "powerpoint": "powerpnt", "edge": "msedge", "vs code": "code", "vscode": "code",
                   "visual studio code": "code", "calculator": "calculatorapp", "google chrome": "chrome"}
NEVER_CLOSE = {"jarvis", "python", "pythonw", "ollama", "explorer", "svchost", "csrss", "winlogon", "wininit", "lsass",
               "services", "dwm", "smss", "system", "registry", "fontdrvhost", "memcompression"}
NOT_APPS = re.compile(r"uninstall|manual|documentation|readme|license|help|website|release notes", re.I)
_PREFIX = re.compile(r"^(microsoft|google|adobe|mozilla) ")


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", name.lower())).strip()


class Control:
    def __init__(self, cache_file: str | Path, run: Callable | None = None, press: Callable | None = None,
                 launch: Callable | None = None, browse: Callable | None = None, later: Callable | None = None,
                 processes: Callable | None = None, shot_dir: str | Path | None = None,
                 close_windows: Callable | None = None):
        self.cache_file = Path(cache_file).expanduser()
        self.run = run or self._run
        self.press = press or self._press
        self.launch = launch or getattr(os, "startfile", None)
        self.browse = browse or webbrowser.open
        self.later = later or self._later
        self.processes = processes or self._processes
        self.close_windows = close_windows or self._close_windows
        self.shot_dir = Path(shot_dir).expanduser() if shot_dir else self.cache_file.parent / "screenshots"
        self.apps: dict[str, str] = {}
        try:
            self.apps = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    # ---- plumbing --------------------------------------------------------------

    @staticmethod
    def _run(argv: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _powershell(self, script: str) -> str:
        return (self.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script]).stdout or "").strip()

    @staticmethod
    def _press(key: int, times: int = 1) -> None:
        import ctypes

        for _ in range(times):
            ctypes.windll.user32.keybd_event(key, 0, 0, 0)
            ctypes.windll.user32.keybd_event(key, 0, 2, 0)
            time.sleep(0.01)

    @staticmethod
    def _later(seconds: float, fn: Callable) -> None:
        """Run after Jarvis has had time to say so."""
        t = threading.Timer(seconds, fn)
        t.daemon = True
        t.start()

    @staticmethod
    def _processes() -> list[str]:
        import psutil

        return [p.info["name"] or "" for p in psutil.process_iter(["name"])]

    @staticmethod
    def _close_windows(name: str) -> int:
        """Ask windows titled for this app to close, as their X button would. Store apps ignore taskkill.

        Only "Name" or "Document - Name" counts: a browser tab about "word games" isn't Word.
        """
        import ctypes
        from ctypes import wintypes

        user32, hits = ctypes.windll.user32, []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):
            size = user32.GetWindowTextLengthW(hwnd)
            if size and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(size + 1)
                user32.GetWindowTextW(hwnd, buf, size + 1)
                title = buf.value.lower().strip()
                if title == name or title.endswith((f" - {name}", f" \u2013 {name}")):
                    hits.append(hwnd)
            return True

        user32.EnumWindows(each, 0)
        for hwnd in hits:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return len(hits)

    # ---- apps and websites -----------------------------------------------------

    def refresh_apps(self) -> int:
        """Everything in the Start menu, desktop and Store apps alike. Takes a few seconds, so it runs aside."""
        raw = self._powershell("Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress")
        rows = json.loads(raw) if raw else []
        rows = [rows] if isinstance(rows, dict) else rows
        apps = {}
        for r in rows:
            name = str(r.get("Name") or "")
            if name and r.get("AppID") and not NOT_APPS.search(name):
                apps[_clean(name)] = str(r["AppID"])
        if apps:
            self.apps = apps
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(apps), encoding="utf-8")
        return len(apps)

    def start_background(self) -> None:
        threading.Thread(target=self.refresh_apps, daemon=True, name="app-list").start()

    def find_app(self, said: str) -> tuple[str, str] | None:
        """(name, how to start it). Exact matches only: "open the report" must not start something by accident."""
        q = _clean(said)
        q = APP_ALIASES.get(q, q)
        if q in BUILT_IN and q not in self.apps:
            return q, BUILT_IN[q]
        if q in self.apps:
            return q, "shell:AppsFolder\\" + self.apps[q]
        for name, app in self.apps.items():
            if _PREFIX.sub("", name) == q:
                return name, "shell:AppsFolder\\" + app
        starts = sorted((n for n in self.apps if n.startswith(q + " ")), key=len) if len(q) >= 4 else []
        return (starts[0], "shell:AppsFolder\\" + self.apps[starts[0]]) if starts else None

    def open_app(self, said: str) -> str | None:
        hit = self.find_app(said)
        if not hit:
            return None
        self.launch(hit[1])
        return f"Opening {hit[0].title()}."

    def open_site(self, said: str) -> str | None:
        q = _clean(said)
        if q in SITES:
            self.browse(SITES[q])
            return f"Opening {q.title()}."
        if not re.fullmatch(r"(www )?[a-z0-9-]+ (com|org|net|io|ai|dev|rw|co uk)", q):
            return None
        host = q.replace(" ", ".")
        self.browse("https://" + host)
        return f"Opening {host}."

    def open_url(self, url: str) -> str:
        if not re.match(r"https?://", url, re.I):
            return "I only open web addresses that start with http or https."
        self.browse(url)
        return f"Opened {url}."

    def search_web(self, where: str, query: str) -> str:
        self.browse(SEARCH[where] + quote_plus(query))
        return f"Searching {where.title()} for {query}."

    def open_anything(self, said: str) -> str:
        return (self.open_app(said) or self.open_site(said)
                or f"I couldn't find an app or website called {said}.")

    def close_app(self, said: str) -> str:
        q = _clean(said)
        stem = PROCESS_ALIASES.get(q, q.replace(" ", ""))
        if stem in NEVER_CLOSE:
            return f"I won't close {said}. Windows or I need it to keep running."
        windows = self.close_windows(q)
        running = {n for n in self.processes() if Path(n).stem.lower() == stem}
        if not running and not windows:
            return f"{said.title()} isn't running."
        for name in running:  # no /F: the app closes normally and can ask to save your work
            self.run(["taskkill", "/IM", name])
        return f"Asked {said.title()} to close. If it has unsaved work, it will ask you first."

    # ---- sound, screen and media -----------------------------------------------

    def volume(self, change: str, percent: int | None = None) -> str:
        if change == "set":
            level = max(0, min(100, int(percent or 0)))
            self.press(VK["volume_down"], 100 // VOLUME_STEP)  # Windows can't be told a level this way: go to zero first
            self.press(VK["volume_up"], level // VOLUME_STEP)
            return f"Volume set to {level} percent."
        if change == "mute":
            self.press(VK["volume_mute"])
            return "Sound toggled."
        steps = max(1, int(percent or 10) // VOLUME_STEP)
        self.press(VK["volume_up" if change == "up" else "volume_down"], steps)
        return "Turned it up." if change == "up" else "Turned it down."

    def media(self, action: str) -> str:
        self.press(VK[action])
        return {"play_pause": "Done.", "next": "Next track.", "previous": "Previous track.", "stop": "Stopped."}[action]

    def brightness_now(self) -> int | None:
        out = self._powershell("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness")
        return int(out) if out.isdigit() else None

    def brightness(self, change: str, percent: int | None = None) -> str:
        if change == "set":
            level = int(percent or 0)
        else:
            now = self.brightness_now()
            if now is None:
                return "I can't control this screen's brightness."
            level = now + (percent or BRIGHTNESS_STEP) * (1 if change == "up" else -1)
        level = max(MIN_BRIGHTNESS, min(100, level))
        self._powershell("Invoke-CimMethod -InputObject (Get-CimInstance -Namespace root/WMI -ClassName "
                         f"WmiMonitorBrightnessMethods) -MethodName WmiSetBrightness -Arguments @{{Brightness={level};Timeout=1}}")
        return f"Brightness set to {level} percent."

    def lock(self) -> str:
        import ctypes

        self.later(1.5, ctypes.windll.user32.LockWorkStation)
        return "Locking your laptop."

    def show_desktop(self) -> str:
        import ctypes

        u = ctypes.windll.user32
        u.keybd_event(0x5B, 0, 0, 0)  # Windows key
        u.keybd_event(0x44, 0, 0, 0)  # D
        u.keybd_event(0x44, 0, 2, 0)
        u.keybd_event(0x5B, 0, 2, 0)
        return "Here's your desktop."

    def screenshot(self) -> str:
        import mss
        import mss.tools

        self.shot_dir.mkdir(parents=True, exist_ok=True)
        path = self.shot_dir / time.strftime("screenshot-%Y%m%d-%H%M%S.png")
        with mss.MSS() as s:
            img = s.grab(s.monitors[1])
            mss.tools.to_png(img.rgb, img.size, output=str(path))
        return f"Screenshot saved to {path}."

    # ---- power: always behind a confirmation -----------------------------------

    def power(self, action: str) -> str:
        if action == "sleep":
            self.later(3, lambda: self._powershell(
                "Add-Type -AssemblyName System.Windows.Forms; "
                "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)"))
            return "Going to sleep in a few seconds."
        if action not in ("shutdown", "restart"):
            return "I can only sleep, restart or shut down."
        self.run(["shutdown", "/s" if action == "shutdown" else "/r", "/t", str(SHUTDOWN_DELAY)])
        word = "Shutting down" if action == "shutdown" else "Restarting"
        return f"{word} in {SHUTDOWN_DELAY} seconds. Save your work, or say cancel the {action} to stop it."

    def cancel_power(self) -> str:
        done = self.run(["shutdown", "/a"]).returncode == 0
        return "Cancelled. Your laptop will stay on." if done else "There's no shutdown or restart waiting."


def add_control_routes(router, c: Control) -> None:
    """Instant answers. Add these before the file routes, so "open chrome" starts Chrome rather than finding a file."""
    router.add("control", r"(open|launch|start) (up )?(the |my )?(app |application |program |website |site )?(?P<q>.+?)"
                          r"( app| application| website)?",
               lambda m: c.open_app(m.group("q")) or c.open_site(m.group("q")))
    router.add("control", r"go to (?P<q>.+)", lambda m: c.open_site(m.group("q")))
    router.add("control", r"(search|look up) (?P<w>google|youtube) for (?P<q>.+)|"
                          r"(search|look up) (?P<q2>.+) on (?P<w2>google|youtube)|play (?P<q3>.+) on (?P<w3>youtube)",
               lambda m: c.search_web(m.group("w") or m.group("w2") or m.group("w3"),
                                      m.group("q") or m.group("q2") or m.group("q3")))
    router.add("control", r"(turn |set )?(the |my )?(volume|sound) (to|at) (?P<n>\d{1,3})( percent)?",
               lambda m: c.volume("set", int(m.group("n"))))
    router.add("control", r"(turn (the |my )?(volume|sound|it|music) up|volume up|louder|make it louder|increase (the )?volume)",
               lambda m: c.volume("up"))
    router.add("control", r"(turn (the |my )?(volume|sound|it|music) down|volume down|quieter|make it quieter|"
                          r"lower (the )?volume|decrease (the )?volume)", lambda m: c.volume("down"))
    router.add("control", r"(un)?mute( the| my)?( sound| volume| laptop| audio)?", lambda m: c.volume("mute"))
    router.add("control", r"(pause|resume|play|stop)( the| my)? (music|song|video|track|media)|pause|resume|play music",
               lambda m: c.media("play_pause"))
    router.add("control", r"next( song| track| video)|skip( this| the)?( song| track| video)?", lambda m: c.media("next"))
    router.add("control", r"(previous|last|go back a)( song| track| video)", lambda m: c.media("previous"))
    router.add("control", r"(turn |set )?(the |my )?(screen )?brightness (to|at) (?P<n>\d{1,3})( percent)?",
               lambda m: c.brightness("set", int(m.group("n"))))
    router.add("control", r"(turn (the |my )?(screen )?brightness up|brightness up|brighter|make (it|the screen) brighter|"
                          r"increase (the )?brightness)", lambda m: c.brightness("up"))
    router.add("control", r"(turn (the |my )?(screen )?brightness down|brightness down|dimmer|dim (it|the screen|my screen)|"
                          r"make (it|the screen) dimmer|lower (the )?brightness|decrease (the )?brightness)",
               lambda m: c.brightness("down"))
    router.add("control", r"lock (my |the )?(laptop|computer|pc|screen)", lambda m: c.lock())
    router.add("control", r"(show (me )?(my |the )?desktop|minimi[sz]e everything)", lambda m: c.show_desktop())
    router.add("control", r"(take|grab) a? ?screenshot|screenshot( this| my screen)?", lambda m: c.screenshot())
    router.add("control", r"cancel (the |my )?(shutdown|shut down|restart|reboot)", lambda m: c.cancel_power())


def control_tools(c: Control):
    from jarvis.tools import Tool

    def act(a):
        action, value = str(a.get("action", "")), a.get("value")
        n = int(value) if str(value or "").isdigit() else None
        if action.startswith("volume_"):
            return c.volume(action[7:], n)
        if action.startswith("brightness_"):
            return c.brightness(action[11:], n)
        if action in ("play_pause", "next", "previous", "stop"):
            return c.media(action)
        simple = {"lock": c.lock, "show_desktop": c.show_desktop, "screenshot": c.screenshot}
        return simple[action]() if action in simple else f"I don't know how to {action}."

    return [
        Tool(name="laptop_control", description="Control sound, brightness, media, or lock the laptop. 'value' is a percent.",
             parameters={"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["volume_up", "volume_down", "volume_set", "volume_mute", "brightness_up",
                                                       "brightness_down", "brightness_set", "play_pause", "next", "previous",
                                                       "stop", "lock", "show_desktop", "screenshot"]},
                 "value": {"type": "integer"}}, "required": ["action"]},
             handler=act),
        Tool(name="open_app_or_site", description="Open an installed app or a website by name or http(s) address.",
             parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
             handler=lambda a: (c.open_url(str(a.get("name"))) if "://" in str(a.get("name"))
                                else c.open_anything(str(a.get("name", ""))))),
        Tool(name="close_app", description="Close a running app normally, so it can ask to save work.",
             parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
             handler=lambda a: c.close_app(str(a.get("name", ""))), confirm=True),
        Tool(name="laptop_power", description="Put the laptop to sleep, restart it or shut it down.",
             parameters={"type": "object", "properties": {"action": {"type": "string", "enum": ["sleep", "restart", "shutdown"]}},
                         "required": ["action"]},
             handler=lambda a: c.power(str(a.get("action", ""))), confirm=True),
    ]
