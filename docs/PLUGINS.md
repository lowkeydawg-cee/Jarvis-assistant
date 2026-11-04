# Plugins

Add a skill to Jarvis by dropping one Python file into your plugins folder (`plugins` inside your
data folder, for example `D:\jarvis-data\plugins`). Restart Jarvis and it's loaded. Settings shows
which plugins loaded and the error for any that didn't. A broken plugin never stops Jarvis.

**Plugins are real code running on your laptop with your permissions. Only use ones you wrote or
trust.** Jarvis never downloads plugins by itself.

## A complete plugin

Save this as `D:\jarvis-data\plugins\dice.py`:

```python
import random


def roll(args):
    sides = int(args.get("sides") or 6)
    return f"You rolled a {random.randint(1, sides)}."


def register(jarvis):
    # A tool the AI can use. read_only=True means it runs without asking you first.
    jarvis.tool("roll_dice", "Roll a dice with any number of sides.", roll,
                parameters={"type": "object", "properties": {"sides": {"type": "integer"}}},
                read_only=True, triggers=r"dice|roll")

    # An instant answer: no AI needed. The pattern matches the whole sentence, in lowercase,
    # without punctuation.
    jarvis.route(r"roll (a )?dice", lambda match: roll({}))
```

Then say "roll a dice" or "roll a 20-sided dice".

## What a plugin can do

| Call | What it does |
|---|---|
| `jarvis.tool(name, description, handler, parameters=None, read_only=False, triggers="")` | Adds a tool for the AI. `handler(args)` returns text. It **asks you first** unless `read_only=True`. |
| `jarvis.route(pattern, handler)` | Adds an instant answer. `handler(match)` returns the reply, or `None` to let the AI answer. |
| `jarvis.every(seconds, job)` | Runs `job()` in the background. If it returns text, Jarvis says it and sends it to your phone. |
| `jarvis.notify(text)` | Says something now and sends it to your phone. |

Always set `triggers`, a regular expression of words that make the tool relevant. Without it, the
tool's description goes out with every question and uses up your cloud quota.

Voices Jarvis doesn't recognise can't use plugin tools or shortcuts. Files starting with `_` are
ignored, which is handy for switching one off.
