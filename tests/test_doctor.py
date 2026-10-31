import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from jarvis import doctor
from jarvis.cloud import CloudError
from jarvis.config import DEFAULTS, _merge
from jarvis.errors import explain
from jarvis.llm import OllamaError
from jarvis.vitals import vitals


class Fake(BaseHTTPRequestHandler):
    """Plays Ollama (/api/tags) and the cloud (/v1/models) at once."""

    models = ["llama3.2:3b", "nomic-embed-text:latest"]
    cloud_status = 200
    cloud_models = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "whisper-large-v3"]

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/tags":
            body, code = {"models": [{"name": n} for n in type(self).models]}, 200
        else:
            code = type(self).cloud_status
            body = {"data": [{"id": m} for m in type(self).cloud_models]} if code == 200 else {"error": "nope"}
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake():
    Fake.models, Fake.cloud_status = ["llama3.2:3b", "nomic-embed-text:latest"], 200
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def cfg_for(base, **over):
    c = _merge(DEFAULTS, {"llm": {"host": base, "model": "llama3.2:3b",
                                  "cloud": {"base_url": base + "/v1", "api_key": "k"}}})
    return _merge(c, over)


def by_name(checks):
    return {c.name: c for c in checks}


def test_all_good(fake):
    checks = doctor.run_checks(cfg_for(fake))
    names = by_name(checks)
    assert names["Ollama"].status == "ok" and names["Local model"].status == "ok"
    assert names["Cloud brain"].status == "ok"
    assert doctor.summary(checks) in {"Everything looks good", "Working, 1 thing to look at"}


def test_missing_model_says_how_to_pull(fake):
    Fake.models = ["llama3.1:8b"]
    c = by_name(doctor.run_checks(cfg_for(fake)))["Local model"]
    assert c.status == "warn" and c.fix == "Run: ollama pull llama3.2:3b"  # cloud works, so only a warning


def test_retired_cloud_model_is_caught_with_a_replacement(fake):
    # The key works, but Groq retired the model, so every answer silently fell back to the slow local one.
    old = cfg_for(fake, llm={"cloud": {"model": "llama-3.3-70b-versatile"}})
    c = by_name(doctor.run_checks(old))["Cloud brain"]
    assert c.status == "fail" and "isn't available" in c.detail
    assert c.fix == "In config.yaml under llm: cloud: set model: openai/gpt-oss-120b"


def test_rejected_key_and_missing_key(fake, monkeypatch):
    Fake.cloud_status = 401
    c = by_name(doctor.run_checks(cfg_for(fake)))["Cloud brain"]
    assert c.status == "fail" and "rejected" in c.detail
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    no_key = cfg_for(fake, llm={"cloud": {"api_key": ""}})
    c = by_name(doctor.run_checks(no_key))["Cloud brain"]
    assert c.status == "warn" and "console.groq.com" in c.fix


def test_ollama_down_is_fatal_only_without_cloud(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    cfg = _merge(DEFAULTS, {"llm": {"host": "http://127.0.0.1:9", "provider": "local"}})
    checks = doctor.run_checks(cfg)
    assert by_name(checks)["Ollama"].status == "fail"
    assert "Ollama app" in by_name(checks)["Ollama"].fix
    assert doctor.print_report(checks) == 1


def test_notes_model_checked_when_brain_on(fake, tmp_path):
    Fake.models = ["llama3.2:3b"]
    cfg = cfg_for(fake, brain={"enabled": True, "folders": [str(tmp_path), str(tmp_path / "nope")]})
    names = by_name(doctor.run_checks(cfg))
    assert names["Notes model"].status == "fail" and "nomic-embed-text" in names["Notes model"].fix
    assert names["Notes folders"].status == "warn" and "nope" in names["Notes folders"].detail


@pytest.mark.parametrize("exc, words", [
    (CloudError("Cloud model returned 401: invalid"), "key was rejected"),
    (CloudError("Cloud model returned 429: rate limit"), "free limit"),
    (CloudError("Can't reach the cloud model: boom"), "internet"),
    (OllamaError("Can't reach Ollama at http://x. Start it"), "Ollama app"),
    (OllamaError("Model 'x' isn't pulled yet."), "isn't downloaded"),
    (requests.Timeout("read timeout"), "took too long"),
    (ValueError("weird"), "jarvis --doctor"),
])
def test_errors_are_explained(exc, words):
    assert words in explain(exc)


def test_vitals_shape():
    v = vitals()
    assert v["disk"]["free_gb"] > 0 and 0 <= v["disk"]["percent"] <= 100
    assert v["cpu"] is None or 0 <= v["cpu"] <= 100


def test_web_serves_system_and_health(fake):
    from jarvis.server import WebSession, serve

    class LLM:
        model = "fake"

        def chat_stream(self, *a, **k):
            return {"role": "assistant", "content": "hi"}

    session = WebSession(LLM(), cfg_for(fake))
    httpd, _ = serve(session, "127.0.0.1", 0, open_browser=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        system = json.loads(urllib.request.urlopen(base + "/api/system", timeout=5).read())
        health = json.loads(urllib.request.urlopen(base + "/api/health", timeout=10).read())
    finally:
        httpd.shutdown()
    assert "disk" in system
    assert health["summary"] and any(c["name"] == "Ollama" for c in health["checks"])
