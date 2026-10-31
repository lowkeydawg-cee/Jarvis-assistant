# Jarvis on your phone

Jarvis keeps running on your laptop. Your phone becomes a remote for it: same brain, same reminders, same voice, from anywhere. There are three parts, and you can use any of them alone.

1. **The phone app.** Talk to Jarvis from your phone, at home or out on mobile data.
2. **Phone alerts.** Jarvis pushes updates to your lock screen, even when the app is closed.
3. **Telegram.** Chat with Jarvis or send it voice notes from anywhere, with no Tailscale, and get your alerts there too.

All three are set up from the laptop, in **Settings, Your phone**. No config file editing needed.

## Part 1: the phone app

Tailscale links your laptop and phone in a private network that only your devices can join. It's free for personal use. It also gives Jarvis an https address, which phones require before they let a web page use the microphone.

1. Install Tailscale on the laptop and on the phone from https://tailscale.com/download, and sign in with the same account on both.
2. Start Jarvis on the laptop (double-click `start-jarvis-web.bat`).
3. In Jarvis, open **Settings**, then switch on **Use Jarvis on my phone**.
4. Run the command it shows, once, in PowerShell on the laptop:

   ```powershell
   tailscale serve --bg 8765
   ```

   If PowerShell says `tailscale` isn't recognised, use the full path:

   ```powershell
   & "C:\Program Files\Tailscale\tailscale.exe" serve --bg 8765
   ```

   Tailscale may ask you to enable HTTPS for your network the first time; follow the link it prints.
5. Press **Check again** in Settings. A QR code appears.
6. Scan it with your phone's camera. Jarvis opens on the phone.
7. Add it to your home screen: on Android in Chrome, menu, then **Add to Home screen** or **Install app**. On iPhone in Safari, the Share button, then **Add to Home Screen**.

From then on, tapping the Jarvis icon opens it like an app. Only one device listens and speaks at a time, so opening it on the phone quietly hands over from the laptop, and the laptop can take it back with **Use Jarvis here**.

### How it stays private

- The Jarvis server still only listens on the laptop itself (127.0.0.1). Tailscale passes the phone's requests to it, so nothing is opened to your Wi-Fi or the internet.
- Every request from a phone must carry the pairing key, a long random secret the phone got from the QR code. Without it, a phone only sees a "pair this phone first" page.
- The key can only be seen or changed on the laptop. **Sign out all phones** makes a new key, and every phone has to scan again.
- Phone access is off until you switch it on.

## Part 2: phone alerts

Alerts go through ntfy, a free push service with apps for Android and iPhone. There's no account or key. Jarvis makes a private, hard-to-guess topic name, and your phone subscribes to it.

1. In Jarvis on the laptop, open **Settings**, then switch on **Phone alerts**. Jarvis shows your topic name, like `jarvis-3f9a1c0d2b7e4a65`.
2. Install the **ntfy** app on your phone, tap **+**, and subscribe to that topic.
3. Press **Send a test**. It should land on your phone within a couple of seconds.

What Jarvis sends:

| Alert | When |
|---|---|
| Reminders | A reminder or schedule item is due. These buzz. |
| Heads up | Low battery, low disk space, a meeting about to start, or a build log that failed. Needs `proactive.enabled: true` in config.yaml. |
| Your briefing | The morning briefing, at the time set in `proactive.briefing_at`. |
| Jarvis replied | You asked something, then closed the page or put the phone away before the answer came. The answer follows you. |
| Jarvis needs a hand | Something broke, like the Groq key being rejected. |
| Jarvis | Jarvis came online, and anything you ask it to "send to my phone". |

Choose which ones you want in config.yaml:

```yaml
notify:
  send: [reminders, alerts, briefing, away, problems, status]
```

Jarvis never repeats the same alert within ten minutes and never sends more than 30 an hour. Warnings and the briefing stay silent during quiet hours (`proactive.quiet_hours`); reminders you set yourself always come through.

Keep the topic name private, since anyone who knows it can read your alerts. **New topic** in Settings makes a fresh one. If you'd rather not use the public ntfy.sh server, you can run your own ntfy server and set `notify.server` in config.yaml.

## Part 3: Telegram (chat from anywhere, no Tailscale needed)

Your own private Telegram bot. Message it or send a voice note from anywhere, and Jarvis answers with everything it has: your reminders, notes, weather and the cloud brain. Alerts from Part 2 arrive here too, with the same on/off choices. Only the laptop makes connections, so nothing is opened to the internet, and only your Telegram account can use it.

**a. Create the bot.** In Telegram, search for **@BotFather** (blue tick), open it, and send `/newbot`. Give it a name (for example `Jarvis`) and a username ending in `bot` (for example `saint_jarvis_bot`). It replies with a **token** like `8123456789:AAH...`. Copy it.

**b. Save the token on the laptop.** Open PowerShell and run these three lines one at a time. Paste the token when asked; nothing shows while you paste, and that's expected.

```powershell
$t = Read-Host "Paste your Telegram bot token" -AsSecureString
[Environment]::SetEnvironmentVariable("TELEGRAM_BOT_TOKEN", [Net.NetworkCredential]::new("", $t).Password, "User")
Remove-Variable t
```

**c. Close PowerShell and open a new one.** A window only sees settings saved before it opened. Check it:

```powershell
$env:TELEGRAM_BOT_TOKEN.Length
```

You should see a number around 46. Nothing or `0` means redo step b.

**d. Link your phone.** Start Jarvis (`start-jarvis-web.bat`). Open **Settings, Your phone**: it shows a message like `/start 123456`. In Telegram, open your bot, press **Start**, and send that message. Jarvis replies "Linked!".

Try "what's the weather", "remind me in 10 minutes to stretch", or hold the mic button and send a voice note.

## Things to try from the phone

- "Remind me to call mum when I get home at 6." The reminder buzzes your phone at 6.
- "How's my laptop?" Battery, disk and memory from wherever you are.
- "Send the address of the venue to my phone." It arrives as an alert you can copy.

## Troubleshooting

| What you see | Fix |
|---|---|
| "Pair this phone first" | Scan the QR code on the laptop again. If you pressed Sign out all phones, every phone needs the new code. |
| The phone can't load the page | Check Tailscale is connected on both, and Jarvis is running on the laptop. `jarvis --doctor` has a Phone line that says what's missing. |
| The microphone does nothing on the phone | Use the https Tailscale address (from the QR code), not a plain http one. |
| No alerts arrive | Press Send a test. If nothing comes, check the topic in the ntfy app matches the one in Settings exactly. |
| Settings says Telegram isn't set up | The token isn't saved, or Jarvis was started from a window opened before you saved it. Redo part 3, steps b and c, then restart Jarvis. |
| The Jarvis window says `[telegram] couldn't start: ... Unauthorized` | The token is wrong. Copy the full token from @BotFather again (send it `/mybots`, pick your bot, then API Token). |
