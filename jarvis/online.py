"""Live knowledge from free services that need no account or key.

- Weather: Open-Meteo (forecast and city lookup).
- Facts: Wikipedia's page summaries, so "who is..." and "what is..." get a sourced
  answer instead of a small model's guess.
- News: BBC World headlines over RSS.

What leaves the computer: the city name, the topic you ask about, and plain
requests for the headlines. Turn it all off with online.enabled: false.
Results are cached briefly so repeated questions are instant.
"""

from __future__ import annotations

import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable
from urllib.parse import quote

UA = {"User-Agent": "Jarvis/0.4 (personal voice assistant; https://github.com/lowkeydawg-cee/Jarvis-assistant)"}

# WMO weather codes, as Open-Meteo reports them.
CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "drizzling lightly", 53: "drizzling", 55: "drizzling heavily", 61: "raining lightly", 63: "raining",
    65: "raining heavily", 66: "freezing rain", 67: "freezing rain", 71: "snowing lightly", 73: "snowing",
    75: "snowing heavily", 77: "snow grains", 80: "showery", 81: "showery", 82: "pouring with rain",
    85: "snow showers", 86: "heavy snow showers", 95: "thundery", 96: "thundery with hail", 99: "thundery with hail",
}


class OnlineError(RuntimeError):
    pass


class Online:
    def __init__(self, settings_path: str | Path, city: str = "", news_feed: str = "",
                 get: Callable | None = None, timeout: float = 8):
        import requests

        self.get = get or requests.get
        self.timeout = timeout
        self.news_feed = news_feed or "https://feeds.bbci.co.uk/news/world/rss.xml"
        self.settings_path = Path(settings_path).expanduser()
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        saved = {}
        if self.settings_path.is_file():
            try:
                saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except ValueError:
                saved = {}
        self.city = saved.get("city") or city

    # ---- plumbing ------------------------------------------------------------

    def _fetch(self, url: str, ttl: float, kind: str = "json"):
        with self._lock:
            hit = self._cache.get(url)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        try:
            resp = self.get(url, headers=UA, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json() if kind == "json" else resp.text
        except Exception as exc:
            raise OnlineError(f"couldn't reach {url.split('/')[2]}: {exc}") from exc
        with self._lock:
            self._cache[url] = (time.monotonic(), data)
        return data

    def set_city(self, city: str) -> str:
        place = self.place(city)  # check it exists before saving
        self.city = place["name"]
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps({"city": self.city}), encoding="utf-8")
        return self.city

    # ---- weather -------------------------------------------------------------

    def place(self, city: str) -> dict:
        data = self._fetch(f"https://geocoding-api.open-meteo.com/v1/search?name={quote(city.strip())}&count=1",
                           ttl=86400)
        results = data.get("results") or []
        if not results:
            raise OnlineError(f"I couldn't find a place called {city.strip().title()}.")
        r = results[0]
        return {"name": r["name"], "country": r.get("country", ""), "lat": r["latitude"], "lon": r["longitude"]}

    def weather(self, city: str = "") -> dict:
        city = city or self.city
        if not city:
            raise OnlineError("no city")
        p = self.place(city)
        data = self._fetch(
            f"https://api.open-meteo.com/v1/forecast?latitude={p['lat']}&longitude={p['lon']}"
            "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            "&timezone=auto&forecast_days=2", ttl=600)
        cur, day = data["current"], data["daily"]

        def daily(i):
            return {"high": round(day["temperature_2m_max"][i]), "low": round(day["temperature_2m_min"][i]),
                    "rain": int(day["precipitation_probability_max"][i] or 0),
                    "sky": CODES.get(day["weather_code"][i], "mixed")}

        return {"city": p["name"], "temp": round(cur["temperature_2m"]), "feels": round(cur["apparent_temperature"]),
                "sky": CODES.get(cur["weather_code"], "mixed"), "code": cur["weather_code"],
                "wind": round(cur["wind_speed_10m"]), "today": daily(0), "tomorrow": daily(1)}

    def weather_text(self, city: str = "", when: str = "today") -> str:
        w = self.weather(city)
        if when == "tomorrow":
            t = w["tomorrow"]
            return (f"Tomorrow in {w['city']}: {t['sky']}, a high of {t['high']} and a low of {t['low']} degrees, "
                    f"with a {t['rain']} percent chance of rain.")
        t = w["today"]
        rain = f" There's a {t['rain']} percent chance of rain." if t["rain"] >= 20 else ""
        return (f"It's {w['temp']} degrees and {w['sky']} in {w['city']}. "
                f"Today's high is {t['high']}, low {t['low']}.{rain}")

    # ---- facts ---------------------------------------------------------------

    def lookup(self, topic: str, sentences: int = 2) -> dict | None:
        """Wikipedia's summary of the best page for `topic`, or None when nothing fits."""
        topic = re.sub(r"\s+", " ", topic).strip(" ?.!")
        if not topic:
            return None
        found = self._fetch("https://en.wikipedia.org/w/rest.php/v1/search/page?limit=1&q=" + quote(topic), ttl=3600)
        pages = found.get("pages") or []
        if not pages:
            return None
        key = pages[0]["key"]
        s = self._fetch("https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(key, safe=""), ttl=3600)
        if s.get("type") == "disambiguation" or not s.get("extract"):
            return None
        parts = re.split(r"(?<=[.!?])\s+", s["extract"].strip())
        text = " ".join(parts[:sentences])
        text = re.sub(r"\s*\([^()]*\)", "", text)  # drop pronunciations and dates in brackets, hard to listen to
        return {"title": s.get("title", key), "text": text,
                "url": s.get("content_urls", {}).get("desktop", {}).get("page", "")}

    # ---- news ----------------------------------------------------------------

    def headlines(self, n: int = 5) -> list[dict]:
        root = ET.fromstring(self._fetch(self.news_feed, ttl=1800, kind="text"))
        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if title:
                out.append({"title": title, "url": (item.findtext("link") or "").strip()})
            if len(out) >= n:
                break
        return out

    def news_text(self, n: int = 3) -> str:
        items = self.headlines(n)
        if not items:
            return "I couldn't get any headlines right now."
        return "Here are the top headlines. " + " ".join(f"{i + 1}: {h['title']}." for i, h in enumerate(items))


# ---- wiring into the router and the model's tools ----------------------------

KNOWLEDGE_SKIP = re.compile(r"^(my|your|our|it|this|that|these|those|you|i|we|me|he|she|they|the time|the date|today|"
                            r"up|going on|happening|wrong|the weather|the news|the plan|\d)\b"
                            r"|\b(plus|minus|times|divided|squared|percent of)\b")


def add_online_routes(router, online: Online) -> None:
    def weather(m):
        g = m.groupdict()
        city = next((g[k] for k in ("c1", "c2", "c3", "c4") if g.get(k)), "")
        city = re.sub(r"\b(today|tomorrow|now|right now|please)\b", "", city).strip()
        if not city and not online.city:
            return "Which city are you in? Say, for example, I live in Kigali."
        when = "tomorrow" if "tomorrow" in m.group(0) else "today"
        try:
            return online.weather_text(city, when)
        except OnlineError as exc:
            return str(exc) if "find a place" in str(exc) else "I can't reach the weather service right now."

    router.add("weather", r"((whats|what is|hows|how is)( the)? )?weather( like)?( today| tomorrow| now| outside)?"
                          r"( like)?( (in|for) (?P<c1>.+))?|"
                          r"(is it|will it|is it going to|its going to) (rain|be (hot|cold|sunny|warm))( today| tomorrow)?"
                          r"( (in|for) (?P<c2>.+))?|"
                          r"(whats|what is) the (temperature|forecast)( outside| today| tomorrow)?( (in|for) (?P<c3>.+))?|"
                          r"(do i need|should i (take|bring)) an umbrella( today| tomorrow)?( (in|for) (?P<c4>.+))?",
               weather)

    def set_city(m):
        try:
            return f"Got it, you're in {online.set_city(m.group('city'))}. Ask me about the weather any time."
        except OnlineError as exc:
            return str(exc)

    router.add("city", r"(i live in|im in|i am in|im based in|i am based in|set my (city|location) to|"
                       r"my (city|location) is) (?P<city>.+)", set_city)

    def news(m):
        try:
            return online.news_text()
        except OnlineError:
            return "I can't reach the news right now."

    router.add("news", r"((whats|what is|tell me|read me|give me|any|read)( the| me the)? )?"
                       r"(news|headlines|latest news|top stories)( today| please| headlines)?|whats happening in the world",
               news)

    def knowledge(m):
        q = next((v for k, v in m.groupdict().items() if v and k.startswith("q")), "")
        if not q or KNOWLEDGE_SKIP.match(q):
            return None  # personal or vague: let the model handle it
        try:
            hit = online.lookup(q)
        except OnlineError:
            return None
        return f"{hit['text']} That's from Wikipedia." if hit else None

    router.add("knowledge", r"((who|what) (is|was|are|were)|whats|whos) (a |an |the )?(?P<q1>.+)|(tell me )?about (?P<q2>.+)|"
                            r"(look up|search for|search wikipedia for|define) (?P<q3>.+)|who invented (?P<q4>.+)",
               knowledge)


def online_tools(online: Online):
    from jarvis.tools import Tool

    def look(a):
        hit = online.lookup(str(a.get("topic", "")), sentences=4)
        return f"Wikipedia, {hit['title']}: {hit['text']}" if hit else "Nothing found on Wikipedia."

    return [
        Tool(name="look_up", description="Look a topic up on Wikipedia for accurate facts about people, places, "
                                         "things, history and science. Use it before answering factual questions.",
             parameters={"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
             handler=look),
        Tool(name="get_weather", description="Current weather and forecast. 'city' is optional (defaults to the user's city).",
             parameters={"type": "object", "properties": {"city": {"type": "string"}, "when": {"type": "string",
                         "description": "today or tomorrow"}}},
             handler=lambda a: online.weather_text(str(a.get("city") or ""), str(a.get("when") or "today"))),
        Tool(name="news_headlines", description="Today's top world news headlines.",
             handler=lambda a: online.news_text(5)),
    ]
