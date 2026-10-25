# Roadmap: what Jarvis is missing, by urgency

Ranked from the first real install on an 8 GB, CPU-only Windows laptop, where the
problems were: very slow replies, the mic not picking up speech, a hard setup, and
vague errors.

**Urgency:** 🔴 do next, it blocks everyday use · 🟠 soon, big gain for the effort · 🟡 worth doing · ⚪ nice to have
**Effort:** S = an afternoon · M = a few days · L = a week or more

## 🔴 Do next

| # | Idea | Why it matters | Effort |
|---|---|---|---|
| 1 | **Push-to-talk hotkey** (hold a key to talk, as well as automatic listening) | The mic test showed automatic speech detection is fragile in noisy rooms and on quiet laptop mics. Holding a key always captures exactly what you said. | S |
| 2 | **`jarvis --doctor`**: one command that checks Ollama, the models, the API key, the mic, disk space and config paths, and prints a fix for each problem | Most of the first install went on finding these problems by hand. | S |
| 3 | **Clear error messages** that say what failed ("Ollama isn't running", "Groq key rejected", "reply took longer than 5 minutes") instead of "Something went wrong" | The one message users saw gave them nothing to act on. | S |
| 4 | **Warm up the model at startup**, and keep it loaded (`keep_alive`) | Loading took 21 s, and it happened on the first question. Warming it while Jarvis says hello hides that wait. | S |
| 5 | **One-click Windows installer** (`setup.ps1`): venv, packages, voice, a config with this machine's real folders, and a model that suits its RAM and GPU | Setup took an expert most of a day. | M |

## 🟠 Soon

| # | Idea | Why it matters | Effort |
|---|---|---|---|
| 6 | **Latency log for every turn**: speech-to-text ms, time to the model's first word, voice ms | Without it, nobody can tell which stage is slow. It turns "it's slow" into a number. | S |
| 7 | **Smarter instant answers**: match by meaning (embeddings) so paraphrases work, plus **your own commands in config.yaml** ("open my project", "start the build") | The router only knows exact phrasings today. Custom commands make Jarvis useful without a model at all. | M |
| 8 | ~~**Long-term memory**~~ Done: "remember that..." persists and reaches the model. Next: keep a summary of past conversations too. | History is lost every time Jarvis closes. | M |
| 9 | **Treat tool output as untrusted** (notes, files, web pages, MCP results), so text inside a file can't steer Jarvis into running commands | The confirmation step already protects actions, but this closes prompt injection properly before more tools arrive. | M |
| 10 | **Tests on every push** (GitHub Actions, Windows and Linux) | 58 tests exist but only run by hand. | S |

## 🟡 Worth doing

| # | Idea | Why it matters | Effort |
|---|---|---|---|
| 11 | **Better second-brain search**: keyword plus meaning, then reranking, reading only clean text (no badges or code) | Answers are only as good as the note it finds. | M |
| 12 | **Echo cancellation**, so you can interrupt Jarvis on laptop speakers, not only with headphones | Today barge-in needs headphones. | L |
| 13 | **Reliable "Hey Jarvis"**: tune the wake word on your own voice, and show its confidence in `--mic-test` | The wake word is off by default because it was never tuned. | M |
| 14 | **Real calendar**: Google or Outlook through an MCP server, including recurring events | Briefings miss meetings unless you export an .ics file by hand. | M |
| 15 | **Power-aware mode**: on battery, switch to cloud and lite settings automatically | Running the local model drains a laptop fast. | S |

## ⚪ Nice to have

| # | Idea | Why it matters | Effort |
|---|---|---|---|
| 16 | **Tray app** with the live transcript and what Jarvis is doing | Easier to see than a terminal. | M |
| 17 | **Phone as the microphone**: open the web app on your phone over home Wi-Fi, with a login and HTTPS | Lets you talk to Jarvis from anywhere in the house. | M |
| 18 | **More languages**: Whisper and Kokoro already support several | Wider use. | S |

## Already done

- Hybrid brain: Groq in the cloud, with local Ollama as the fallback (v0.3).
- Browser app with the browser's own speech recognition and voices (v0.3).
- Lite mode: instant answers, a short local prompt, `--mic-test`, cloud speech to text, and the Kokoro voice.
- Reminders and a schedule with snooze and desktop notifications; long-term memory.
- Recognises the owner's voice, and keeps personal answers from other voices.
- Live knowledge without keys: weather, Wikipedia facts, headlines; a Today panel in the browser app.
- Browser app: one active tab, voice picker, mic meter, "Up next" panel, sentences no longer split at pauses.
