"""Weather, Wikipedia and headlines, with the internet replaced by canned answers."""

import json

import pytest

from jarvis.online import Online, OnlineError, add_online_routes
from jarvis.router import Router

GEO = {"results": [{"name": "Kigali", "country": "Rwanda", "latitude": -1.95, "longitude": 30.06}]}
FORECAST = {
    "current": {"temperature_2m": 19.4, "apparent_temperature": 18.6, "weather_code": 3, "wind_speed_10m": 7.2},
    "daily": {"weather_code": [61, 51], "temperature_2m_max": [25.1, 26.8], "temperature_2m_min": [16.2, 15.4],
              "precipitation_probability_max": [78, 49]},
}
SEARCH = {"pages": [{"key": "Ada_Lovelace"}]}
SUMMARY = {"type": "standard", "title": "Ada Lovelace", "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Ada_Lovelace"}},
           "extract": "Augusta Ada King, Countess of Lovelace (née Byron; 10 December 1815 – 27 November 1852), was an "
                      "English mathematician. She is often regarded as the first computer programmer. She died young."}
RSS = """<?xml version="1.0"?><rss><channel><title>BBC</title>
<item><title>First story</title><link>https://bbc.co.uk/1</link></item>
<item><title>Second story</title><link>https://bbc.co.uk/2</link></item>
<item><title>Third story</title><link>https://bbc.co.uk/3</link></item></channel></rss>"""


class Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data

    @property
    def text(self):
        return self.data


@pytest.fixture
def online(tmp_path):
    calls = []

    def get(url, headers, timeout):
        calls.append(url)
        if "geocoding" in url:
            return Resp(GEO if "Kigali" in url or "kigali" in url else {"results": []})
        if "forecast" in url:
            return Resp(FORECAST)
        if "search/page" in url:
            return Resp(SEARCH if "lovelace" in url.lower() else {"pages": []})
        if "page/summary" in url:
            return Resp(SUMMARY)
        if "rss" in url:
            return Resp(RSS)
        raise AssertionError(url)

    o = Online(tmp_path / "online.json", get=get)
    o.calls = calls
    r = Router(routes=False)
    add_online_routes(r, o)
    return o, r


def ask(r, text):
    hit = r.answer(text)
    return hit[1] if hit else None


def test_weather_asks_for_a_city_then_remembers_it(online, tmp_path):
    o, r = online
    assert ask(r, "what's the weather") == "Which city are you in? Say, for example, I live in Kigali."
    assert ask(r, "I live in kigali") == "Got it, you're in Kigali. Ask me about the weather any time."
    assert json.loads((tmp_path / "online.json").read_text()) == {"city": "Kigali"}
    assert ask(r, "what's the weather like") == ("It's 19 degrees and overcast in Kigali. "
                                                 "Today's high is 25, low 16. There's a 78 percent chance of rain.")
    assert ask(r, "will it rain tomorrow") == ("Tomorrow in Kigali: drizzling lightly, a high of 27 and a low of 15 "
                                               "degrees, with a 49 percent chance of rain.")
    assert Online(tmp_path / "online.json", get=None).city == "Kigali"  # kept across restarts


def test_weather_is_cached(online):
    o, r = online
    o.set_city("Kigali")
    ask(r, "weather")
    ask(r, "weather")
    assert sum("forecast" in u for u in o.calls) == 1


def test_unknown_city(online):
    o, r = online
    assert ask(r, "I live in Atlantisville") == "I couldn't find a place called Atlantisville."
    assert ask(r, "weather in atlantisville") == "I couldn't find a place called Atlantisville."


def test_wikipedia_answers_facts_and_drops_brackets(online):
    o, r = online
    assert ask(r, "who is Ada Lovelace") == ("Augusta Ada King, Countess of Lovelace, was an English mathematician. "
                                             "She is often regarded as the first computer programmer. That's from Wikipedia.")


@pytest.mark.parametrize("said", ["what is my schedule", "who are you", "what is 2 plus 2", "what's going on",
                                  "what is the meaning of zzqx"])
def test_personal_vague_or_unknown_questions_go_to_the_model(online, said):
    o, r = online
    assert ask(r, said) is None


def test_headlines(online):
    o, r = online
    assert ask(r, "what's the news") == "Here are the top headlines. 1: First story. 2: Second story. 3: Third story."
    assert o.headlines(2) == [{"title": "First story", "url": "https://bbc.co.uk/1"},
                              {"title": "Second story", "url": "https://bbc.co.uk/2"}]


def test_offline_is_handled(tmp_path):
    def down(url, headers, timeout):
        raise ConnectionError("no internet")

    o = Online(tmp_path / "o.json", city="Kigali", get=down)
    r = Router(routes=False)
    add_online_routes(r, o)
    assert ask(r, "weather") == "I can't reach the weather service right now."
    assert ask(r, "news") == "I can't reach the news right now."
    assert ask(r, "who is Ada Lovelace") is None  # the model can still try
    with pytest.raises(OnlineError):
        o.weather()
