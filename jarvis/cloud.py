"""Cloud brain and the hybrid router that falls back to local Ollama.

Any provider that speaks the OpenAI chat completions API works: Groq (fast and
has a free tier), OpenRouter, Together, OpenAI, or Claude through Anthropic's
OpenAI-compatible endpoint. The rest of Jarvis talks to every brain through the
same three methods as OllamaClient, so the assistant loop never knows which
one answered.

Embeddings and vision always stay on the local Ollama, so your notes and
screenshots never leave the machine.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

import requests

from jarvis.llm import OllamaClient, OllamaError


class CloudError(RuntimeError):
    pass


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Jarvis's Ollama-shaped history into OpenAI chat format.

    Ollama tool calls have no ids and dict arguments; OpenAI needs ids on both
    the call and its result, and arguments as a JSON string. Tool results are
    matched to calls in order, which is how both APIs return them.
    """
    out: list[dict[str, Any]] = []
    pending: list[str] = []
    counter = 0
    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            calls = []
            pending = []
            for call in msg["tool_calls"]:
                counter += 1
                call_id = call.get("id") or f"call_{counter}"
                fn = call.get("function", {})
                args = fn.get("arguments") or {}
                calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": args if isinstance(args, str) else json.dumps(args),
                    },
                })
                pending.append(call_id)
            out.append({"role": "assistant", "content": msg.get("content") or None, "tool_calls": calls})
        elif role == "tool":
            call_id = msg.get("tool_call_id") or (pending.pop(0) if pending else None)
            if call_id is None:
                # A result with no call in view; keep the information without breaking the API.
                out.append({"role": "user", "content": f"(Tool result) {msg.get('content', '')}"})
                continue
            if call_id in pending:
                pending.remove(call_id)
            out.append({"role": "tool", "tool_call_id": call_id, "content": msg.get("content", "")})
        else:
            out.append({"role": role, "content": msg.get("content", "")})
    return out


class OpenAICompatClient:
    """Streaming chat with tool calls against any OpenAI-compatible API."""

    def __init__(self, base_url: str, api_key: str, model: str, temperature: float = 0.6, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    def chat(self, messages, tools=None):
        return self.chat_stream(messages, tools, on_text=None)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "stream": True,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            raise CloudError(f"Can't reach the cloud model: {exc}") from exc
        if resp.status_code >= 400:
            detail = resp.text[:300].strip()
            raise CloudError(f"Cloud model returned {resp.status_code}: {detail}")

        content: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        # The stream is UTF-8 but often declares no charset, and requests then assumes Latin-1,
        # which turned every dash and accent into garbage ("—" became "â€”").
        resp.encoding = "utf-8"
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise CloudError(str(chunk["error"]))
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    content.append(piece)
                    if on_text:
                        on_text(piece)
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""

        out: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if calls:
            tool_calls = []
            for _, slot in sorted(calls.items()):
                try:
                    args = json.loads(slot["args"]) if slot["args"] else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({"id": slot["id"], "function": {"name": slot["name"], "arguments": args}})
            out["tool_calls"] = tool_calls
        return out


class HybridLLM:
    """Cloud first for speed, local Ollama when private mode is on or the cloud fails.

    Exposes the same interface as OllamaClient, so it drops straight into the
    Assistant. Embeddings and vision always go to the local model.
    """

    def __init__(self, local: OllamaClient, cloud: OpenAICompatClient | None, private: bool = False,
                 log: Callable[[str], None] = print, local_tools: bool = True):
        self.local = local
        self.local_tools = local_tools
        self.cloud = cloud
        self.private = private
        self.log = log
        self.last_provider = ""

    @property
    def model(self) -> str:
        return self.cloud.model if self.cloud and not self.private else self.local.model

    def describe(self) -> str:
        if self.cloud and not self.private:
            return f"cloud ({self.cloud.model}), local fallback ({self.local.model})"
        return f"local ({self.local.model})"

    def check(self, *extra_models: str) -> None:
        """Local models must exist for whatever needs them; the chat model may live in the cloud."""
        if self.cloud is None:
            self.local.check(*extra_models)
            return
        try:
            self.local.check(*extra_models)
        except OllamaError as exc:
            if any(extra_models):
                raise  # embeddings or vision need Ollama, so this is a real problem
            self.log(f"  [hybrid] local fallback unavailable: {exc}")

    def chat(self, messages, tools=None):
        return self.chat_stream(messages, tools, on_text=None)

    def chat_stream(self, messages, tools=None, on_text=None):
        if self.cloud and not self.private:
            started = False

            def relay(piece: str) -> None:
                nonlocal started
                started = True
                if on_text:
                    on_text(piece)

            try:
                msg = self.cloud.chat_stream(messages, tools, relay)
                self.last_provider = "cloud"
                return msg
            except CloudError as exc:
                if started:
                    raise  # half a reply was already spoken; don't start over in another voice
                self.log(f"  [hybrid] cloud failed, using local model: {exc}")
        if not self.local_tools:
            # Without tools, the full persona (all about tools) only slows a small model and confuses it.
            from jarvis.persona import lite_system_prompt

            if messages and messages[0].get("role") == "system":
                messages = [{"role": "system", "content": lite_system_prompt()}, *messages[1:]]
            tools = None
        msg = self.local.chat_stream(messages, tools, on_text)
        self.last_provider = "local"
        return msg

    def warm(self) -> None:
        if not self.cloud or self.private:
            self.local.warm()

    def vision(self, model, prompt, image_b64):
        return self.local.vision(model, prompt, image_b64)

    def embed(self, model, texts):
        return self.local.embed(model, texts)


def build_llm(cfg: dict, log: Callable[[str], None] = print):
    """Build the brain from config: local, cloud, or hybrid (the default when a key is set)."""
    c = cfg["llm"]
    local = OllamaClient(c["host"], c["model"], c["temperature"], timeout=int(c.get("timeout", 300)),
                         keep_alive=str(c.get("keep_alive", "30m")))
    provider = str(c.get("provider", "hybrid")).lower()
    local_tools = bool(c.get("local_tools", True))
    if provider == "local":
        return local if local_tools else HybridLLM(local, None, log=log, local_tools=False)
    cc = c.get("cloud") or {}
    key = cc.get("api_key") or os.environ.get(cc.get("api_key_env") or "", "")
    if not key:
        if provider == "cloud":
            raise CloudError(f"No API key. Set {cc.get('api_key_env') or 'llm.cloud.api_key'} and try again.")
        log(f"  [hybrid] no {cc.get('api_key_env') or 'API key'} set, so everything runs locally")
        return HybridLLM(local, None, log=log, local_tools=local_tools)
    cloud = OpenAICompatClient(cc["base_url"], key, cc["model"], c["temperature"],
                               timeout=int(cc.get("timeout", 60)))
    return HybridLLM(local, cloud, private=bool(c.get("private", False)), log=log, local_tools=local_tools)
