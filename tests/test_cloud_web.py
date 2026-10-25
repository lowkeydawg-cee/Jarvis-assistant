import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jarvis.cloud import CloudError, HybridLLM, OpenAICompatClient, build_llm, to_openai_messages
from jarvis.config import DEFAULTS, _merge
from jarvis.llm import OllamaClient
from jarvis.server import WebSession, serve


def sse(chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


class FakeOpenAI(BaseHTTPRequestHandler):
    """Streams scripted OpenAI-style chunks and records each request body."""

    script: list = []
    seen: list = []
    status = 200

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.headers.get("Authorization"), body))
        if type(self).status != 200:
            self.send_response(type(self).status)
            self.end_headers()
            self.wfile.write(b'{"error":"rate limited"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(sse(type(self).script.pop(0)).encode())


@pytest.fixture
def openai_server():
    FakeOpenAI.script, FakeOpenAI.seen, FakeOpenAI.status = [], [], 200
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


def test_history_converts_to_openai_shape():
    history = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "disk?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "system_info", "arguments": {}}},
            {"id": "abc", "function": {"name": "run_command", "arguments": {"command": "df"}}},
        ]},
        {"role": "tool", "content": "50 GB free", "tool_name": "system_info"},
        {"role": "tool", "content": "exit code 0", "tool_name": "run_command", "tool_call_id": "abc"},
        {"role": "tool", "content": "orphan"},
    ]
    out = to_openai_messages(history)
    calls = out[2]["tool_calls"]
    assert calls[0]["id"] == "call_1" and calls[1]["id"] == "abc"
    assert calls[1]["function"]["arguments"] == '{"command": "df"}'
    assert out[2]["content"] is None
    assert out[3] == {"role": "tool", "tool_call_id": "call_1", "content": "50 GB free"}
    assert out[4]["tool_call_id"] == "abc"
    assert out[5]["role"] == "user"  # orphan result kept as plain context
    assert "tool_name" not in json.dumps(out)


def test_cloud_streams_text_and_tool_calls(openai_server):
    FakeOpenAI.script = [[
        {"choices": [{"delta": {"content": "Checking "}}]},
        {"choices": [{"delta": {"content": "now."}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "run_", "arguments": '{"comm'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "command", "arguments": 'and": "df"}'}}]}}]},
    ]]
    client = OpenAICompatClient(openai_server, "sk-test", "fast-model")
    pieces = []
    msg = client.chat_stream([{"role": "user", "content": "hi"}], tools=[{"type": "function"}], on_text=pieces.append)
    assert pieces == ["Checking ", "now."]
    assert msg["content"] == "Checking now."
    assert msg["tool_calls"] == [{"id": "c1", "function": {"name": "run_command", "arguments": {"command": "df"}}}]
    auth, body = FakeOpenAI.seen[0]
    assert auth == "Bearer sk-test"
    assert body["model"] == "fast-model" and body["stream"] is True and body["tools"]


class FakeLocal:
    model = "llama3.2:3b"

    def __init__(self, text="Local here."):
        self.text, self.calls = text, 0

    def chat_stream(self, messages, tools=None, on_text=None):
        self.calls += 1
        on_text and on_text(self.text)
        return {"role": "assistant", "content": self.text}

    def embed(self, model, texts):
        return [[1.0] for _ in texts]


def test_hybrid_falls_back_to_local_when_cloud_fails(openai_server):
    FakeOpenAI.status = 429
    local = FakeLocal()
    logs = []
    llm = HybridLLM(local, OpenAICompatClient(openai_server, "k", "m"), log=logs.append)
    out = []
    msg = llm.chat_stream([{"role": "user", "content": "hi"}], on_text=out.append)
    assert msg["content"] == "Local here." and out == ["Local here."]
    assert llm.last_provider == "local" and "429" in logs[0]


def test_hybrid_uses_cloud_then_private_mode_stays_local(openai_server):
    FakeOpenAI.script = [[{"choices": [{"delta": {"content": "Cloud here."}}]}]]
    local = FakeLocal()
    llm = HybridLLM(local, OpenAICompatClient(openai_server, "k", "m"))
    assert llm.chat([{"role": "user", "content": "hi"}])["content"] == "Cloud here."
    assert llm.last_provider == "cloud" and local.calls == 0
    llm.private = True
    assert llm.chat([{"role": "user", "content": "secret"}])["content"] == "Local here."
    assert len(FakeOpenAI.seen) == 1  # the private question never reached the cloud
    assert llm.embed("nomic", ["x"]) == [[1.0]]  # embeddings always local


def test_build_llm_picks_provider(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    cfg = _merge(DEFAULTS, {})
    assert isinstance(build_llm(cfg, log=lambda m: None), HybridLLM)
    assert build_llm(cfg, log=lambda m: None).cloud is None
    assert isinstance(build_llm(_merge(DEFAULTS, {"llm": {"provider": "local"}})), OllamaClient)
    with pytest.raises(CloudError):
        build_llm(_merge(DEFAULTS, {"llm": {"provider": "cloud"}}))
    monkeypatch.setenv("GROQ_API_KEY", "gsk")
    llm = build_llm(cfg)
    assert llm.cloud.api_key == "gsk" and "groq" in llm.cloud.base_url


# ---- the web server -------------------------------------------------------

class ScriptedLLM:
    model = "fake"
    last_provider = "cloud"

    def __init__(self, replies):
        self.replies = list(replies)

    def chat_stream(self, messages, tools=None, on_text=None):
        msg = self.replies.pop(0)
        on_text and msg.get("content") and on_text(msg["content"])
        return msg


@pytest.fixture
def web():
    started = []

    def start(replies, cfg_over=None):
        cfg = _merge(DEFAULTS, cfg_over or {})
        session = WebSession(ScriptedLLM(replies), cfg)
        httpd, _ = serve(session, "127.0.0.1", 0, open_browser=False)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return session, f"http://127.0.0.1:{httpd.server_port}"

    yield start
    for h in started:
        h.shutdown()


def call(base, path, body=None, headers=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read()


class Events:
    """Reads the SSE stream on a thread so the test can wait for events."""

    def __init__(self, base):
        self.items = []
        self.resp = urllib.request.urlopen(base + "/api/events", timeout=10)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.resp:
            line = line.decode().strip()
            if line.startswith("data:"):
                self.items.append(json.loads(line[5:]))

    def wait_for(self, pred, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.items:
                if pred(e):
                    return e
            time.sleep(0.02)
        raise AssertionError(f"event never arrived; got {self.items}")


def test_web_serves_page_and_status(web):
    _, base = web([])
    status, page = call(base, "/")
    assert status == 200 and b"JARVIS" in page and b"SpeechRecognition" in page
    data = json.loads(call(base, "/api/status")[1])
    assert "run_command" in data["tools"] and data["version"]


def test_web_chat_streams_sentences_back(web):
    session, base = web([{"role": "assistant", "content": "All good. Disk has plenty of room."}])
    ev = Events(base)
    time.sleep(0.1)
    call(base, "/api/chat", {"text": "how's my disk"})
    ev.wait_for(lambda e: e.get("text") == "All good.")
    ev.wait_for(lambda e: e.get("text") == "Disk has plenty of room.")
    done = ev.wait_for(lambda e: e.get("type") == "state" and e["state"] == "idle")
    assert done["provider"] == "cloud"
    assert session.bot.history[0] == {"role": "user", "content": "how's my disk"}


def test_web_asks_before_running_a_command(web, monkeypatch):
    monkeypatch.setattr("jarvis.tools.run_command", lambda argv, timeout=20: "exit code 0\nok")
    replies = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {
            "name": "run_command", "arguments": {"command": "git status", "reason": "Checking your repo."}}}]},
        {"role": "assistant", "content": "Your repo is clean."},
    ]
    session, base = web(replies)
    ev = Events(base)
    time.sleep(0.1)
    call(base, "/api/chat", {"text": "git status please"})
    ev.wait_for(lambda e: e.get("text") == "Checking your repo.")
    ev.wait_for(lambda e: e.get("type") == "confirm")
    call(base, "/api/chat", {"text": "yes"})  # a spoken yes routes to the pending question
    ev.wait_for(lambda e: e.get("text") == "Your repo is clean.")
    tool_msg = next(m for m in session.bot.history if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1" and "ok" in tool_msg["content"]


def test_web_rejects_other_sites(web):
    _, base = web([])
    with pytest.raises(urllib.error.HTTPError) as err:
        call(base, "/api/chat", {"text": "hi"}, headers={"Origin": "https://evil.example"})
    assert err.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as err:
        call(base, "/api/status", headers={"Host": "evil.example:80"})
    assert err.value.code == 403


def test_web_private_switch(web):
    session, base = web([])
    session.llm.private = False
    call(base, "/api/settings", {"private": True})
    assert session.llm.private is True


# ---- quick-answer router --------------------------------------------------

from datetime import datetime as _dt

from jarvis.assistant import Assistant
from jarvis.router import QuickRouter


def router():
    return QuickRouter(clock=lambda: _dt(2026, 10, 25, 9, 5), disk=lambda: "Plenty of disk.",
                       battery=lambda: "Eighty percent.")


@pytest.mark.parametrize("said, expected", [
    ("What time is it?", "It's 9 oh 5 in the morning."),
    ("Hey Jarvis, what's the time", "It's 9 oh 5 in the morning."),
    ("what's the date today", "It's Sunday, the 25th of October."),
    ("What day is it?", "It's Sunday, the 25th of October."),
    ("how much disk space do I have", "Plenty of disk."),
    ("How's my storage?", "Plenty of disk."),
    ("battery", "Eighty percent."),
    ("what's my battery level", "Eighty percent."),
])
def test_router_answers_simple_requests(said, expected):
    assert router().answer(said) == expected


@pytest.mark.parametrize("said", [
    "what time is the standup meeting",
    "why is my disk filling up so fast",
    "write a poem about the date we met",
    "how do I fix the battery drain on my phone",
    "",
])
def test_router_leaves_real_questions_to_the_model(said):
    assert router().answer(said) is None


def test_assistant_uses_router_before_the_model():
    class NoLLM:
        def chat_stream(self, *a, **k):
            raise AssertionError("the model should not be called")

    said = []

    class Speaker:
        def feed(self, s): said.append(s)
        def say(self, s): said.append(s)
        def wait(self): return False

    bot = Assistant(NoLLM(), Speaker(), lambda: "", DEFAULTS)
    bot.router = router()
    assert bot.respond("what time is it") == "It's 9 oh 5 in the morning."
    assert said == ["It's 9 oh 5 in the morning."] and bot.last_source == "instant"
    assert bot.history[-1] == {"role": "assistant", "content": "It's 9 oh 5 in the morning."}
