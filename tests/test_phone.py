import http.client
import json
import threading
import time

import pytest

import jarvis.phone as phone_mod
from jarvis.config import DEFAULTS, _merge
from jarvis.notify import HOURLY_CAP, Notifier
from jarvis.phone import Phone, cookie_value, hostname
from jarvis.pwa import asset, icon
from jarvis.server import WebSession, serve


# ---- phone alerts ---------------------------------------------------------

class Outbox:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, url, message, headers):
        if self.fail:
            raise OSError("no internet")
        self.sent.append((url, message, headers))


def notifier(tmp_path, cfg=None, clock=None, fail=False):
    box = Outbox(fail)
    n = Notifier(_merge(DEFAULTS["notify"], cfg or {}), tmp_path, post=box, background=False,
                 clock=clock or time.time)
    return n, box


def test_alerts_stay_off_until_switched_on(tmp_path):
    n, box = notifier(tmp_path)
    assert not n.enabled and n.topic == ""
    assert n.send("reminders", "call mum") is False and box.sent == []


def test_switching_on_makes_a_private_topic_that_survives_a_restart(tmp_path):
    n, _ = notifier(tmp_path)
    n.turn_on()
    assert n.topic.startswith("jarvis-") and len(n.topic) > 15
    again, _ = notifier(tmp_path)
    assert again.enabled and again.topic == n.topic
    assert again.subscribe_url == "https://ntfy.sh/" + n.topic


def test_alert_reaches_ntfy_with_title_priority_and_tap_to_open(tmp_path):
    n, box = notifier(tmp_path, {"enabled": True, "topic": "jarvis-test"})
    n.click_url = "https://laptop.tail1.ts.net"
    assert n.send("reminders", "Call  mum\n now")
    url, message, headers = box.sent[0]
    assert url == "https://ntfy.sh/jarvis-test" and message == "Call mum now"
    assert headers["Title"] == "Reminder" and headers["Priority"] == "4"
    assert headers["Click"] == "https://laptop.tail1.ts.net"


def test_alerts_respect_your_choices_and_never_spam(tmp_path):
    now = [1000.0]
    n, box = notifier(tmp_path, {"enabled": True, "topic": "t", "send": ["alerts"]}, clock=lambda: now[0])
    assert n.send("briefing", "Today is Friday") is False  # not on the list
    assert n.send("alerts", "Battery low") and n.send("alerts", "Battery low") is False  # repeat swallowed
    now[0] += 601
    assert n.send("alerts", "Battery low")  # ten minutes later it may say it again
    for i in range(HOURLY_CAP):
        n.send("alerts", f"alert {i}")
    assert len(box.sent) == HOURLY_CAP  # the hourly cap holds
    assert n.send("briefing", "a test", force=True)  # a test you asked for always goes


def test_a_failed_alert_never_breaks_jarvis(tmp_path):
    n, _ = notifier(tmp_path, {"enabled": True, "topic": "t"}, fail=True)
    assert n.send("alerts", "hi") is True
    assert "no internet" in n.last_error


# ---- pairing a phone ------------------------------------------------------

def test_phone_hosts_and_key(tmp_path):
    p = Phone({}, tmp_path)
    assert not p.host_allowed("laptop.tail1.ts.net")  # off by default
    p.turn_on()
    assert p.host_allowed("laptop.tail1.ts.net") and p.host_allowed("100.101.2.3:8765")
    assert p.host_allowed("192.168.1.20:8765")
    assert not p.host_allowed("evil.example") and not p.host_allowed("8.8.8.8")
    assert p.key_ok(p.key) and not p.key_ok("guess") and not p.key_ok("")
    old = p.key
    p.reset_key()
    assert not p.key_ok(old)
    assert Phone({}, tmp_path).key == p.key  # kept across restarts
    assert hostname("[::1]:80") == "::1" and hostname("A.ts.net:443") == "a.ts.net"
    assert cookie_value("a=1; jarvis_key=xyz; b=2") == "xyz"


def test_phone_panel_reports_tailscale_steps(tmp_path):
    class Run:
        def __init__(self, serving):
            self.serving = serving

        def __call__(self, argv, **kw):
            out = {"BackendState": "Running", "Self": {"DNSName": "laptop.tail1.ts.net."}}
            if argv[1] == "serve":
                out = {"TCP": {"443": {}}} if self.serving else {}
            return type("R", (), {"stdout": json.dumps(out)})()

    import jarvis.phone as phone_mod

    orig = phone_mod.tailscale_exe
    phone_mod.tailscale_exe = lambda: "tailscale"
    try:
        p = Phone({"phone": True}, tmp_path, run=Run(False))
        info = p.info(8765)
        assert info["tailscale"]["running"] and not info["tailscale"]["serving"] and info["pair_url"] == ""
        assert info["serve_command"] == "tailscale serve --bg 8765"
        p = Phone({"phone": True}, tmp_path, run=Run(True))
        info = p.info(8765)
        assert info["url"] == "https://laptop.tail1.ts.net" and info["https"]
        assert info["pair_url"] == f"https://laptop.tail1.ts.net/pair?key={p.key}"
    finally:
        phone_mod.tailscale_exe = orig


def test_app_icons_are_real_pngs():
    png = icon(48)
    assert png.startswith(b"\x89PNG") and len(png) > 100
    body, kind = asset("/manifest.webmanifest")
    assert kind == "application/manifest+json" and json.loads(body)["display"] == "standalone"
    assert asset("/sw.js")[1] == "text/javascript" and asset("/nope") is None


# ---- the server with a phone ----------------------------------------------

class QuietLLM:
    model = "fake"
    last_provider = "cloud"

    def __init__(self, replies=()):
        self.replies = list(replies)

    def chat_stream(self, messages, tools=None, on_text=None):
        msg = self.replies.pop(0) if self.replies else {"role": "assistant", "content": "Done."}
        on_text and on_text(msg["content"])
        return msg


@pytest.fixture
def phone_web(tmp_path, monkeypatch):
    # Behave as if Tailscale isn't installed, whatever this machine has: the fake runner below
    # returns nothing, which only works while the real Tailscale isn't there to be asked.
    monkeypatch.setattr(phone_mod, "tailscale_exe", lambda: "")
    started = []

    def start(phone_on=True, replies=()):
        cfg = _merge(DEFAULTS, {"data_dir": str(tmp_path)})
        session = WebSession(QuietLLM(replies), cfg)
        session.phone = Phone({"phone": phone_on}, tmp_path, run=lambda *a, **k: None)
        session.bot.notifier, box = notifier(tmp_path, {"enabled": True, "topic": "t"})
        httpd, _ = serve(session, "127.0.0.1", 0, open_browser=False)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return session, httpd.server_port, box

    yield start
    for h in started:
        h.shutdown()


def request(port, path, host=None, method="GET", headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    if body is not None:
        h["Content-Type"] = "application/json"
    conn.request(method, path, body=None if body is None else json.dumps(body), headers=h)
    resp = conn.getresponse()
    return resp.status, dict(resp.getheaders()), resp.read()


TS = "laptop.tail1.ts.net"
RELAY = {"X-Forwarded-For": "100.101.2.3", "Tailscale-User-Login": "saint@example.com"}


def test_unpaired_phone_sees_only_the_pairing_page(phone_web):
    session, port, _ = phone_web()
    status, _, body = request(port, "/", host=TS, headers=RELAY)
    assert status == 401 and b"Pair this phone" in body
    assert request(port, "/api/status", host=TS, headers=RELAY)[0] == 401
    assert request(port, "/api/chat", host=TS, headers=RELAY, method="POST", body={"text": "hi"})[0] == 401
    assert request(port, "/pair?key=wrong", host=TS, headers=RELAY)[0] == 401
    assert request(port, "/manifest.webmanifest", host=TS, headers=RELAY)[0] == 200  # needed to install the app


def test_pairing_sets_a_cookie_and_then_the_phone_works(phone_web):
    session, port, _ = phone_web()
    status, headers, _ = request(port, f"/pair?key={session.phone.key}", host=TS, headers=RELAY)
    assert status == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and session.phone.key in cookie
    paired = {**RELAY, "Cookie": cookie.split(";")[0]}
    status, _, body = request(port, "/", host=TS, headers=paired)
    assert status == 200 and b"manifest.webmanifest" in body
    data = json.loads(request(port, "/api/status", host=TS, headers=paired)[2])
    assert data["remote"] is True and data["phone"] is True
    # The phone can never read or change the phone key.
    assert request(port, "/api/phone", host=TS, headers=paired)[0] == 404
    assert request(port, "/api/phone", host=TS, headers=paired, method="POST", body={"reset_key": True})[0] == 403


def test_phone_switched_off_means_no_way_in(phone_web):
    session, port, _ = phone_web(phone_on=False)
    assert request(port, "/", host=TS, headers=RELAY)[0] == 403
    # A relayed request posing as localhost is still not "this laptop".
    assert request(port, "/api/status", headers=RELAY)[0] == 403
    assert request(port, "/api/status")[0] == 200


def test_laptop_can_switch_phone_and_alerts(phone_web):
    session, port, box = phone_web(phone_on=False)
    data = json.loads(request(port, "/api/phone", method="POST", body={"phone": True})[2])
    assert data["phone"]["enabled"] and session.phone.enabled
    assert json.loads(request(port, "/api/notify/test", method="POST", body={})[2])["ok"]
    assert "test from Jarvis" in box.sent[-1][1]


def test_an_answer_you_walked_away_from_goes_to_your_phone(phone_web):
    session, port, box = phone_web(replies=[{"role": "assistant", "content": "Your build passed."}])
    session.chat("tell me when the build is done")  # nobody has the page open
    deadline = time.time() + 5
    while time.time() < deadline and not box.sent:
        time.sleep(0.02)
    assert box.sent and box.sent[0][1] == "Your build passed."
    assert box.sent[0][2]["Title"] == "Jarvis replied"


def test_no_phone_alert_while_you_are_watching(phone_web):
    session, port, box = phone_web(replies=[{"role": "assistant", "content": "All good."}])
    session.hub.subscribe("tab1")
    session.hub.claim("tab1")
    session.set_visible("tab1", True)
    session.chat("how are things")
    deadline = time.time() + 2
    while time.time() < deadline and session.bot.attention.is_set():
        time.sleep(0.02)
    time.sleep(0.2)
    assert box.sent == []
