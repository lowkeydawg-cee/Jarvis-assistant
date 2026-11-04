"""Market and trading-bot questions go straight to the trading bot, in code."""

from jarvis.router import Router
from jarvis.trading import add_trading_routes, spoken

BRIEF = """(written 2 minutes ago)
Market brief, Mon 10:15 New York
Mood: stressed (moves 1.8x their usual size), stocks -1.2% today on average.
Relationships: 9 of 28 pairs still move together (32%). Mixed: trust only STRONG signals.
Signals: 1 strong, 2 risky, 3 avoid; 4 healthy pairs waiting.
Best now: 1. Crypto good (10/15 pairs healthy, normal, 1 strong signal); 2. Bonds good (4/6 pairs healthy, calm); 3. Gold & metals mixed (2/6 pairs healthy, normal); 4. Currencies poor (1/6 pairs healthy, normal); 5. US stocks poor (5/42 pairs healthy, stressed). Closed now: World stocks.
World: US stocks -1.2%, gold +0.8%, bitcoin +2.1%. Looks risk-off: stocks down, money moving to safe havens.
- banks: -2.1%, stressed, 1/6 pairs healthy
This describes the market now; it is not a forecast.
Alerts in the last day:
- 5 minutes ago: MARKET STRESSED: spread moves are 1.8x normal."""

TOOLS = ["trading__get_timing_report", "trading__get_market_brief", "trading__get_market_watch", "trading__get_bot_status",
         "trading__get_profit", "trading__get_recent_trades", "trading__explain", "web_search"]


def router_with(tools=TOOLS, configured=True, error=None):
    calls = []

    def call(name, args):
        calls.append(name)
        return BRIEF if name.endswith("market_brief") else f"{name} says hi"
    r = Router(routes=False)
    add_trading_routes(r, call, tools, configured, error)
    return r, calls


def test_market_questions_call_the_bot_not_the_model():
    r, calls = router_with()
    for q in ("how does the market look?", "So talk to me, how does the market look", "how are stocks doing today",
              "Jarvis what's the stock market like"):
        name, reply = r.answer(q)
        assert name == "trading_market", q
        assert "Mood: stressed" in reply and "not a forecast" in reply and "MARKET STRESSED" in reply
    assert set(calls) == {"trading__get_market_brief"}


def test_each_question_goes_to_the_right_tool():
    r, calls = router_with()
    for q, tool in (("what's strong right now", "trading__get_market_watch"),
                    ("any opportunities today?", "trading__get_market_watch"),
                    ("how's the trading bot doing", "trading__get_bot_status"),
                    ("how much profit did we make", "trading__get_profit"),
                    ("show my last trades", "trading__get_recent_trades"),
                    ("where should I trade right now", "trading__get_market_brief"),
                    ("which market is best at the moment", "trading__get_market_brief"),
                    ("how is bitcoin doing", "trading__get_market_brief"),
                    ("what about ethereum and gold", "trading__get_market_brief"),
                    ("how's the euro against the dollar", "trading__get_market_brief"),
                    ("is crypto favourable today", "trading__get_market_brief"),
                    ("when was the best time to trade this week", "trading__get_timing_report"),
                    ("when was the market good", "trading__get_timing_report"),
                    ("when could I have entered", "trading__get_timing_report"),
                    ("give me the weekly report", "trading__get_timing_report")):
        calls.clear()
        assert r.answer(q) is not None, q
        assert calls == [tool], q
    assert r.answer("what's the weather like") is None


def test_a_broken_bot_says_why():
    r, _ = router_with(tools=[], configured=True, error="FileNotFoundError: python.exe")
    name, reply = r.answer("how does the market look")
    assert "didn't load" in reply and "FileNotFoundError" in reply and "connect_jarvis" in reply


def test_without_the_bot_market_questions_still_go_to_the_model():
    r, _ = router_with(tools=[], configured=False)
    assert r.answer("how does the market look") is None
    assert "isn't connected" in r.answer("how's the trading bot")[1]


def test_spoken_brief_is_short():
    s = spoken("get_market_brief", BRIEF)
    assert s.startswith("Mood: stressed") and "banks" not in s and len(s) < 900
    assert "Best now: 1. Crypto good" in s and "3. Gold" in s and "4. Currencies" not in s
    assert "Closed now: World stocks" in s and "risk-off" in s


def test_old_or_simulated_data_is_said_first():
    text = ("NOT LIVE: the watcher last scanned 3 days ago and isn't running now, so this is old.\n" + BRIEF)
    s = spoken("get_market_brief", text)
    assert s.startswith("NOT LIVE")
    report = ("SIMULATED PRICES: offline demo.\nWhen the markets were good, last 7 days.\n1. Crypto (a).\n"
              "2. Bonds (b).\n3. Oil (c).\nEntries you could have taken: x.\nThis is what happened, not a forecast.")
    s = spoken("get_timing_report", report)
    assert s.startswith("SIMULATED PRICES") and "2. Bonds" in s and "3. Oil" not in s and s.endswith("forecast.")


def test_trading_hint_never_calls_old_data_live():
    from jarvis.assistant import TRADING_HINT
    assert "NOT LIVE" in TRADING_HINT and "get_timing_report" in TRADING_HINT and "have live data" not in TRADING_HINT
