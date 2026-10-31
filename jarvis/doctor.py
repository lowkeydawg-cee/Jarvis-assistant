"""Health checks: what's working, what isn't, and the exact fix for each problem.

`jarvis --doctor` prints these in the terminal, and the browser app shows the
same list in its Health panel. Most of a first install goes on finding these
problems by hand, so every failed check carries a concrete next step.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str          # ok, warn or fail
    detail: str          # what was found, in plain words
    fix: str = ""        # what to do about it, empty when nothing is needed

    def to_dict(self) -> dict:
        return asdict(self)


def _python() -> Check:
    v = sys.version_info
    text = f"Python {v.major}.{v.minor}.{v.micro}"
    if v < (3, 10):
        return Check("Python", FAIL, text, "Install Python 3.10 or newer from python.org.")
    return Check("Python", OK, text)


def _ollama(cfg: dict, cloud_ready: bool) -> list[Check]:
    c = cfg["llm"]
    host = c["host"].rstrip("/")
    needed = [("Local model", c["model"])]
    if cfg["brain"]["enabled"]:
        needed.append(("Notes model", cfg["brain"]["embed_model"]))
    if cfg["vision"]["enabled"]:
        needed.append(("Vision model", cfg["vision"]["model"]))
    # Without a cloud brain, Ollama is the only brain, so it failing is fatal.
    level = WARN if cloud_ready and len(needed) == 1 else FAIL
    try:
        resp = requests.get(f"{host}/api/tags", timeout=3)
        resp.raise_for_status()
        names = {m.get("name", "") for m in resp.json().get("models", [])}
    except (requests.RequestException, ValueError):
        return [Check("Ollama", level, f"Not running at {host}.",
                      "Open the Ollama app from the Start menu, or run: ollama serve")]
    out = [Check("Ollama", OK, f"Running, {len(names)} models installed.")]
    for label, model in needed:
        base = model.split(":")[0]
        if model in names or any(n.split(":")[0] == base and ":" not in model for n in names):
            out.append(Check(label, OK, f"{model} is installed."))
        else:
            out.append(Check(label, level if label == "Local model" else FAIL,
                             f"{model} isn't downloaded yet.", f"Run: ollama pull {model}"))
    return out


def _cloud(cfg: dict) -> Check:
    c = cfg["llm"]
    if str(c.get("provider", "hybrid")).lower() == "local":
        return Check("Cloud brain", OK, "Turned off. Everything runs on this computer.")
    cc = c.get("cloud") or {}
    env = cc.get("api_key_env") or "GROQ_API_KEY"
    key = cc.get("api_key") or os.environ.get(env, "")
    if not key:
        return Check("Cloud brain", WARN, f"No {env} set, so answers come from the slower local model.",
                     "Get a free key at console.groq.com/keys, then follow docs/SETUP-KEYS.md.")
    try:
        resp = requests.get(f"{cc['base_url'].rstrip('/')}/models",
                            headers={"Authorization": f"Bearer {key}"}, timeout=6)
    except requests.RequestException:
        return Check("Cloud brain", WARN, "Couldn't reach the cloud. Are you online?",
                     "Check your internet connection. Jarvis uses the local model meanwhile.")
    if resp.status_code in (401, 403):
        return Check("Cloud brain", FAIL, "The API key was rejected.",
                     "Make a new key at console.groq.com/keys and save it again (docs/SETUP-KEYS.md).")
    if resp.status_code >= 400:
        return Check("Cloud brain", WARN, f"The cloud answered with error {resp.status_code}.",
                     "Try again in a minute. Jarvis uses the local model meanwhile.")
    model = cc.get("model", "")
    try:
        available = [m.get("id", "") for m in resp.json().get("data", [])]
    except ValueError:
        available = []
    if model and available and model not in available:
        # Providers retire models (Groq dropped Llama 3.3 70B): the key works but every answer fails.
        chat = [m for m in available if not any(w in m for w in ("whisper", "guard", "orpheus", "tts"))]
        suggestion = next((m for m in ("openai/gpt-oss-120b", "qwen/qwen3.8-27b") if m in available), chat[0] if chat else "")
        return Check("Cloud brain", FAIL, f"Key accepted, but the model {model} isn't available any more.",
                     f"In config.yaml under llm: cloud: set model: {suggestion}" if suggestion else
                     "Pick a model listed at console.groq.com/docs/models and set it in config.yaml.")
    return Check("Cloud brain", OK, f"Key accepted, using {model or 'the cloud model'}.")


def _disk() -> Check:
    free_gb = shutil.disk_usage(Path.home()).free / 1024 ** 3
    if free_gb < 2:
        return Check("Disk space", FAIL, f"Only {free_gb:.1f} GB free.", "Free some space, or move models to another drive.")
    if free_gb < 8:
        return Check("Disk space", WARN, f"{free_gb:.0f} GB free, which is tight for AI models.",
                     "Free some space, or move models to another drive.")
    return Check("Disk space", OK, f"{free_gb:.0f} GB free.")


def _folders(cfg: dict) -> list[Check]:
    if not cfg["brain"]["enabled"]:
        return []
    missing = [f for f in cfg["brain"]["folders"] if not Path(f).expanduser().is_dir()]
    if not cfg["brain"]["folders"]:
        return [Check("Notes folders", WARN, "Notes search is on, but no folders are listed.",
                      "Add your notes folders under brain.folders in config.yaml.")]
    if missing:
        return [Check("Notes folders", WARN, f"Can't find: {', '.join(missing)}",
                      "Fix the paths under brain.folders in config.yaml.")]
    return [Check("Notes folders", OK, f"{len(cfg['brain']['folders'])} folders found.")]


def _microphone() -> Check:
    try:
        import sounddevice as sd
    except Exception:
        return Check("Microphone", OK, "Handled by the browser in the web app.")
    try:
        dev = sd.query_devices(kind="input")
        return Check("Microphone", OK, f"Default input: {dev['name']}.")
    except Exception:
        return Check("Microphone", WARN, "No microphone found for terminal voice mode.",
                     "Plug one in or allow microphone access in Windows privacy settings, then run jarvis --mic-test.")


def port_free(host: str, port: int) -> Check:
    with socket.socket() as s:
        s.settimeout(0.5)
        busy = s.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) == 0
    if busy:
        return Check("Web port", WARN, f"Port {port} is already in use, maybe by another Jarvis.",
                     "Close the other Jarvis window, or start with --port 8766.")
    return Check("Web port", OK, f"Port {port} is free.")


def _phone(cfg: dict) -> list[Check]:
    """Only shown once the phone is switched on: is Tailscale ready to carry it?"""
    from jarvis.phone import Phone

    phone = Phone(cfg["web"], cfg["data_dir"])
    if not phone.enabled:
        return []
    port = int(cfg["web"]["port"])
    ts = phone.tailscale()
    if not ts["installed"]:
        return [Check("Phone", WARN, "Phone access is on, but Tailscale isn't installed.",
                      "Install Tailscale from tailscale.com/download on the laptop and the phone, and sign in on both.")]
    if not ts["running"]:
        return [Check("Phone", WARN, "Tailscale is installed but not connected.", "Open Tailscale and sign in.")]
    if not ts["serving"]:
        return [Check("Phone", WARN, "Tailscale is connected but isn't passing Jarvis to your phone yet.",
                      f"Run this once in PowerShell: tailscale serve --bg {port}")]
    return [Check("Phone", OK, f"Your phone can reach Jarvis at https://{ts['name']}")]


def _alerts(cfg: dict) -> list[Check]:
    from jarvis.notify import Notifier

    n = Notifier(cfg["notify"], cfg["data_dir"])
    if not n.enabled:
        return []
    return [Check("Phone alerts", OK, f"Sending to {n.subscribe_url}. Subscribe to it in the ntfy app.")]


def run_checks(cfg: dict, include_port: bool = False) -> list[Check]:
    cloud = _cloud(cfg)
    cloud_ready = cloud.status == OK and str(cfg["llm"].get("provider", "hybrid")).lower() != "local"
    checks = [_python(), cloud, *_ollama(cfg, cloud_ready), _disk(), *_folders(cfg), _microphone(),
              *_phone(cfg), *_alerts(cfg)]
    if include_port:
        checks.append(port_free(cfg["web"]["host"], int(cfg["web"]["port"])))
    return checks


def summary(checks: list[Check]) -> str:
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    if fails:
        return f"{fails} problem{'s' if fails > 1 else ''} to fix" + (f", {warns} to look at" if warns else "")
    if warns:
        return f"Working, {warns} thing{'s' if warns > 1 else ''} to look at"
    return "Everything looks good"


def print_report(checks: list[Check]) -> int:
    """Print the checks for a terminal. Returns an exit code: 1 if anything failed."""
    marks = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
    print(f"\nJarvis health check  ({platform.system()} {platform.release()})\n")
    for c in checks:
        print(f"  [{marks[c.status]}] {c.name}: {c.detail}")
        if c.fix:
            print(f"         Fix: {c.fix}")
    print(f"\n  {summary(checks)}.\n")
    return 1 if any(c.status == FAIL for c in checks) else 0
