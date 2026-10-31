"""Command line entry point: wires every optional feature into the Assistant."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

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


def day_facts(bot: Assistant, cfg: dict) -> str:
    """Facts for the briefing: the watchers' (time, repos, disk) plus today's schedule."""
    from jarvis.watchers import briefing_facts

    facts = briefing_facts(cfg["proactive"])
    if bot.online is not None and bot.online.city:
        try:
            facts += "\n" + bot.online.weather_text()
        except Exception:
            pass  # offline: the briefing goes ahead without the weather
    if bot.skills is not None:
        facts += "\n" + bot.skills.schedule("today")
    return facts


def add_features(bot: Assistant, cfg: dict, llm: OllamaClient) -> list:
    """Register second brain, vision, briefing and MCP tools. Returns things to close."""
    closers = []
    box = bot.toolbox
    data = Path(cfg["data_dir"]).expanduser()

    from jarvis.memory import Memory

    bot.memory = Memory(data / "memory.json")
    bot.owner_name = bot.owner_name or cfg["user"].get("name", "")
    if cfg["reminders"]["enabled"]:
        from jarvis.reminders import ReminderService, ReminderStore, announcement
        from jarvis.skills import Skills

        store = ReminderStore(data / "reminders.json")

        def fire(r, late):
            text = announcement(r, late)
            bot.post("say", text)
            if bot.on_reminder:
                bot.on_reminder(r, text)
            if bot.notifier is not None:
                bot.notifier.send("reminders", r.text, title="Reminder" if r.kind == "reminder" else "Coming up")

        service = ReminderService(store, fire)
        bot.skills = Skills(store, bot.memory, service)
        if bot.router:
            bot.skills.add_routes(bot.router)
        for tool in bot.skills.tools():
            box.register(tool)
        service.start()

    if cfg["online"]["enabled"]:
        from jarvis.online import Online, add_online_routes, online_tools

        o = cfg["online"]
        bot.online = Online(data / "online.json", city=o.get("city", ""), news_feed=o.get("news_feed", ""))
        if bot.router:
            add_online_routes(bot.router, bot.online)
        for tool in online_tools(bot.online):
            box.register(tool)

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
            handler=lambda a: day_facts(bot, cfg),
        ))
        if bot.router:
            from jarvis.router import BRIEFING_PATTERN, briefing_route

            bot.router.add("briefing", BRIEFING_PATTERN, briefing_route(lambda: day_facts(bot, cfg)))

    if bot.notifier is not None:
        box.register(phone_tool(bot.notifier))

    servers = cfg["mcp"].get("servers") or {}
    if servers:
        from jarvis.mcp_client import MCPHub

        hub = MCPHub(servers)
        hub.start()
        print(f"  [mcp] {hub.register(box)} tools ready")
        closers.append(hub)

    return closers


def phone_tool(notifier) -> Tool:
    def handler(a: dict) -> str:
        if not notifier.enabled:
            return "Phone alerts are off. Turn them on in Settings, under Your phone."
        sent = notifier.send("status", str(a.get("message", "")), title=str(a.get("title") or "Jarvis"), force=True)
        return "Sent to your phone." if sent else "There was nothing to send."

    return Tool(
        name="send_to_phone",
        description="Send a short message to the user's phone as a notification. Use for 'send that to my phone', "
                    "'text me the address', or to report back on something they asked you to keep an eye on.",
        parameters={"type": "object", "properties": {
            "message": {"type": "string", "description": "The text to send."},
            "title": {"type": "string", "description": "A two to four word title."},
        }, "required": ["message"]},
        handler=handler,
    )


def start_telegram(bot: Assistant, cfg: dict, llm) -> None:
    """Chat with Jarvis from anywhere through your own Telegram bot; alerts go there too."""
    import os

    from jarvis.telegram import TelegramBridge, transcriber_for

    t = cfg["telegram"]
    token = os.environ.get(t.get("token_env", "TELEGRAM_BOT_TOKEN"), "")
    if not t["enabled"] or not token:
        return
    try:
        bridge = TelegramBridge(
            token, Path(cfg["data_dir"]).expanduser() / "telegram.json",
            make_assistant=lambda speaker, listen: bot.share_with(Assistant(llm, speaker, listen, cfg)),
            transcribe=transcriber_for(cfg["stt"]),
        )
        bridge.start()
    except Exception as exc:
        print(f"  [telegram] couldn't start: {exc}. Check the token (docs/PHONE.md, part 3).")
        return
    bot.telegram = bridge
    if bot.notifier is not None:
        bot.notifier.channels.append(bridge.notify)


def make_notifier(bot: Assistant, cfg: dict) -> None:
    from jarvis.notify import Notifier

    bot.notifier = Notifier(cfg["notify"], cfg["data_dir"])


def start_watchers(bot: Assistant, cfg: dict) -> None:
    from jarvis.watchers import Scheduler, briefing_facts, build_watchers

    p = cfg["proactive"]

    def briefing() -> None:
        facts = day_facts(bot, cfg)
        bot.post("ask", "Give me my briefing for today in three to five short spoken sentences, "
                        "using these facts:\n" + facts)
        if bot.notifier is not None:
            bot.notifier.send("briefing", "\n".join(line for line in facts.splitlines() if line.strip()))

    def alert(message: str) -> None:
        bot.post("say", message)
        if bot.notifier is not None:
            bot.notifier.send("alerts", message)

    quiet = tuple(p["quiet_hours"]) if p.get("quiet_hours") else None
    Scheduler(build_watchers(p), alert, quiet_hours=quiet,
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
    parser.add_argument("--doctor", action="store_true", help="Check Ollama, models, the cloud key, disk and mic, with a fix for each problem")
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
    if args.doctor:
        from jarvis.doctor import print_report, run_checks

        sys.exit(print_report(run_checks(cfg, include_port=True)))
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
        sys.exit(f"{exc}\nRun jarvis --doctor for a full check.")

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


def warm_up(llm) -> None:
    """Load the local model in the background while Jarvis starts up."""
    def go():
        try:
            llm.warm()
        except Exception as exc:
            print(f"  [brain] couldn't preload the model: {exc}")
    if hasattr(llm, "warm"):
        threading.Thread(target=go, daemon=True, name="warm-up").start()


def start_extras(bot: Assistant, cfg: dict, llm, args) -> list:
    warm_up(llm)
    make_notifier(bot, cfg)
    closers = add_features(bot, cfg, llm)
    start_telegram(bot, cfg, llm)
    if cfg["proactive"]["enabled"]:
        start_watchers(bot, cfg)
    if args.briefing:
        from jarvis.watchers import briefing_facts

        bot.post("ask", "Give me my briefing for today in three to five short spoken sentences, "
                        "using these facts:\n" + day_facts(bot, cfg))
    return closers


def run_web(cfg: dict, llm, args) -> None:
    from jarvis.server import WebSession, serve

    session = WebSession(llm, cfg)
    v = cfg["voice_id"]
    if v["enabled"] and Path(v["model"]).is_file():
        from jarvis.voiceid import VoiceID

        session.voice = VoiceID(v["model"], Path(cfg["data_dir"]).expanduser() / "voiceprint.json",
                                float(v["threshold"]))
        session.bot.owner_name = session.voice.name
    elif v["enabled"]:
        print("  [voice] to recognise your voice, run: python scripts/download_speaker_model.py")
    closers = start_extras(session.bot, cfg, llm, args)
    if session.bot.skills is not None:
        session.attach_skills(session.bot.skills)
    w = cfg["web"]
    from jarvis.phone import Phone

    session.phone = Phone(w, cfg["data_dir"])
    try:
        httpd, _ = serve(session, w["host"], args.port or int(w["port"]), bool(w.get("open_browser", True)))
    except OSError as exc:
        sys.exit(f"Couldn't start the web server: {exc}. Is Jarvis already running? Try --port 8766.")
    announce_online(session)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
        for c in closers:
            c.close()


def announce_online(session) -> None:
    """Tell the phone Jarvis is up, with a tap-to-open link. Runs aside: Tailscale can be slow to answer."""
    def go():
        info = session.phone_info()  # also points alerts at the phone address
        if session.notifier is not None:
            where = " Tap to open it." if info.get("url") else ""
            session.notifier.send("status", "Jarvis is online on your laptop." + where)

    threading.Thread(target=go, daemon=True, name="online-note").start()


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
