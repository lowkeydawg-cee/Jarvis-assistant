# Setup notes: first attempt on Saint's laptop (2026-10-24)

Machine: Windows 11, Python 3.14, 8 GB RAM, Intel UHD 620 (no usable GPU), Ollama on CPU.

## What worked
- `pip install -e ".[all,dev]"` on Python 3.14, all 30 tests pass.
- Ollama with llama3.2 (3B), nomic-embed-text; notes index built (99 files, 575 chunks).
- Text mode answers simple questions and gives the briefing.
- Filesystem MCP server via npx (limited to 3 tools with `allow_tools`).

## What didn't
- Replies are very slow (1-5 min). With 14 MCP tool schemas in the prompt, requests
  passed the old 120 s timeout; `llm.timeout` is now configurable (set to 300 here).
- Another app (catalog_agent, qwen2.5vl:3b) shared Ollama and the 8 GB of RAM, making it worse.
- Voice mode: in Saint's test it didn't pick up their voice reliably.
- C: drive ran low; Jarvis data moved to `D:\jarvis-data` (HF_HOME, brain db, Ollama models copy).
  The Ollama app's own "Model location" setting still points at C: and overrides OLLAMA_MODELS.

## Ideas for next time
- Use a smaller/faster model (llama3.2:1b, qwen2.5:1.5b) or a remote/cloud model for the brain.
- Fewer tools in the prompt; disable MCP and the second brain for voice mode.
- Check the mic: input device selection, `audio.silence_threshold`, `stt.model: tiny.en`.
