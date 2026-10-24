"""Second brain: a local, searchable index of your notes, docs and code.

How it works:
1. Files in the configured folders are split into overlapping chunks.
2. Each chunk is turned into an embedding (a list of numbers that captures its
   meaning) by a local Ollama embedding model.
3. Chunks and embeddings are stored in one SQLite file.
4. A question is embedded the same way, and the closest chunks by cosine
   similarity are handed to the LLM as context.

Re-indexing is incremental: only files whose modification time changed are
re-embedded. A plain NumPy matrix search is plenty fast for a personal corpus
(tens of thousands of chunks); swap in Chroma or Qdrant if it ever isn't.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

TEXT_EXTS = {
    ".md", ".txt", ".rst", ".org", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp",
    ".h", ".cs", ".go", ".rs", ".rb", ".php", ".html", ".css", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".sh", ".ps1", ".sql", ".csv",
}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".idea"}
MAX_FILE_BYTES = 2_000_000

Embedder = Callable[[list[str]], list[list[float]]]


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    """Split on paragraph boundaries into chunks of roughly `size` characters."""
    paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    chunks: list[str] = []
    cur = ""
    for para in paras:
        while len(para) > size:  # one huge paragraph: hard split
            head, para = para[:size], para[size - overlap :]
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(head)
        if cur and len(cur) + len(para) + 2 > size:
            chunks.append(cur)
            cur = cur[-overlap:] + "\n\n" + para if overlap else para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


def read_file(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8", errors="ignore")


def iter_files(folders: Iterable[str | Path]) -> Iterable[Path]:
    for folder in folders:
        root = Path(os.path.expanduser(str(folder)))
        if root.is_file():
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                p = Path(dirpath) / name
                ext = p.suffix.lower()
                if (ext in TEXT_EXTS or ext == ".pdf") and p.stat().st_size <= MAX_FILE_BYTES:
                    yield p


class KnowledgeBase:
    def __init__(self, db_path: str | Path, embed: Embedder, doc_prefix: str = "", query_prefix: str = ""):
        self.db_path = Path(os.path.expanduser(str(db_path)))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embed = embed
        self.doc_prefix = doc_prefix
        self.query_prefix = query_prefix
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS chunks (path TEXT, mtime REAL, idx INTEGER, text TEXT, vec BLOB)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path)")
        self.db.commit()
        self._matrix: np.ndarray | None = None
        self._rows: list[tuple[str, str]] = []

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def index(self, folders: Iterable[str | Path], batch: int = 32, log: Callable[[str], None] = print) -> int:
        """Embed new or changed files. Returns how many files were (re)indexed."""
        with self.lock:
            known = dict(self.db.execute("SELECT path, MAX(mtime) FROM chunks GROUP BY path"))
        seen, changed = set(), 0
        for path in iter_files(folders):
            key = str(path.resolve())
            seen.add(key)
            mtime = path.stat().st_mtime
            if known.get(key) == mtime:
                continue
            try:
                chunks = chunk_text(read_file(path))
            except Exception as exc:
                log(f"  [brain] skipped {path.name}: {exc}")
                continue
            vecs: list[list[float]] = []
            for i in range(0, len(chunks), batch):
                vecs += self.embed([self.doc_prefix + c for c in chunks[i : i + batch]])
            with self.lock:
                self.db.execute("DELETE FROM chunks WHERE path = ?", (key,))
                self.db.executemany(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                    [(key, mtime, i, c, np.asarray(v, dtype=np.float32).tobytes())
                     for i, (c, v) in enumerate(zip(chunks, vecs))],
                )
                self.db.commit()
            changed += 1
        removed = [p for p in known if p not in seen]
        with self.lock:
            self.db.executemany("DELETE FROM chunks WHERE path = ?", [(p,) for p in removed])
            self.db.commit()
            self._matrix = None
        if changed or removed:
            log(f"  [brain] indexed {changed} files, removed {len(removed)}, {self.count()} chunks total")
        return changed

    def _load(self) -> None:
        with self.lock:
            rows = self.db.execute("SELECT path, text, vec FROM chunks").fetchall()
        self._rows = [(p, t) for p, t, _ in rows]
        if rows:
            m = np.stack([np.frombuffer(v, dtype=np.float32) for _, _, v in rows])
            self._matrix = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
        else:
            self._matrix = np.zeros((0, 1), dtype=np.float32)

    def search(self, query: str, k: int = 4) -> list[tuple[float, str, str]]:
        """Return (score, path, text) for the k chunks closest in meaning to query."""
        if self._matrix is None:
            self._load()
        if not len(self._rows):
            return []
        q = np.asarray(self.embed([self.query_prefix + query])[0], dtype=np.float32)
        scores = self._matrix @ (q / (np.linalg.norm(q) + 1e-9))
        top = np.argsort(-scores)[:k]
        return [(float(scores[i]), *self._rows[i]) for i in top]

    def answer_context(self, query: str, k: int = 4, min_score: float = 0.3) -> str:
        hits = [h for h in self.search(query, k) if h[0] >= min_score]
        if not hits:
            return "No matching notes found."
        return "\n\n".join(f"From {Path(p).name} (match {s:.2f}):\n{t}" for s, p, t in hits)
