"""Turn exceptions into one spoken sentence that says what failed and what to do."""

from __future__ import annotations

import requests


def explain(exc: BaseException) -> str:
    text = str(exc)
    low = text.lower()
    name = type(exc).__name__
    if isinstance(exc, requests.Timeout) or "timed out" in low or "read timeout" in low:
        return "That took too long, so I stopped waiting. Try again, or ask something shorter."
    if name == "CloudError":
        if "401" in text or "403" in text or "invalid api key" in low:
            return "The cloud key was rejected. Make a new Groq key and save it again."
        if "429" in text or "rate limit" in low:
            return "I've hit the cloud's free limit for now. Give it a minute, or switch on Private mode."
        if "can't reach" in low or "connection" in low:
            return "I can't reach the cloud. Check your internet, or switch on Private mode."
        return "The cloud brain had a problem. Try again in a moment."
    if name == "OllamaError":
        if "can't reach" in low:
            return "My local brain isn't running. Open the Ollama app and try again."
        if "isn't pulled" in low or "not found" in low:
            return "The local model isn't downloaded yet. Run jarvis --doctor to see the fix."
        return "My local brain had a problem. Run jarvis --doctor to check it."
    return "Something went wrong on my end. Run jarvis --doctor to check what's up."
