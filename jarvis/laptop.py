"""Jarvis knows your laptop: every file on your drives, your apps, and what's running.

A background scan builds a local index (SQLite with full-text search) of every
file under the folders you choose, by default your whole user folder and any
other drive such as D:. For documents, notes and code it also indexes the words
inside, so "find the essay where I wrote about climate" works, not just names.
The first scan takes a few minutes; later scans only re-read changed files.

It also answers questions about the machine itself: installed apps, what's
running and eating memory, what starts with Windows, the Wi-Fi, Chrome
bookmarks and history.

Everything is read-only. A deny list keeps Jarvis away from passwords,
browser cookies, keys and similar secrets, even if you ask. The index lives in
~/.jarvis/laptop.sqlite and never leaves the laptop. Note that whatever Jarvis
reads to answer you is sent to the cloud brain unless Private mode is on.
"""

from __future__ import annotations

import fnmatch
import json
import os
import platform
import re
import shutil
import sqlite3
import string
import subprocess
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
    ".html", ".htm", ".css", ".scss", ".sql", ".sh", ".ps1", ".bat", ".kt", ".swift", ".dart", ".r", ".m", ".tex",
    ".ipynb", ".xml", ".srt", ".vtt",
}
DOC_EXT = {".pdf", ".docx", ".pptx", ".xlsx"}
KINDS = {
    "document": {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".pages", ".tex"},
    "spreadsheet": {".xls", ".xlsx", ".csv", ".ods", ".numbers"},
    "presentation": {".ppt", ".pptx", ".odp", ".key"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".svg", ".tiff", ".raw"},
    "video": {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv"},
    "audio": {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"},
    "code": {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".cs", ".go", ".rs", ".rb", ".php", ".ipynb",
             ".html", ".css", ".sql", ".kt", ".swift", ".dart"},
    "app": {".exe", ".msi", ".appx", ".lnk"},
}

# Folders that are never worth indexing: system files, caches, build output, package stores.
SKIP_DIRS = {
    "windows", "$recycle.bin", "system volume information", "programdata", "program files", "program files (x86)",
    "appdata", "node_modules", ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
    ".pytest_cache", ".cache", ".gradle", ".m2", ".nuget", ".npm", ".yarn", ".cargo", ".rustup", "site-packages",
    "dist", "build", ".next", ".idea", ".vs", "temp", "tmp", "$windows.~bt", "$windows.~ws", "recovery",
    "msocache", "perflogs", ".ollama", "jarvis-data",
}

# Never read, whatever is asked: these hold passwords, sessions and keys.
DENY_FOLDERS = ["*/.ssh/*", "*/.gnupg/*", "*/.aws/*", "*/.azure/*", "*/.kube/*", "*/.docker/*",
                "*/microsoft/credentials/*", "*/microsoft/protect/*"]
DENY_NAMES = [  # checked against the file's own name, so a folder called "Secrets" doesn't hide everything
    "*.pem", "*.key", "*.pfx", "*.p12", "*.kdbx", "*.keychain*", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    ".env", ".env.*", "*credentials*", "*secret*", "*password*", "*.wallet", "login data*", "cookies*",
    "web data*", "local state", ".git-credentials", ".netrc", ".npmrc", ".pypirc", "token*",
    "phone.json", "telegram.json", "notify.json",
]

# Windows marks OneDrive files that live only in the cloud. Opening one downloads it, so they're indexed by name only.
CLOUD_ONLY = 0x1000 | 0x40000 | 0x400000  # OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS

MAX_CONTENT = 20_000        # characters of text kept per file for search (keeps the index small)
MAX_READ_BYTES = 20_000_000  # never open files bigger than this to extract text


def is_denied(path: str | Path) -> bool:
    p = str(path).replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pat) for pat in DENY_NAMES) or any(fnmatch.fnmatch(p, pat) for pat in DENY_FOLDERS)


def kind_of(ext: str) -> str:
    for kind, exts in KINDS.items():
        if ext in exts:
            return kind
    return "other"


def default_roots() -> list[str]:
    """Your user folder, plus every other fixed drive (D:, E:...) on Windows."""
    roots = [str(Path.home())]
    if os.name == "nt":
        system = os.environ.get("SystemDrive", "C:").upper()
        for letter in string.ascii_uppercase[3:]:  # D: onwards
            drive = f"{letter}:\\"
            if f"{letter}:" != system and os.path.isdir(drive):
                roots.append(drive)
    return roots


def human_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---- text extraction --------------------------------------------------------


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if path.stat().st_size > MAX_READ_BYTES:
            return ""
        if ext in TEXT_EXT:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read(MAX_CONTENT * 2)
            if ext == ".ipynb":
                try:
                    nb = json.loads(text)
                    text = "\n".join("".join(c.get("source", [])) for c in nb.get("cells", []))
                except ValueError:
                    pass
            return text[:MAX_CONTENT]
        if ext in (".docx", ".pptx", ".xlsx"):
            return _office_text(path)[:MAX_CONTENT]
        if ext == ".pdf":
            return _pdf_text(path)[:MAX_CONTENT]
    except (OSError, zipfile.BadZipFile, ValueError, KeyError):
        return ""
    return ""


def _office_text(path: Path) -> str:
    """Words from Word, PowerPoint and Excel files, straight from their XML. No extra packages."""
    parts = []
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.endswith(".xml") and (
            n.startswith("word/document") or n.startswith("ppt/slides/slide") or n == "xl/sharedStrings.xml")]
        for name in sorted(names)[:200]:
            xml = z.read(name).decode("utf-8", errors="ignore")
            xml = re.sub(r"</w:p>|</a:p>|</si>", "\n", xml)
            parts.append(re.sub(r"<[^>]+>", "", xml))
            if sum(len(p) for p in parts) > MAX_CONTENT:
                break
    return "\n".join(parts)


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # optional: pip install pypdf
    except ImportError:
        return ""
    out = []
    reader = PdfReader(str(path))
    for page in reader.pages[:40]:
        out.append(page.extract_text() or "")
        if sum(len(p) for p in out) > MAX_CONTENT:
            break
    return "\n".join(out)


# ---- the index ----------------------------------------------------------------


class LaptopIndex:
    def __init__(self, db_path: str | Path, roots: list[str] | None = None, extra_skip: Iterable[str] = (),
                 content: bool = True, clock: Callable[[], float] = time.time):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.roots = [str(Path(r).expanduser()) for r in (roots or default_roots())]
        self.skip = SKIP_DIRS | {s.lower() for s in extra_skip}
        self.content = content
        self.clock = clock
        self._lock = threading.Lock()
        self.scanning = False
        self.progress = 0
        self.last_scan: float | None = None
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY, name TEXT, ext TEXT, kind TEXT, folder TEXT,
                    size INTEGER, mtime REAL, seen REAL);
                CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
                CREATE INDEX IF NOT EXISTS files_size ON files(size);
                CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
                    path UNINDEXED, name, folder, body, tokenize='unicode61 remove_diacritics 2');
                CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            """)
            row = db.execute("SELECT v FROM meta WHERE k='last_scan'").fetchone()
            self.last_scan = float(row[0]) if row else None

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    # ---- scanning ----

    def walk(self) -> Iterable[os.DirEntry]:
        stack = [r for r in self.roots if os.path.isdir(r)]
        while stack:
            folder = stack.pop()
            try:
                with os.scandir(folder) as it:
                    for entry in it:
                        name = entry.name.lower()
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if name not in self.skip and not name.startswith(("$", ".")):
                                    stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                yield entry
                        except OSError:
                            continue
            except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
                continue

    def scan(self, stop: threading.Event | None = None) -> dict:
        """Index new and changed files, forget deleted ones. Returns counts."""
        with self._lock:
            if self.scanning:
                return {"busy": True}
            self.scanning = True
        started = self.clock()
        added = updated = 0
        self.progress = 0
        db = self._db()
        try:
            known = dict(db.execute("SELECT path, mtime FROM files"))
            batch = 0
            for entry in self.walk():
                if stop is not None and stop.is_set():
                    break
                self.progress += 1
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                path = entry.path
                old = known.get(path)
                if old is not None and abs(old - st.st_mtime) < 1:
                    db.execute("UPDATE files SET seen=? WHERE path=?", (started, path))
                else:
                    p = Path(path)
                    ext = p.suffix.lower()
                    body = ""
                    cloud_only = getattr(st, "st_file_attributes", 0) & CLOUD_ONLY
                    if self.content and (ext in TEXT_EXT or ext in DOC_EXT) and not is_denied(path) and not cloud_only:
                        body = extract_text(p)
                    db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?)",
                               (path, entry.name, ext, kind_of(ext), str(p.parent), st.st_size, st.st_mtime, started))
                    db.execute("DELETE FROM search WHERE path=?", (path,))
                    db.execute("INSERT INTO search (path, name, folder, body) VALUES (?,?,?,?)",
                               (path, _words(entry.name), _words(str(p.parent)), body))
                    if old is None:
                        added += 1
                    else:
                        updated += 1
                batch += 1
                if batch >= 500:
                    db.commit()
                    batch = 0
            removed = 0
            if stop is None or not stop.is_set():
                gone = [r[0] for r in db.execute("SELECT path FROM files WHERE seen < ?", (started,))]
                removed = len(gone)
                for i in range(0, len(gone), 500):
                    chunk = gone[i:i + 500]
                    marks = ",".join("?" * len(chunk))
                    db.execute(f"DELETE FROM files WHERE path IN ({marks})", chunk)
                    db.execute(f"DELETE FROM search WHERE path IN ({marks})", chunk)
                self.last_scan = started
                db.execute("INSERT OR REPLACE INTO meta VALUES ('last_scan', ?)", (str(started),))
            db.commit()
            return {"added": added, "updated": updated, "removed": removed, "seconds": round(self.clock() - started)}
        finally:
            db.close()
            self.scanning = False

    def start_background(self, every_hours: float = 6, log: Callable[[str], None] = print) -> threading.Event:
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                try:
                    result = self.scan(stop)
                    if not result.get("busy"):
                        log(f"  [laptop] index up to date: {self.count()} files "
                            f"(+{result['added']} new, {result['updated']} changed, {result['removed']} gone)")
                except Exception as exc:
                    log(f"  [laptop] scan failed: {exc}")
                stop.wait(every_hours * 3600)

        threading.Thread(target=loop, daemon=True, name="laptop-index").start()
        return stop

    # ---- questions ----

    def count(self) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    def search(self, query: str, kind: str = "", limit: int = 10) -> list[dict]:
        terms = [t for t in re.findall(r"[\w\-']+", query.lower()) if len(t) > 1]
        if not terms:
            return []
        match = " ".join(f'"{t}"*' for t in terms)
        sql = ("SELECT s.path, f.size, f.mtime, f.kind, snippet(search, 3, '[', ']', ' ... ', 12), bm25(search, 0, 8, 3, 1) "
               "FROM search s JOIN files f ON f.path = s.path WHERE search MATCH ?")
        args: list = [match]
        if kind:
            sql += " AND f.kind = ?"
            args.append(kind)
        sql += " ORDER BY bm25(search, 0, 8, 3, 1) LIMIT ?"
        args.append(limit)
        with self._db() as db:
            try:
                rows = db.execute(sql, args).fetchall()
            except sqlite3.OperationalError:
                return []
        return [{"path": r[0], "size": r[1], "modified": r[2], "kind": r[3], "snippet": r[4] or ""} for r in rows]

    def recent(self, days: float = 7, kind: str = "", limit: int = 15) -> list[dict]:
        since = self.clock() - days * 86400
        sql = "SELECT path, size, mtime, kind FROM files WHERE mtime >= ?"
        args: list = [since]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY mtime DESC LIMIT ?"
        args.append(limit)
        with self._db() as db:
            return [{"path": r[0], "size": r[1], "modified": r[2], "kind": r[3]} for r in db.execute(sql, args)]

    def largest(self, limit: int = 10, under: str = "") -> list[dict]:
        sql = "SELECT path, size, mtime, kind FROM files"
        args: list = []
        if under:
            sql += " WHERE path LIKE ?"
            args.append(str(Path(under).expanduser()).rstrip("\\/") + "%")
        sql += " ORDER BY size DESC LIMIT ?"
        args.append(limit)
        with self._db() as db:
            return [{"path": r[0], "size": r[1], "modified": r[2], "kind": r[3]} for r in db.execute(sql, args)]

    def overview(self) -> dict:
        with self._db() as db:
            total = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
            kinds = db.execute("SELECT kind, COUNT(*), SUM(size) FROM files GROUP BY kind ORDER BY SUM(size) DESC").fetchall()
            folders = db.execute(
                "SELECT folder, SUM(size) FROM files GROUP BY folder ORDER BY SUM(size) DESC LIMIT 8").fetchall()
        return {"files": total[0], "bytes": total[1], "roots": self.roots, "last_scan": self.last_scan,
                "scanning": self.scanning, "kinds": [{"kind": k, "files": n, "bytes": b} for k, n, b in kinds],
                "biggest_folders": [{"folder": f, "bytes": b} for f, b in folders]}

    def allowed(self, path: str | Path) -> bool:
        """Jarvis may read a file only inside the indexed folders, and never a secret."""
        try:
            real = Path(path).expanduser().resolve()
        except OSError:
            return False
        if is_denied(real):
            return False
        for root in self.roots:
            try:
                real.relative_to(Path(root).resolve())
                return True
            except ValueError:
                continue
        return False


def _words(text: str) -> str:
    """Split file names so "ClimateEssay_final-v2.docx" is found by "climate essay"."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return re.sub(r"[_\-.\\/]+", " ", text)


# ---- the machine itself ----------------------------------------------------------


def installed_apps(limit: int = 400) -> list[dict]:
    """Programs from Windows' uninstall list (what Settings > Apps shows)."""
    if os.name != "nt":
        return []
    import winreg

    apps: dict[str, dict] = {}
    places = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
              (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
    for hive, key in places:
        try:
            root = winreg.OpenKey(hive, key)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(root)[0]):
            try:
                sub = winreg.OpenKey(root, winreg.EnumKey(root, i))
                name = winreg.QueryValueEx(sub, "DisplayName")[0]
            except OSError:
                continue

            def val(v):
                try:
                    return str(winreg.QueryValueEx(sub, v)[0])
                except OSError:
                    return ""

            if val("SystemComponent") == "1" or not name:
                continue
            apps[name.lower()] = {"name": name, "version": val("DisplayVersion"), "publisher": val("Publisher"),
                                  "installed": val("InstallDate")}
    return sorted(apps.values(), key=lambda a: a["name"].lower())[:limit]


def running_programs(limit: int = 12) -> list[dict]:
    import psutil

    groups: dict[str, dict] = {}
    for p in psutil.process_iter(["name", "memory_info", "cpu_percent"]):
        try:
            name = (p.info["name"] or "").removesuffix(".exe")
            mem = p.info["memory_info"].rss if p.info["memory_info"] else 0
        except (psutil.Error, AttributeError):
            continue
        if not name or name.lower() in ("system idle process", "system", "registry", "memory compression"):
            continue
        g = groups.setdefault(name, {"name": name, "memory": 0, "count": 0})
        g["memory"] += mem
        g["count"] += 1
    return sorted(groups.values(), key=lambda g: -g["memory"])[:limit]


def startup_programs() -> list[str]:
    out = []
    if os.name == "nt":
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Run")
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(key)[1]):
                try:
                    out.append(winreg.EnumValue(key, i)[0])
                except OSError:
                    pass
        folder = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup"
        if folder.is_dir():
            out += [p.stem for p in folder.iterdir() if p.suffix.lower() in (".lnk", ".exe", ".bat")]
    return sorted(set(out))


def wifi() -> str:
    if os.name != "nt":
        return ""
    try:
        res = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    fields = {}
    for line in res.stdout.splitlines():
        k, _, v = line.partition(":")
        fields[k.strip().lower()] = v.strip()
    if fields.get("state", "").lower() != "connected":
        return "Wi-Fi isn't connected."
    return (f"Connected to {fields.get('ssid', 'a network')}, signal {fields.get('signal', 'unknown')}, "
            f"{fields.get('receive rate (mbps)', '?')} Mbps.")


def machine_summary() -> str:
    import psutil

    gb = 1024 ** 3
    mem = psutil.virtual_memory()
    lines = [f"{platform.system()} {platform.release()} ({platform.version()}), computer name {platform.node()}.",
             f"Processor: {platform.processor() or 'unknown'}, {psutil.cpu_count(logical=False)} cores, "
             f"{psutil.cpu_count()} threads, {psutil.cpu_percent(interval=0.3):.0f}% busy.",
             f"Memory: {(mem.total - mem.available) / gb:.1f} of {mem.total / gb:.1f} GB in use."]
    for part in psutil.disk_partitions(all=False):
        try:
            u = shutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        lines.append(f"Drive {part.mountpoint}: {u.free / gb:.0f} GB free of {u.total / gb:.0f} GB.")
    b = psutil.sensors_battery()
    if b:
        lines.append(f"Battery {b.percent:.0f}%{' and charging' if b.power_plugged else ''}.")
    up = timedelta(seconds=int(time.time() - psutil.boot_time()))
    lines.append(f"On for {up.days} days {up.seconds // 3600} hours since the last restart.")
    w = wifi()
    if w:
        lines.append(w)
    return "\n".join(lines)


# ---- Chrome and Edge -----------------------------------------------------------


def browser_profiles() -> list[tuple[str, Path]]:
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    found = []
    for name, base in (("Chrome", local / "Google/Chrome/User Data"), ("Edge", local / "Microsoft/Edge/User Data")):
        if base.is_dir():
            for prof in sorted(base.iterdir()):
                if prof.is_dir() and (prof.name == "Default" or prof.name.startswith("Profile ")):
                    found.append((f"{name} {prof.name}", prof))
    return found


def bookmarks(query: str = "", limit: int = 15) -> list[dict]:
    out = []

    def walk(node, folder):
        if node.get("type") == "url":
            out.append({"title": node.get("name", ""), "url": node.get("url", ""), "folder": folder})
        for child in node.get("children", []):
            walk(child, folder + "/" + node.get("name", "") if node.get("type") == "folder" else folder)

    for label, prof in browser_profiles():
        try:
            data = json.loads((prof / "Bookmarks").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for root in data.get("roots", {}).values():
            if isinstance(root, dict):
                walk(root, label)
    q = query.lower().split()
    hits = [b for b in out if all(w in (b["title"] + " " + b["url"]).lower() for w in q)]
    return hits[:limit]


def history(query: str = "", days: float = 30, limit: int = 15) -> list[dict]:
    """Pages you visited. Chrome locks its history file while open, so a copy is read."""
    # Chrome stores time as microseconds since 1601-01-01.
    since = int((time.time() + 11644473600 - days * 86400) * 1_000_000)
    words = [w for w in query.lower().split() if w]
    out = []
    for label, prof in browser_profiles():
        src = prof / "History"
        if not src.is_file():
            continue
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "h.sqlite"
            try:
                shutil.copy2(src, copy)
                db = sqlite3.connect(copy)
                sql = "SELECT title, url, last_visit_time, visit_count FROM urls WHERE last_visit_time >= ?"
                args: list = [since]
                for w in words:
                    sql += " AND (lower(title) LIKE ? OR lower(url) LIKE ?)"
                    args += [f"%{w}%", f"%{w}%"]
                sql += " ORDER BY last_visit_time DESC LIMIT ?"
                args.append(limit)
                for title, url, t, n in db.execute(sql, args):
                    when = datetime.fromtimestamp(t / 1_000_000 - 11644473600)
                    out.append({"title": title, "url": url, "when": when.strftime("%d %b %H:%M"), "visits": n,
                                "browser": label})
                db.close()
            except (OSError, sqlite3.Error):
                continue
    out.sort(key=lambda r: r["when"], reverse=True)
    return out[:limit]


# ---- tools for the model ---------------------------------------------------------


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d %b %Y %H:%M")


def _list(rows: list[dict], snippet: bool = False) -> str:
    lines = []
    for r in rows:
        line = f"- {r['path']} ({human_size(r['size'])}, changed {_when(r['modified'])})"
        if snippet and r.get("snippet"):
            line += f"\n  ...{r['snippet'].strip()[:220]}..."
        lines.append(line)
    return "\n".join(lines)


def tools(index: LaptopIndex, browser: bool = True) -> list:
    from jarvis.tools import Tool

    def status_note() -> str:
        if index.last_scan is None:
            return (f"Note: the first scan of the laptop is still running ({index.progress} files so far), "
                    "so results may be incomplete.\n")
        return ""

    def find(a):
        rows = index.search(str(a.get("query", "")), str(a.get("kind") or ""), int(a.get("limit") or 10))
        if not rows:
            return status_note() + "No files matched."
        return status_note() + _list(rows, snippet=True)

    def recent(a):
        rows = index.recent(float(a.get("days") or 7), str(a.get("kind") or ""), int(a.get("limit") or 15))
        return status_note() + (_list(rows) or "Nothing changed in that time.")

    def big(a):
        rows = index.largest(int(a.get("limit") or 10), str(a.get("folder") or ""))
        return status_note() + (_list(rows) or "No files indexed yet.")

    def overview(a):
        o = index.overview()
        kinds = ", ".join(f"{k['kind']} {k['files']} files ({human_size(k['bytes'] or 0)})" for k in o["kinds"][:8])
        folders = "\n".join(f"- {f['folder']}: {human_size(f['bytes'] or 0)}" for f in o["biggest_folders"])
        return (status_note() + f"Indexed {o['files']} files, {human_size(o['bytes'])}, across {', '.join(o['roots'])}. "
                f"Last full scan: {_when(o['last_scan']) if o['last_scan'] else 'not finished yet'}.\n"
                f"By type: {kinds}.\nFolders holding the most:\n{folders}")

    def read(a):
        path = Path(str(a.get("path", ""))).expanduser()
        if not index.allowed(path):
            return ("I can't open that one. It's either outside the folders I know, or it could hold passwords "
                    "or keys, which I never read.")
        if not path.is_file():
            return "That file doesn't exist anymore."
        text = extract_text(path)
        if not text.strip():
            return f"{path.name} isn't a text document I can read ({human_size(path.stat().st_size)})."
        start = int(a.get("offset") or 0)
        chunk = text[start:start + 8000]
        more = f"\n[{len(text) - start - len(chunk)} more characters; ask with offset {start + len(chunk)}]" \
            if start + len(chunk) < len(text) else ""
        return f"{path}:\n{chunk}{more}"

    def folder(a):
        path = Path(str(a.get("path", ""))).expanduser()
        if not index.allowed(path) or not path.is_dir():
            return "I can't list that folder."
        items = []
        try:
            for e in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:80]:
                if is_denied(e):
                    continue
                items.append(f"{e.name}/" if e.is_dir() else f"{e.name} ({human_size(e.stat().st_size)})")
        except OSError as exc:
            return f"I couldn't open it: {exc}"
        return f"{path} holds:\n" + "\n".join(items)

    def apps(a):
        q = str(a.get("query") or "").lower()
        rows = [x for x in installed_apps() if q in x["name"].lower()] if q else installed_apps()
        if not rows:
            return "No installed apps matched." if q else "I couldn't read the installed apps list."
        return f"{len(rows)} apps:\n" + "\n".join(
            f"- {x['name']} {x['version']}".rstrip() + (f" ({x['publisher']})" if x["publisher"] else "")
            for x in rows[:150])

    def running(a):
        rows = running_programs(int(a.get("limit") or 12))
        return "Using the most memory now:\n" + "\n".join(
            f"- {r['name']}: {human_size(r['memory'])}" + (f" across {r['count']} processes" if r["count"] > 1 else "")
            for r in rows)

    def startup(a):
        rows = startup_programs()
        return "Starts with Windows: " + (", ".join(rows) if rows else "nothing I can see.")

    out = [
        Tool("find_files", "Search every file on the user's laptop by name, folder and the words inside documents, "
             "notes and code. Use for 'find my...', 'where is...', 'the file where I wrote about...'.",
             find, {"type": "object", "properties": {
                 "query": {"type": "string", "description": "Words from the name or content."},
                 "kind": {"type": "string", "enum": ["", *KINDS, "other"],
                          "description": "Optional file type filter."},
                 "limit": {"type": "integer"}}, "required": ["query"]}),
        Tool("recent_files", "Files the user created or changed recently.", recent,
             {"type": "object", "properties": {"days": {"type": "number"}, "kind": {"type": "string"},
                                               "limit": {"type": "integer"}}}),
        Tool("largest_files", "The biggest files, optionally inside one folder. Use for freeing up space.", big,
             {"type": "object", "properties": {"folder": {"type": "string"}, "limit": {"type": "integer"}}}),
        Tool("laptop_overview", "A summary of everything on the laptop: how many files, of what kind, and which "
             "folders take the most space.", overview),
        Tool("read_file", "Read a document, note, spreadsheet, slide deck or code file on the laptop (read-only). "
             "Use after find_files to answer from its contents.", read,
             {"type": "object", "properties": {"path": {"type": "string"},
                                               "offset": {"type": "integer"}}, "required": ["path"]}),
        Tool("list_folder", "List what's inside a folder on the laptop.", folder,
             {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
        Tool("installed_apps", "List or search the programs installed on the laptop.", apps,
             {"type": "object", "properties": {"query": {"type": "string"}}}),
        Tool("running_programs", "What's running right now and how much memory each program uses. Use for 'why "
             "is my laptop slow'.", running, {"type": "object", "properties": {"limit": {"type": "integer"}}}),
        Tool("startup_programs", "Programs that start automatically with Windows.", startup),
        Tool("laptop_details", "The computer's specs and state: Windows version, processor, memory, every drive's "
             "free space, battery, uptime and Wi-Fi.", lambda a: machine_summary()),
    ]
    if browser:
        out += [
            Tool("browser_history", "Search the pages the user visited in Chrome or Edge. Use for 'that site I "
                 "looked at yesterday about...'.",
                 lambda a: "\n".join(f"- {r['when']} {r['title'] or r['url']} ({r['url']})"
                                     for r in history(str(a.get('query') or ''), float(a.get('days') or 30)))
                 or "Nothing matched in your browsing history.",
                 {"type": "object", "properties": {"query": {"type": "string"}, "days": {"type": "number"}}}),
            Tool("bookmarks", "Search the user's Chrome or Edge bookmarks.",
                 lambda a: "\n".join(f"- {r['title']}: {r['url']}" for r in bookmarks(str(a.get('query') or '')))
                 or "No bookmarks matched.",
                 {"type": "object", "properties": {"query": {"type": "string"}}}),
        ]
    return out
