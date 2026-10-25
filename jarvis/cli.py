"""Command line entry point: wires every optional feature into the Assistant."""

from __future__ import annotations

import argparse
import sys
import threading

from jarvis import __version__
from jarvis.assistant import Assistant
from jarvis.cloud import CloudError, build_llm
from jarvis.config import load_config
from jarvis.llm import OllamaClient, OllamaError
from jarvis.tools import Tool


def build_brain(cfg: dict, llm: OllamaClient):
    from jarvis.knowledge import KnowledgeBase

    b = cfg["brain"]
    return KnowledgeBase(
        b["db"], lambda texts: llm.embed(b["embed_model"], texts),
        doc_prefix=b.get("doc_prefix", ""), query_prefix=b.get("query_prefix", ""),
    )


def add_features(bot: Assistant, cfg: dict, llm: OllamaClient) -> list:
    """Register second brain, vision, briefing and MCP tools. Returns things to close."""
    closers = []
    box = bot.toolbox

    if cfg["brain"]["enabled"]:
        brain = build_brain(cfg, llm)
        k = int(cfg["brain"].get("top_k", 4))
        box.register(Tool(
            name="search_notes",
            description=(
                "Search the user's own notes, documents and code by meaning. Use whenever the user asks "
                "about something they wrote, saved, or worked on, or says 'my notes', 'my project', 'where did I'."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to look for, in plain words."}},
                "required": ["query"],
            },
            handler=lambda a: brain.answer_context(str(a.get("query", "")), k=k),
        ))
        if bot.router:
            from jarvis.router import add_notes_route

            add_notes_route(bot.router, brain.search)
        if cfg["brain"].get("reindex_on_start", True) and cfg["brain"]["folders"]:
            threading.Thread(
                target=brain.index, args=(cfg["brain"]["folders"],), daemon=True, name="brain-index"
            ).start()

    if cfg["vision"]["enabled"]:
        from jarvis.vision import ScreenVision

        bot.screen = ScreenVision(llm, cfg["vision"])
        box.register(Tool(
            name="look_at_screen",
            description="Take a screenshot of the user's screen and describe it. Use when they say 'look at this', "
                        "'what's on my screen', or ask about an error they are seeing.",
            parameters={
                "type": "object",
                "properties": {"question": {"type": "string", "description": "What to look for on screen."}},
            },
            handler=lambda a: bot.screen.look(str(a.get("question", ""))),
            announce=lambda a: "Taking a look at your screen.",
        ))
        hotkey = cfg["vision"].get("hotkey")
        if hotkey:
            try:
                from jarvis.vision import Hotkey

                Hotkey(hotkey, lambda: bot.post("screen")).start()
                print(f"  [vision] press {hotkey} to have Jarvis look at your screen")
            except Exception as exc:
                print(f"  [vision] hotkey unavailable: {exc}")

    if cfg["proactive"]["enabled"]:
        from jarvis.watchers import briefing_facts

        box.register(Tool(
            name="daily_briefing",
            description="Get today's facts for a briefing: meetings, git repos, disk. Use for 'brief me' or 'what's my day'.",
            handler=lambda a: briefing_facts(cfg["proactive"]),
        ))
        if bot.router:
            from jarvis.router import BRIEFING_PATTERN, briefing_route

            bot.router.add("briefing", BRIEFING_PATTERN, briefing_route(lambda: briefing_facts(cfg["proactive"])))

    servers = cfg["mcp"].get("servers") or {}
    if servers:
        from jarvis.mcp_client import MCPHub

        hub = MCPHub(servers)
        hub.start()
        print(f"  [mcp] {hub.register(box)} tools ready")
        closers.append(hub)

    return closers


def start_watchers(bot: Assistant, cfg: dict) -> None:
    from jarvis.watchers import Scheduler, briefing_facts, build_watchers

    p = cfg["proactive"]

    def briefing() -> None:
        bot.post("ask", "Give me my briefing for today in three to five short spoken sentences, "
                        "using these facts:\n" + briefing_facts(p))

    quiet = tuple(p["quiet_hours"]) if p.get("quiet_hours") else None
    Scheduler(build_watchers(p), lambda m: bot.post("say", m), quiet_hours=quiet,
              briefing_at=p.get("briefing_at"), on_briefing=briefing).start()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="Your local voice assistant.")
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("--text", action="store_true", help="Type instead of talk (no mic or speakers)")
    parser.add_argument("--speak", action="store_true", help="In text mode, still speak replies aloud")
    parser.add_argument("--web", action="store_true", help="Run Jarvis in your browser (recommended on modest laptops)")
    parser.add_argument("--port", type=int, help="Port for --web (default 8765)")
    parser.add_argument("--local", action="store_true", help="Keep all thinking on this computer (no cloud model)")
    parser.add_argument("--mic-test", action="store_true", help="Check your microphone: levels, speech detection, transcript")
    parser.add_argument("--wake", action="store_true", help="Wait for 'Hey Jarvis' before each request")
    parser.add_argument("--barge-in", action="store_true", help="Let yourself interrupt Jarvis (use headphones)")
    parser.add_argument("--index", nargs="*", metavar="FOLDER",
                        help="Index folders into the second brain, then exit (defaults to brain.folders)")
    parser.add_argument("--briefing", action="store_true", help="Give the daily briefing right after starting")
    parser.add_argument("--model", help="Override the Ollama model")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.model:
        cfg["llm"]["model"] = args.model
    if args.wake:
        cfg["wake_word"]["enabled"] = True
    if args.barge_in:
        cfg["voice"]["barge_in"] = True
    if args.local:
        cfg["llm"]["provider"] = "local"
    if args.mic_test:
        from jarvis import mictest

        mictest.run(cfg)
        return

    try:
        llm = build_llm(cfg)
    except CloudError as exc:
        sys.exit(str(exc))
    if hasattr(llm, "describe"):
        print(f"  [brain] {llm.describe()}")
    try:
        llm.check(
            cfg["brain"]["embed_model"] if cfg["brain"]["enabled"] or args.index is not None else "",
            cfg["vision"]["model"] if cfg["vision"]["enabled"] else "",
        )
    except OllamaError as exc:
        sys.exit(str(exc))

    if args.index is not None:
        folders = args.index or cfg["brain"]["folders"]
        if not folders:
            sys.exit("Nothing to index. Pass folders or set brain.folders in config.yaml.")
        build_brain(cfg, llm).index(folders)
        return

    if args.web:
        run_web(cfg, llm, args)
        return

    from jarvis.voice import Speaker

    if args.text:
        if args.speak:
            from jarvis.tts import build_voice
            speaker = Speaker(build_voice(cfg["tts"]))
        else:
            from jarvis.tts import PrintVoice
            speaker = Speaker(PrintVoice())

        def listen() -> str:
            return input("You: ")

        bot = Assistant(llm, speaker, listen, cfg)
        run(bot, cfg, llm, args)
        return

    print("Loading speech models, one moment...")
    from jarvis.audio import Microphone
    from jarvis.stt import build_transcriber
    from jarvis.tts import build_voice
    from jarvis.voice import BargeIn, Vad

    vad = Vad(threshold=float(cfg["voice"]["vad_threshold"]))
    mic = Microphone(cfg["audio"], vad=vad)
    ears = build_transcriber(cfg["stt"])
    barge = BargeIn(mic, vad, int(cfg["voice"]["barge_in_min_ms"])) if cfg["voice"]["barge_in"] else None
    speaker = Speaker(build_voice(cfg["tts"]), barge_in=barge)
    bot: Assistant

    def listen() -> str:
        print("  [listening]")
        prefix = barge.take_audio() if barge else None
        text = ears.transcribe(mic.record_utterance(prefix=prefix, stop=bot.attention))
        if text:
            print(f"You: {text}")
        return text

    bot = Assistant(llm, speaker, listen, cfg)

    wait = None
    if cfg["wake_word"]["enabled"]:
        from jarvis.wakeword import WakeWord

        wake = WakeWord(cfg["wake_word"])

        def wait(attention: threading.Event) -> bool:
            print("  [waiting for 'Hey Jarvis']")
            return wake.wait(mic.frames(stop=attention))

    run(bot, cfg, llm, args, wait)


def start_extras(bot: Assistant, cfg: dict, llm, args) -> list:
    closers = add_features(bot, cfg, llm)
    if cfg["proactive"]["enabled"]:
        start_watchers(bot, cfg)
    if args.briefing:
        from jarvis.watchers import briefing_facts

        bot.post("ask", "Give me my briefing for today in three to five short spoken sentences, "
                        "using these facts:\n" + briefing_facts(cfg["proactive"]))
    return closers


def run_web(cfg: dict, llm, args) -> None:
    from jarvis.server import WebSession, serve

    session = WebSession(llm, cfg)
    closers = start_extras(session.bot, cfg, llm, args)
    w = cfg["web"]
    try:
        httpd, _ = serve(session, w["host"], args.port or int(w["port"]), bool(w.get("open_browser", True)))
    except OSError as exc:
        sys.exit(f"Couldn't start the web server: {exc}. Is Jarvis already running? Try --port 8766.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
        for c in closers:
            c.close()


def run(bot: Assistant, cfg: dict, llm: OllamaClient, args, wait=None) -> None:
    closers = start_extras(bot, cfg, llm, args)
    try:
        bot.run(wait_for_wake=wait)
    except KeyboardInterrupt:
        print()
    finally:
        for c in closers:
            c.close()


if __name__ == "__main__":
    main()
