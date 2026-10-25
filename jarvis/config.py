"""Configuration loading with sensible defaults."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "llm": {
        "host": "http://localhost:11434",
        "model": "llama3.1:8b",
        "temperature": 0.6,
        "history_turns": 12,
        "timeout": 300,
        "keep_alive": "30m",           # how long Ollama keeps the model loaded between questions
        # hybrid: cloud first, local Ollama as fallback. local: Ollama only. cloud: cloud only.
        "provider": "hybrid",
        "private": False,
        # Send tool descriptions to the local model. Off makes local replies much faster
        # on CPU-only laptops; instant answers still cover time, briefing and notes.
        "local_tools": True,
        "cloud": {
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-versatile",
            "api_key_env": "GROQ_API_KEY",
            "api_key": "",
            "timeout": 60,
        },
    },
    # Answer common requests (time, date, battery, disk, briefing, notes) without the model.
    "router": {
        "enabled": True,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
    },
    "stt": {
        "provider": "local",           # local, or cloud (Groq Whisper, falls back to local)
        "cloud": {
            "base_url": "https://api.groq.com/openai/v1",
            "model": "whisper-large-v3-turbo",
            "api_key_env": "GROQ_API_KEY",
            "timeout": 20,
        },
        "model": "base.en",
        "device": "auto",
        "compute_type": "int8",
        "language": "en",
    },
    "tts": {
        "engine": "piper",
        "voice": "voices/en_GB-alan-medium.onnx",
        "kokoro_model": "voices/kokoro-v1.0.int8.onnx",
        "kokoro_voices": "voices/voices-v1.0.bin",
        "kokoro_voice": "bm_george",
        "kokoro_lang": "en-gb",
        "speed": 1.1,
    },
    "audio": {
        "device": None,                # None = Windows default; or a number from jarvis --mic-test
        "sample_rate": 16000,
        "silence_threshold": 0.012,
        "silence_seconds": 1.0,
        "max_record_seconds": 15,
    },
    "wake_word": {
        "enabled": False,
        "model": "hey_jarvis",
        "threshold": 0.5,
    },
    "voice": {
        "barge_in": False,
        "barge_in_min_ms": 300,
        "vad_threshold": 0.5,
    },
    "brain": {
        "enabled": False,
        "folders": [],
        "db": "~/.jarvis/brain.sqlite",
        "embed_model": "nomic-embed-text",
        "doc_prefix": "search_document: ",
        "query_prefix": "search_query: ",
        "top_k": 4,
        "reindex_on_start": True,
    },
    "vision": {
        "enabled": False,
        "model": "qwen2.5vl:7b",
        "hotkey": "<ctrl>+<alt>+j",
        "max_width": 1600,
        "monitor": 1,
    },
    "mcp": {
        "servers": {},
    },
    "proactive": {
        "enabled": False,
        "quiet_hours": [22, 7],
        "briefing_at": "08:30",
        "battery": True,
        "battery_low_percent": 20,
        "disk": True,
        "disk_min_free_gb": 10,
        "calendar_ics": "",
        "meeting_lead_minutes": 10,
        "git_repos": [],
        "logs": [],
    },
    "tools": {
        "enabled": True,
        "confirm_commands": True,
        "command_timeout": 20,
        "allowed_commands": [
            "ls", "dir", "pwd", "whoami", "date", "uptime", "df", "free",
            "git", "python", "python3", "pip", "node", "npm", "ping", "echo",
            "cat", "type", "hostname", "systeminfo", "sw_vers", "uname",
        ],
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config.yaml (if present) on top of the defaults."""
    candidates = [Path(path)] if path else [Path("config.yaml")]
    for candidate in candidates:
        if candidate.is_file():
            with candidate.open("r", encoding="utf-8") as fh:
                return _merge(DEFAULTS, yaml.safe_load(fh) or {})
    if path:
        raise FileNotFoundError(f"Config file not found: {path}")
    return copy.deepcopy(DEFAULTS)
