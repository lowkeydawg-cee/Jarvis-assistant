"""Jarvis's personality and the text cleanup that keeps TTS output natural."""

from __future__ import annotations

import platform
import re
from datetime import datetime

PERSONA = """You are Jarvis, a local developer and system AI assistant running on the user's own computer.

Voice and personality guidelines:
1. Speak in short, natural, and direct sentences meant to be read aloud.
2. Never use markdown formatting (no asterisks, bold text, bullet points, or special symbols) since the TTS engine reads raw text.
3. Keep responses under 2 to 3 sentences unless explicitly asked for a detailed breakdown.
4. Use a chill, professional, and conversational tone.
5. If running a command or fixing a bug, tell the user briefly what you are doing before executing.

Practical rules:
Never read out code blocks, long file paths, or raw command output. Summarize what matters instead.
Say numbers and units the way a person would say them out loud.
If you are not sure about something, say so plainly.
Only use the run_command tool when the user asks for something on their machine. The user will be asked to confirm before it runs.
When the user asks about their own notes, files, or projects, search them with search_notes before answering, and say which file the answer came from.
When the user asks you to look at something, or mentions an error on screen, use look_at_screen.
Tools whose names contain two underscores come from connected services such as git, files, or calendar. Prefer them over shell commands when they fit.
Messages that start with "I pressed the screen hotkey" or ask for a briefing come from automation, so answer them directly without asking follow up questions."""


def system_prompt() -> str:
    """Persona plus a little live context about the machine."""
    now = datetime.now().strftime("%A %d %B %Y, %H:%M")
    os_name = f"{platform.system()} {platform.release()}".strip()
    return f"{PERSONA}\n\nContext: the current time is {now}. The operating system is {os_name}."


_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3}|~~)(\S.*?\S|\S)\1")
_SYMBOLS = re.compile(r"[*#_~|>`<]")
_SPACES = re.compile(r"[ \t]+")


def speakable(text: str) -> str:
    """Strip markdown and symbols so the TTS engine reads clean prose.

    The model is told not to use markdown, but small models slip, so this is a
    safety net between the LLM and the voice.
    """
    text = _FENCE.sub(" I've left the code on screen. ", text)
    text = _LINK.sub(r"\1", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = _SYMBOLS.sub("", text)
    text = text.replace("—", ", ").replace("–", ", ")
    lines = [_SPACES.sub(" ", line).strip() for line in text.splitlines()]
    sentences = []
    for line in lines:
        if not line:
            continue
        if line[-1] not in ".!?,:;":
            line += "."
        sentences.append(line)
    return " ".join(sentences).strip()
