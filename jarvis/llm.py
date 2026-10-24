"""Thin client for a local Ollama server: streaming chat, tool calls, vision, embeddings."""

from __future__ import annotations

import json
from typing import Any, Callable

import requests


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, host: str, model: str, temperature: float = 0.6, timeout: int = 120):
        self.host = host.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    def models(self) -> set[str]:
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=5)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise OllamaError(
                f"Can't reach Ollama at {self.host}. Start it with 'ollama serve'."
            ) from exc
        return {m.get("name", "") for m in resp.json().get("models", [])}

    def check(self, *extra_models: str) -> None:
        """Fail early with a helpful message if Ollama or a model is missing."""
        names = self.models()
        for model in (self.model, *extra_models):
            if not model:
                continue
            base = model.split(":")[0]
            if model not in names and not any(n.split(":")[0] == base for n in names):
                raise OllamaError(f"Model '{model}' isn't pulled yet. Run 'ollama pull {model}'.")

    def _post(self, path: str, payload: dict, stream: bool = False) -> requests.Response:
        try:
            resp = requests.post(f"{self.host}{path}", json=payload, timeout=self.timeout, stream=stream)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise OllamaError(f"Ollama request failed: {exc}") from exc
        return resp

    def chat(self, messages: list[dict[str, Any]], tools: list[dict] | None = None) -> dict[str, Any]:
        """Send one chat turn and return the assistant message dict."""
        return self.chat_stream(messages, tools, on_text=None)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Stream a chat turn. Text pieces go to on_text as they arrive.

        Returns the complete assistant message, including any tool calls.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": self.temperature},
        }
        if tools:
            payload["tools"] = tools
        resp = self._post("/api/chat", payload, stream=True)
        content: list[str] = []
        tool_calls: list[dict] = []
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise OllamaError(chunk["error"])
            msg = chunk.get("message") or {}
            piece = msg.get("content") or ""
            if piece:
                content.append(piece)
                if on_text:
                    on_text(piece)
            tool_calls.extend(msg.get("tool_calls") or [])
            if chunk.get("done"):
                break
        out: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if tool_calls:
            out["tool_calls"] = tool_calls
        return out

    def vision(self, model: str, prompt: str, image_b64: str) -> str:
        """Ask a multimodal model (llava, qwen2.5vl, gemma3...) about one image."""
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
            "stream": False,
        }
        return self._post("/api/chat", payload).json().get("message", {}).get("content", "")

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = self._post("/api/embed", {"model": model, "input": texts}).json()
        return data.get("embeddings", [])
