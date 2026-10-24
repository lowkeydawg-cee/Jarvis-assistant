# How Jarvis works

This is the plain-language tour. No code, just what happens and why.

## The big picture

Think of Jarvis as a person sitting at your desk with five skills:

1. **Ears and a mouth** that work at the same time, so it can start answering before it has finished thinking, and stop when you talk over it.
2. **A toolbelt** that can plug in new apps without rewriting Jarvis. That's MCP.
3. **A memory of your own stuff**: your notes, documents and code.
4. **Eyes** that look at your screen, but only when you ask.
5. **A habit of glancing around** every so often and telling you if something needs attention.

Everything runs on your computer. The "thinking" is done by a language model running in Ollama, a free app that hosts AI models locally.

## One conversation, step by step

Say you ask: *"Hey Jarvis, where did I write that auth fix?"*

1. **Waiting.** Jarvis listens quietly for the words "Hey Jarvis". A tiny wake-word model checks the mic about twelve times a second. Nothing is recorded or sent anywhere until it hears its name.
2. **Recording.** Jarvis says "Yes?" and records you. A voice activity detector called Silero tells real speech apart from background noise, so Jarvis knows when you've finished your sentence.
3. **Understanding.** The recording goes to Whisper, a speech-to-text model, which turns it into the text "where did I write that auth fix".
4. **Thinking.** The text goes to the language model, along with Jarvis's personality rules and a list of every tool it can use. The model decides it needs a tool: *search_notes*.
5. **Remembering.** The second brain searches your notes by meaning, not just by matching words, so "auth fix" also finds a note that says "login bug". It hands back the best two or three passages and which file they came from.
6. **Answering.** The model writes a reply using those passages. As soon as the first full sentence is ready, it's sent to Piper, the voice engine, and you hear it. While you're hearing sentence one, sentence two is already being turned into audio. That's why it feels quick.
7. **Interrupting.** If barge-in is on and you start talking, Jarvis stops mid-sentence, drops the rest, and starts listening. It keeps the bit of audio it heard, so your first words aren't lost.

## The five upgrades in plain words

### 1. MCP agent: plugging in apps

**What it is.** The Model Context Protocol is a standard plug shape for AI tools. Lots of apps already ship an MCP "server", a small program that offers a set of actions, like read a file, show git history, or list calendar events.

**How Jarvis uses it.** When Jarvis starts, it launches each server listed in your config, asks "what can you do?", and adds every answer to its toolbelt. The model then sees, for example, *git__git_log* next to its built-in tools and can use it like any other.

**Why it matters.** Adding a new skill is a few lines of config, not new code. Servers that can change things ask for your spoken "yes" first.

**Day to day.** "What did I change in the jarvis repo today?" "Read me the README in my Downloads folder."

### 2. Second brain: answering from your own stuff

**What it is.** Retrieval-augmented generation, or RAG. Instead of the model guessing, it looks things up in your files first.

**How it works.**
- Jarvis reads the folders you choose and cuts each file into small overlapping pieces.
- Each piece is turned into an *embedding*, a list of numbers that captures what it means. Pieces about similar ideas get similar numbers.
- These are saved in one small database file on your computer.
- When you ask something, your question gets turned into numbers the same way, and Jarvis picks the pieces whose numbers are closest.
- Only changed files are processed again, so keeping it up to date is quick and happens in the background.

**Day to day.** "What did the lecture say about recursion?" "Which project used that Stripe webhook?" "Summarize my meeting notes from Monday."

### 3. Streaming voice: feeling like a real assistant

**What it is.** Three pieces working at once, like an assembly line:
- The model writes words a few at a time.
- A sentence splitter waits for a full sentence, cleans out any formatting, and skips code blocks so you don't hear Python read aloud.
- The voice engine turns sentence one into audio and plays it while sentence two is still being prepared.

**Barge-in** adds a fourth piece: while Jarvis speaks, the voice detector keeps listening to the mic. If it hears you for about a third of a second, playback stops. Use headphones for this, otherwise Jarvis can hear itself through the speakers and stop on its own voice.

**Day to day.** Answers start in about a second instead of after the whole paragraph is written, and you can say "no, wait" the moment it goes the wrong way.

### 4. Screen vision: "look at this"

**What it is.** A second, multimodal model that can read images.

**How it works.** Press Ctrl+Alt+J from any app, or say "look at my screen". Jarvis takes one screenshot, shrinks it so the model stays fast, and asks the vision model to describe what matters, quoting any error messages word for word. That description goes to the main model, which tells you what's wrong and how to fix it. The screenshot is never saved and never leaves your machine.

**Day to day.** A scary stack trace, a confusing chart, an installer asking an odd question. Press the key and ask.

### 5. Proactive mode: speaking up when it matters

**What it is.** A set of small watchers running quietly in the background.
- **Battery**: warns once when you drop below 20 percent and aren't plugged in.
- **Disk**: warns once when free space gets low.
- **Meetings**: reads your calendar file and reminds you ten minutes before each event.
- **Build logs**: watches log files you choose and tells you the first error when a build or test run fails.
- **Morning briefing**: once a day at the time you pick, it gathers your meetings, the state of your git repos and your disk space, and the model turns that into a short spoken summary.

**How it stays polite.** Each watcher warns once and then waits for things to recover before warning again. Quiet hours (10pm to 7am by default) mute everything. Watchers only ever talk. They never take actions on their own.

**How alerts get in.** Watchers and the screen hotkey drop messages into an *inbox*. Jarvis checks the inbox between conversations, so an alert never cuts across something you're in the middle of saying.

## Keeping it safe

Every action, whether it's a built-in tool or an MCP tool, goes through one checkpoint before it runs.
- Shell commands must be on an approved list, can't be chained or piped, can't use destructive options, and need your spoken "yes".
- MCP tools ask for confirmation by default. You can turn that off per server for read-only ones.
- Anything other than a clear "yes" counts as "no".

## Putting it all together: a day with Jarvis

- **8:30.** Jarvis gives your briefing: two meetings, three uncommitted changes in your repo, plenty of disk space.
- **9:50.** "Standup starts in 10 minutes."
- **11:15.** Your test run fails in another window. Jarvis says which test broke and the first error line.
- **11:16.** You press Ctrl+Alt+J on the error. Jarvis reads it and explains the fix.
- **14:00.** "Where did I put the notes on the payment flow?" It answers from your notes and names the file.
- **16:40.** "What did I change today?" It checks git through MCP and summarizes your commits.
- **16:41.** Halfway through the answer you say "okay, that's enough". It stops.
