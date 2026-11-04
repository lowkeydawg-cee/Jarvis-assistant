"""Market and trading-bot questions answered straight from the trading bot.

The trading bot (TradingBots/pairs-bot) plugs into Jarvis as the MCP servers
``trading`` and ``trading_control`` (set up by its connect_jarvis.py). Leaving
"how does the market look?" to the model proved unreliable: cloud models often
answer "I have no live market data" without calling the tool. So these
questions are routed in code, like the time or the battery:

    "how does the market look", "how are stocks doing"     -> get_market_brief
    "when was the best time to trade this week"            -> get_timing_report
    "where should I trade now", "how's crypto / gold / the dollar" -> get_market_brief
    "what's strong right now", "any opportunities/signals" -> get_market_watch
    "how much profit", "my p and l"                         -> get_profit
    "how's the trading bot", "bot status"                   -> get_bot_status
    "my trades", "last trades"                              -> get_recent_trades

When the bot is configured but failed to start, the same questions get the
reason instead of a guess, so a broken setup is obvious.
"""

from __future__ import annotations

import re
from typing import Callable

# Checked in order: the first pattern that matches the whole request wins.
INTENTS: list[tuple[str, str, str]] = [
    ("timing", r".*\b(when (was|were|is|are) (the )?(market|markets|it|crypto|stocks?)? ?(good|best|favou?rable)|"
               r"(best|good|favou?rable) (time|times|hours?|days?) to (trade|enter|buy)|when (could|should|would) "
               r"(i|we) have (entered|traded|bought)|weekly report|week in review|this week'?s? (report|results)|"
               r"timing report)\b.*", "get_timing_report"),
    ("where", r".*\b(where (should|to|can|do) (i|we) (trade|invest|look)|which market|best market|best place to trade|"
              r"where.*(favou?rable|best|good) to trade|favou?rable (market|place))\b.*", "get_market_brief"),
    ("profit", r".*\b(profit|profits|pnl|p and l|p n l|how much (did|have) (i|we|it) (made|make|earn|earned|lost|lose))\b.*",
     "get_profit"),
    ("trades", r".*\b(my|last|recent|latest) (paper |demo )?trades\b.*", "get_recent_trades"),
    ("watch", r".*\b(opportunit\w*|signals?|whats strong|what is strong|strong right|anything strong|"
              r"best pairs?|what (pairs|stocks) (should|to)|should i (buy|trade|enter))\b.*", "get_market_watch"),
    ("bot", r".*\b(trading bot|the bot|my bot|pairs bot|bot status|bot doing|bot running)\b.*", "get_bot_status"),
    ("market", r".*\b(market|markets|stocks?|stock market|wall street|nasdaq|s and p|sp 500|dow|crypto|"
               r"cryptos|bitcoin|btc|ethereum|ether|eth|solana|gold|silver|oil|bonds?|treasur\w*|dollar|euro|yen|"
               r"pound|forex|currenc\w*|europe|japan|china|emerging markets?|risk on|risk off)\b.*",
     "get_market_brief"),
]

_WARNINGS = ("NOT LIVE", "SIMULATED PRICES")
_SPOKEN_BRIEF_LINES = _WARNINGS + ("Mood:", "Best now:", "World:", "Relationships:", "Signals:", "Since last brief:")


def _short_best(line: str) -> str:
    """'Best now: 1. Crypto good (...); 2. ...; 7. ... Closed now: X.' -> the top three plus what's closed."""
    body, _, closed = line.partition(" Closed now:")
    ranked = body.removeprefix("Best now:").strip().rstrip(".").split("; ")
    return "Best now: " + "; ".join(ranked[:3]) + "." + (f" Closed now:{closed}" if closed else "")


def spoken(tool: str, text: str) -> str:
    """A short version for the voice; the full text still shows in the chat."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if tool == "get_market_brief" and any(ln.startswith(_SPOKEN_BRIEF_LINES) for ln in lines):
        keep = [_short_best(ln) if ln.startswith("Best now:") else ln
                for ln in lines if ln.startswith(_SPOKEN_BRIEF_LINES)]
        alerts = [ln.lstrip("- ") for ln in lines if ln.startswith("- ") and ":" in ln and "ago" in ln][:2]
        if alerts:
            keep.append("Recent alerts: " + "; ".join(a.split(": ", 1)[-1] for a in alerts))
        keep.append("That's how it looks now, not a forecast.")
        return " ".join(keep)
    if tool == "get_timing_report":
        warn = [ln for ln in lines if ln.startswith(_WARNINGS)]
        body = [ln for ln in lines if not ln.startswith(_WARNINGS)]
        ranked = [ln for ln in body if ln[:1].isdigit()][:2]
        return " ".join(warn + body[:1] + ranked + body[-1:])
    return " ".join(lines[:5])


def add_trading_routes(router, call: Callable[[str, dict], str], tool_names: list[str],
                       configured: bool, error: str | None) -> int:
    """Register the trading routes. `call(name, args)` runs a Jarvis tool. Returns how many were added."""
    available = {n.split("__", 1)[1] for n in tool_names if n.startswith("trading__")}
    added = 0
    for name, pattern, tool in INTENTS:
        if tool in available:
            def handler(m, tool=tool):
                return spoken(tool, call(f"trading__{tool}", {}))
        elif configured:
            reason = error or "it started but didn't offer its tools"

            def handler(m, reason=reason):
                return (f"The trading bot didn't load when I started: {reason}. In the pairs-bot folder run "
                        "connect_jarvis.py, then restart me.")
        elif name in ("bot", "profit", "trades"):
            def handler(m):
                return ("The trading bot isn't connected to me yet. In the pairs-bot folder run "
                        "python connect_jarvis.py, then restart me.")
        else:
            continue  # no bot set up: leave general market questions to the model
        router.add(f"trading_{name}", pattern, handler)
        added += 1
    return added
