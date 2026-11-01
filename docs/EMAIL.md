# Gmail in Jarvis

Ask "any new emails?", "anything important in my inbox?", "what did Andela say about the interview?",
or "draft a reply saying I'll be there". Jarvis reads your inbox and can save replies as **drafts**.
It cannot send email: you open Gmail, check the draft, and press Send yourself.

It uses a Gmail **app password**: a separate 16-letter password just for Jarvis. Your real Google
password is never used, and you can delete the app password any time to cut Jarvis off.

## 1. Turn on 2-Step Verification (if it isn't already)

Google only offers app passwords when 2-Step Verification is on: <https://myaccount.google.com/signinoptions/twosv>

## 2. Create the app password

1. Go to <https://myaccount.google.com/apppasswords> and sign in.
2. Type a name such as `Jarvis` and press **Create**.
3. Google shows a 16-letter password in four groups. Copy it (the spaces don't matter).

## 3. Save your address and the app password

Open **PowerShell** and run these lines one at a time. The second one asks for the app password: paste it
and press Enter. Nothing shows while you paste, and that's expected.

```powershell
[Environment]::SetEnvironmentVariable("GMAIL_ADDRESS", "you@gmail.com", "User")
$p = Read-Host "Paste the Gmail app password" -AsSecureString
[Environment]::SetEnvironmentVariable("GMAIL_APP_PASSWORD", [Net.NetworkCredential]::new("", $p).Password, "User")
Remove-Variable p
```

Replace `you@gmail.com` with your address.

## 4. Restart Jarvis

Close the Jarvis window and start `start-jarvis-web.bat` again (or sign out and back in: Jarvis starts by
itself). The black window shows `[email] connected to you@gmail.com (read and draft only)`.

## What Jarvis does and doesn't do

- Checking mail never marks anything as read.
- It only saves drafts, in Gmail's Drafts folder. There is no sending code in Jarvis at all.
- Text inside emails is treated as untrusted, so an email that says "ignore your instructions" can't make
  Jarvis do anything.
- Voices Jarvis doesn't recognise can't ask about your email.
- To answer you, the email text goes to the active brain (the cloud unless Private mode is on).

To disconnect: delete the app password at <https://myaccount.google.com/apppasswords>, then run
`[Environment]::SetEnvironmentVariable("GMAIL_APP_PASSWORD", $null, "User")`.
