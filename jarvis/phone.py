"""Jarvis on your phone.

The recommended route is Tailscale (free): it links your laptop and phone in a
private network that works anywhere, home Wi-Fi or mobile data, and
`tailscale serve` gives Jarvis a real https address. The phone needs https for
the microphone and to install Jarvis as an app on the home screen.

The web server itself keeps listening on 127.0.0.1 only. Tailscale forwards
the phone's requests to it, so nothing is opened to the rest of your network.

Every request that isn't from this laptop's own browser must carry the phone
key, a long random secret. The phone gets it once by scanning a QR code on the
laptop, then keeps it in a cookie. Resetting the key signs every phone out.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

COOKIE = "jarvis_key"


class Phone:
    def __init__(self, cfg: dict, data_dir: str | Path, run=subprocess.run):
        self.cfg = cfg
        self.state_file = Path(data_dir).expanduser() / "phone.json"
        state = self._load()
        self.enabled = bool(cfg.get("phone")) or bool(state.get("enabled"))
        self.key = str(state.get("key") or "")
        if self.enabled and not self.key:
            self.key = secrets.token_urlsafe(24)
            self._save()
        self.extra_hosts = {hostname(h) for h in cfg.get("extra_hosts") or []}
        self._run = run
        self._ts_cache: dict | None = None

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"enabled": self.enabled, "key": self.key}), encoding="utf-8")

    def turn_on(self) -> None:
        self.enabled = True
        self.key = self.key or secrets.token_urlsafe(24)
        self._save()

    def turn_off(self) -> None:
        self.enabled = False
        self._save()

    def reset_key(self) -> None:
        """A new key: every phone that paired before has to scan the code again."""
        self.key = secrets.token_urlsafe(24)
        self._save()

    # ---- who may talk to Jarvis --------------------------------------------

    def key_ok(self, given: str) -> bool:
        return bool(self.enabled and self.key and given) and secrets.compare_digest(given, self.key)

    def host_allowed(self, host: str) -> bool:
        """Addresses a phone may use: the Tailscale name, this laptop's own IPs, or ones listed in config."""
        if not self.enabled:
            return False
        name = hostname(host)
        if name.endswith(".ts.net") or name in self.extra_hosts:
            return True
        try:
            ip = ipaddress.ip_address(name)
        except ValueError:
            return False
        # A literal IP is only trusted if it's private (home Wi-Fi) or Tailscale's 100.64.0.0/10.
        return ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")

    # ---- addresses to show on the laptop -----------------------------------

    def tailscale(self, refresh: bool = False) -> dict:
        """{"installed", "running", "name", "serving"} from the Tailscale command line."""
        if self._ts_cache is not None and not refresh:
            return self._ts_cache
        out = {"installed": False, "running": False, "name": "", "serving": False}
        exe = tailscale_exe()
        if exe:
            out["installed"] = True
            try:
                res = self._run([exe, "status", "--json"], capture_output=True, text=True, timeout=5)
                data = json.loads(res.stdout or "{}")
                me = data.get("Self") or {}
                out["running"] = data.get("BackendState") == "Running"
                out["name"] = str(me.get("DNSName") or "").rstrip(".")
                res = self._run([exe, "serve", "status", "--json"], capture_output=True, text=True, timeout=5)
                out["serving"] = bool((res.stdout or "").strip() not in ("", "{}", "null"))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        self._ts_cache = out
        return out

    def info(self, port: int, refresh: bool = False) -> dict:
        """Everything the laptop's phone panel needs. Never send this to a phone: it holds the key."""
        ts = self.tailscale(refresh) if self.enabled else {"installed": bool(tailscale_exe()), "running": False,
                                                           "name": "", "serving": False}
        ts_url = f"https://{ts['name']}" if ts["name"] and ts["serving"] else ""
        lan = lan_ip()
        lan_url = f"http://{lan}:{port}" if lan and self.cfg.get("host") == "0.0.0.0" else ""
        base = ts_url or lan_url
        return {
            "enabled": self.enabled,
            "tailscale": ts,
            "serve_command": f"tailscale serve --bg {port}",
            "url": base,
            "pair_url": f"{base}/pair?key={self.key}" if base and self.enabled else "",
            "https": base.startswith("https://"),
        }

    def open_url(self, port: int) -> str:
        """The address a notification should open on the phone, or ''."""
        info = self.info(port)
        return info["url"]


def tailscale_exe() -> str:
    found = shutil.which("tailscale")
    if found:
        return found
    for path in (r"C:\Program Files\Tailscale\tailscale.exe", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if os.path.exists(path):
            return path
    return ""


def lan_ip() -> str:
    """This laptop's address on the home network. Sends nothing: UDP connect only picks a route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return ""


def hostname(host: str) -> str:
    """"example.com:8765" -> "example.com", "[::1]:80" -> "::1"."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host[1:]
    return host.split(":")[0]


def is_local_host(host: str) -> bool:
    return hostname(host) in ("localhost", "127.0.0.1", "::1")


def cookie_value(header: str) -> str:
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return ""
