"""The conversation loop: listen, think (with tools), speak, and handle the inbox.

The inbox is how everything that isn't you talking reaches Jarvis: watcher
alerts, the morning briefing, and the screen hotkey. Producers post to it from
their own threads and set `attention`, which wakes the main loop out of
listening or waiting for the wake word.
"""

from __future__ import annotations

import json
import queue
import re
import threading
from typing import Callable

from jarvis.persona import system_prompt
from jarvis.router import Router
from jarvis.streaming import SentenceStream
from jarvis.tools import ToolBox

_YES = re.compile(r"\b(yes|yeah|yep|sure|go ahead|do it|ok|okay|affirmative|please)\b", re.I)
_NO = re.compile(r"\b(no|nope|don't|do not|stop|cancel|never mind|wait)\b", re.I)
_EXIT = re.compile(r"^\s*(goodbye|bye|exit|quit|shut down|that's all)\b", re.I)


PERSONAL_ROUTES = {"notes", "briefing", "reminder", "schedule", "cancel", "snooze", "remember", "recall", "forget"}


def is_yes(text: str) -> bool:
    return bool(_YES.search(text)) and not _NO.search(text)


def is_exit(text: str) -> bool:
    return bool(_EXIT.search(text))


class Assistant:
    MAX_TOOL_ROUNDS = 5

    def __init__(self, llm, speaker, listen: Callable[[], str], cfg: dict):
        self.llm = llm
        self.speaker = speaker
        self.listen = listen
        self.history_turns = int(cfg["llm"].get("history_turns", 12))
        self.history: list[dict] = []
        self.tools_enabled = cfg["tools"].get("enabled", True)
        self.toolbox = ToolBox(cfg["tools"], confirm=self._confirm, announce=self.speaker.say)
        self.inbox: queue.Queue = queue.Queue()
        self.attention = threading.Event()
        self.screen = None  # set by the CLI when vision is enabled
        self.on_error: Callable[[Exception], None] | None = None
        # Instant answers for common requests, so they never wait on the model.
        self.router = Router() if cfg.get("router", {}).get("enabled", True) else None
        self.last_source = ""  # where the last reply came from: instant, cloud or local
        self.memory = None  # jarvis.memory.Memory, set by the CLI
        self.owner_name = ""
        self.speaking_as: dict | None = None  # {"owner": bool, "score": float} when the voice was checked
        self.skills = None  # jarvis.skills.Skills: reminders, schedule and memory
        self.online = None  # jarvis.online.Online: weather, Wikipedia, headlines
        self.on_reminder: Callable | None = None  # extra alert when a reminder fires (the web page uses it)

    # ---- inbox -----------------------------------------------------------

    def post(self, kind: str, text: str = "", meta: dict | None = None) -> None:
        """Thread-safe: queue something for Jarvis to handle between turns."""
        self.inbox.put((kind, text, meta))
        self.attention.set()

    def drain_inbox(self) -> None:
        self.attention.clear()
        while True:
            try:
                kind, text, meta = self.inbox.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "say":
                    self.speaker.say(text)
                elif kind == "ask":
                    self.respond(text, speaking_as=meta)
                elif kind == "screen" and self.screen is not None:
                    self.speaker.say("Taking a look.")
                    seen = self.screen.look(text)
                    self.respond(
                        "I pressed the screen hotkey. Here is what a vision model saw on my screen:\n"
                        f"{seen}\nTell me briefly what matters, and how to fix any error."
                    )
            except Exception as exc:
                print(f"  [error] {exc}")
                if self.on_error:
                    self.on_error(exc)

    # ---- conversation ----------------------------------------------------

    def _confirm(self, what: str) -> bool:
        print(f"  [confirm] {what}")
        self.speaker.say("Should I go ahead?")
        return is_yes(self.listen())

    def _messages(self) -> list[dict]:
        trimmed = self.history[-self.history_turns * 2 :]
        # Never start the window on a dangling tool result.
        while trimmed and trimmed[0]["role"] == "tool":
            trimmed = trimmed[1:]
        return [{"role": "system", "content": system_prompt()}, *self._context(), *trimmed]

    @property
    def is_guest(self) -> bool:
        """True when the voice was checked and it isn't the owner's."""
        return bool(self.speaking_as) and not self.speaking_as.get("owner")

    def _context(self) -> list[dict]:
        """Who is talking, and what the owner asked Jarvis to remember."""
        lines = []
        if self.is_guest:
            lines.append("The person speaking now is NOT the owner: their voice wasn't recognised. Help with general "
                         "questions, but don't reveal the owner's notes, schedule, reminders or memories, and don't "
                         "run commands.")
        else:
            if self.owner_name:
                recognised = " (you recognised their voice)" if self.speaking_as else ""
                lines.append(f"You are talking with {self.owner_name}, the owner{recognised}. Use their name now and then.")
            if self.memory is not None and self.memory.prompt():
                lines.append(self.memory.prompt())
        return [{"role": "system", "content": "\n\n".join(lines)}] if lines else []

    def respond(self, user_text: str, speaking_as: dict | None = None) -> str:
        """Run one turn. Sentences are spoken as they stream in. Returns the full reply."""
        self.speaking_as = speaking_as
        self.history.append({"role": "user", "content": user_text})
        hit = self.router.answer(user_text) if self.router else None
        if hit:
            name, reply = hit
            if self.is_guest and name in PERSONAL_ROUTES:
                reply = "Sorry, I only share that with the person I know. I didn't recognise your voice."
            print(f"  [instant] {name}")
            self.last_source = "instant"
            self.history.append({"role": "assistant", "content": reply})
            self.speaker.say(reply)
            return reply
        tools = self.toolbox.specs() if self.tools_enabled and not self.is_guest else None
        spoken: list[str] = []
        self.last_source = ""

        def say_once(sentence: str) -> None:
            # Models occasionally repeat their answer word for word; never read a sentence out twice.
            if sentence.strip() in (s.strip() for s in spoken):
                return
            spoken.append(sentence)
            self.speaker.feed(sentence)

        for _ in range(self.MAX_TOOL_ROUNDS):
            stream = SentenceStream()

            def on_text(delta: str) -> None:
                for sentence in stream.feed(delta):
                    say_once(sentence)

            msg = self.llm.chat_stream(self._messages(), tools=tools, on_text=on_text)
            self.last_source = getattr(self.llm, "last_provider", "") or "local"
            for sentence in stream.flush():
                say_once(sentence)
            calls = msg.get("tool_calls") or []
            self.history.append(
                {"role": "assistant", "content": msg.get("content", ""), **({"tool_calls": calls} if calls else {})}
            )
            interrupted = self.speaker.wait()
            if interrupted:
                self.history.append({"role": "user", "content": "(I interrupted you.)"})
                return " ".join(spoken)
            if not calls:
                return " ".join(spoken)
            for call in calls:
                fn = call.get("function", {})
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                result = self.toolbox.call(fn.get("name", ""), args)
                self.history.append({
                    "role": "tool", "content": result, "tool_name": fn.get("name", ""),
                    **({"tool_call_id": call["id"]} if call.get("id") else {}),
                })

        fallback = "I got a bit tangled up there. Could you ask that another way?"
        self.speaker.say(fallback)
        return fallback

    def run(self, wait_for_wake: Callable[[threading.Event], bool] | None = None) -> None:
        self.speaker.say("Jarvis online. What can I do for you?")
        while True:
            self.drain_inbox()
            if wait_for_wake:
                if not wait_for_wake(self.attention):
                    continue  # woken by the inbox, not the wake word
                self.speaker.say("Yes?")
            try:
                heard = self.listen().strip()
            except (KeyboardInterrupt, EOFError):
                break
            if not heard:
                continue
            if is_exit(heard):
                break
            try:
                self.respond(heard)
            except Exception as exc:  # keep the assistant alive on any single failure
                from jarvis.errors import explain

                print(f"  [error] {exc}")
                self.speaker.say(explain(exc))
        self.speaker.say("Signing off. Talk soon.")
