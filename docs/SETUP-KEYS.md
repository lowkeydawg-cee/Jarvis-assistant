# Adding your Groq key (Windows PowerShell)

With a key, Jarvis sends open questions to Groq's fast cloud model, and replies come
in about a second. Without one, everything stays on your laptop. Your notes,
screenshots and instant answers always stay local.

Keep the key secret. Don't paste it into chats, `config.yaml` or GitHub. These steps
store it as a Windows user setting that only your account can read.

## 1. Get a key

Go to <https://console.groq.com/keys>, sign in, click **Create API Key**, and copy it.
It starts with `gsk_`.

## 2. Save it

Open **PowerShell** (no admin needed) and run these three lines one at a time. The
first one asks for the key: paste it and press Enter. Nothing shows while you paste,
and that's expected.

```powershell
$key = Read-Host "Paste your Groq key" -AsSecureString
[Environment]::SetEnvironmentVariable("GROQ_API_KEY", [Net.NetworkCredential]::new("", $key).Password, "User")
Remove-Variable key
```

## 3. Close PowerShell and open a new one

Windows only gives saved settings to windows opened after the change.

## 4. Check it's there

```powershell
if ($env:GROQ_API_KEY) { "Key found, $($env:GROQ_API_KEY.Length) characters" } else { "No key - redo step 2" }
```

## 5. Check Groq accepts it

```powershell
(Invoke-RestMethod https://api.groq.com/openai/v1/models -Headers @{ Authorization = "Bearer $env:GROQ_API_KEY" }).data.id | Select-Object -First 5
```

You should see model names such as `openai/gpt-oss-120b`. A `401` error means the
key was mistyped, so redo step 2.

## 6. Start Jarvis

```powershell
cd "D:\Downloads\jarvis (1)\jarvis"
.\.venv\Scripts\jarvis.exe --text
```

The first lines should say `[brain] cloud (openai/gpt-oss-120b), local fallback (...)`.
Ask it something open, such as "give me one tip to stay focused", then type `goodbye`.

To talk instead of type, use the browser app (recommended on this laptop):

```powershell
.\start-jarvis-web.bat
```

Or use the terminal voice mode: `.\start-jarvis.bat`.

## 7. Optional: faster, more accurate speech to text in terminal voice mode

The same key also works for Groq's Whisper. In `config.yaml`, under `stt:`, add:

```yaml
  provider: cloud
```

## Removing the key

```powershell
[Environment]::SetEnvironmentVariable("GROQ_API_KEY", $null, "User")
```

Then delete the key at console.groq.com/keys as well.
