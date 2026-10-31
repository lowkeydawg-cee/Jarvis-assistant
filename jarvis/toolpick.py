"""Send the model only the tools a request could need.

Every tool description is sent with every question. With 27 tools that was about
2,400 tokens a question, which on Groq's free tier (8,000 tokens a minute) meant
only about three questions a minute before falling back to the slow local model.
Most requests need a handful of tools, so each tool group has trigger words, and
a group is sent only when the recent conversation mentions one of them. Tools
with no group (anything new or unknown) are always sent, so nothing silently
disappears.
"""

from __future__ import annotations

import re

_LAPTOP = (r"file|folder|document|doc|pdf|word|excel|cv|resume|essay|photo|picture|image|video|song|music|movie|"
           r"download|desktop|laptop|computer|\bpc\b|disk|space|storage|drive|\bapps?\b|program|install|running|"
           r"startup|wi-?fi|network|chrome|edge|brave|browser|history|bookmark|visited|website|site|open|read|find|"
           r"where|recent|worked on|search|look for|project|code|zip|\.\w{2,4}\b")
GROUPS: dict[str, str] = {
    "laptop": _LAPTOP,
    "commands": r"run|command|git|status|version|ping|system|cpu|memory|\bram\b|uptime|process|terminal|pip|npm|python|node",
    "phone": r"phone|send|text me|notify|message me|telegram",
    "briefing": r"brief|my day|today|morning|agenda|schedule",
    "reminders": r"remind|reminder|schedule|alarm|calendar|agenda|plan|tomorrow|tonight|morning|evening|"
                 r"\bat \d|\bin \d|minute|hour|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                 r"cancel|snooze|don'?t let me forget|ping me",
    "memory": r"remember|don'?t forget|note that|keep in mind|my name|i like|i prefer",
    "notes": r"note|wrote|written|my project|where did i|notes|second brain",
    "weather": r"weather|rain|temperature|forecast|hot|cold|sunny|umbrella|wind",
    "news": r"news|headline|happening|world|latest",
    "screen": r"screen|look at|see this|this error|what's this|whats this",
}
# Tool name (or "prefix*") -> group. Names not listed here are always sent.
TOOL_GROUP: dict[str, str] = {
    "find_files": "laptop", "read_file": "laptop", "list_folder": "laptop", "recent_files": "laptop",
    "largest_files": "laptop", "laptop_overview": "laptop", "laptop_details": "laptop", "installed_apps": "laptop",
    "running_programs": "laptop", "startup_programs": "laptop", "browser_history": "laptop", "bookmarks": "laptop",
    "open_file": "laptop", "files__*": "laptop",
    "run_command": "commands", "system_info": "commands",
    "send_to_phone": "phone",
    "daily_briefing": "briefing",
    "add_reminder": "reminders", "list_reminders": "reminders", "cancel_reminder": "reminders",
    "remember_fact": "memory",
    "search_notes": "notes",
    "get_weather": "weather",
    "news_headlines": "news",
    "look_at_screen": "screen",
}
_COMPILED = {g: re.compile(p, re.I) for g, p in GROUPS.items()}


def group_of(name: str) -> str | None:
    if name in TOOL_GROUP:
        return TOOL_GROUP[name]
    for key, group in TOOL_GROUP.items():
        if key.endswith("*") and name.startswith(key[:-1]):
            return group
    return None


def pick(names: list[str], context: str) -> list[str]:
    """The tool names worth sending for this conversation context."""
    wanted = {g for g, rx in _COMPILED.items() if rx.search(context or "")}
    return [n for n in names if (group_of(n) is None) or group_of(n) in wanted]
