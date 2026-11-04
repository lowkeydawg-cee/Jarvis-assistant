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
import re
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
STOPWORDS = {"the", "and", "for", "what", "where", "did", "was", "were", "with", "about", "from", "that", "this",
             "have", "has", "how", "why", "when", "who", "you", "your", "my", "mine", "our", "are", "is", "it", "of",
             "to", "in", "on", "at", "do", "does", "can", "write", "wrote", "notes", "note"}

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
        # Keyword index beside the vectors: names, error codes and exact phrases that embeddings blur.
        self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, path UNINDEXED, idx UNINDEXED)")
        if (self.db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0
                and self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0):
            self.db.execute("INSERT INTO chunks_fts (text, path, idx) SELECT text, path, idx FROM chunks")
        self.db.commit()
        self._matrix: np.ndarray | None = None
        self._rows: list[tuple[str, str, int]] = []

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
                self.db.execute("DELETE FROM chunks_fts WHERE path = ?", (key,))
                self.db.executemany(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                    [(key, mtime, i, c, np.asarray(v, dtype=np.float32).tobytes())
                     for i, (c, v) in enumerate(zip(chunks, vecs))],
                )
                self.db.executemany("INSERT INTO chunks_fts (text, path, idx) VALUES (?, ?, ?)",
                                    [(c, key, i) for i, c in enumerate(chunks)])
                self.db.commit()
            changed += 1
        removed = [p for p in known if p not in seen]
        with self.lock:
            self.db.executemany("DELETE FROM chunks WHERE path = ?", [(p,) for p in removed])
            self.db.executemany("DELETE FROM chunks_fts WHERE path = ?", [(p,) for p in removed])
            self.db.commit()
            self._matrix = None
        if changed or removed:
            log(f"  [brain] indexed {changed} files, removed {len(removed)}, {self.count()} chunks total")
        return changed

    def _load(self) -> None:
        with self.lock:
            rows = self.db.execute("SELECT path, text, vec, idx FROM chunks").fetchall()
        self._rows = [(p, t, i) for p, t, _, i in rows]
        if rows:
            m = np.stack([np.frombuffer(v, dtype=np.float32) for _, _, v, _ in rows])
            self._matrix = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
        else:
            self._matrix = np.zeros((0, 1), dtype=np.float32)

    def _keyword_ranks(self, query: str, limit: int = 20) -> list[tuple[str, int]]:
        """(path, chunk index) of the best keyword matches, best first (SQLite FTS5, BM25)."""
        terms = [t for t in re.findall(r"[\w\-]+", query.lower()) if len(t) > 1 and t not in STOPWORDS]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        with self.lock:
            try:
                return self.db.execute("SELECT path, idx FROM chunks_fts WHERE chunks_fts MATCH ? "
                                       "ORDER BY rank LIMIT ?", (match, limit)).fetchall()
            except sqlite3.OperationalError:
                return []

    def hits(self, query: str, k: int = 4) -> list[dict]:
        """Hybrid search: meaning and keywords, merged by reciprocal rank fusion.

        Each ranking alone misses things: embeddings blur exact names and error codes, keywords
        miss paraphrases. A chunk scores 1/(60+rank) in each list, so one found by both rises to
        the top. If the embedding model is down, keywords still answer.
        """
        if self._matrix is None:
            self._load()
        if not len(self._rows):
            return []
        cosine = np.zeros(len(self._rows), dtype=np.float32)
        fused: dict[int, float] = {}
        try:
            q = np.asarray(self.embed([self.query_prefix + query])[0], dtype=np.float32)
            cosine = self._matrix @ (q / (np.linalg.norm(q) + 1e-9))
            for rank, i in enumerate(np.argsort(-cosine)[:20]):
                fused[int(i)] = 1 / (60 + rank)
        except Exception as exc:
            print(f"  [brain] meaning search unavailable, using keywords only: {exc}")
        where = {(p, i): n for n, (p, _, i) in enumerate(self._rows)}
        keyword = set()
        for rank, key in enumerate(self._keyword_ranks(query)):
            n = where.get((key[0], int(key[1])))
            if n is not None:
                keyword.add(n)
                fused[n] = fused.get(n, 0) + 1 / (60 + rank)
        best = sorted(fused, key=fused.get, reverse=True)[:k]
        out = []
        for n in best:
            path, text, idx = self._rows[n]
            # A keyword hit counts as relevant even when its meaning score alone wouldn't.
            score = max(float(cosine[n]), 0.35 if n in keyword else 0.0)
            out.append({"score": score, "path": path, "text": text, "part": idx + 1, "both": n in keyword and cosine[n] >= 0.3})
        return out

    def search(self, query: str, k: int = 4) -> list[tuple[float, str, str]]:
        """Return (score, path, text) for the k best chunks."""
        return [(h["score"], h["path"], h["text"]) for h in self.hits(query, k)]

    def answer_context(self, query: str, k: int = 4, min_score: float = 0.3, on_sources=None) -> str:
        hits = [h for h in self.hits(query, k) if h["score"] >= min_score]
        if not hits:
            return "No matching notes found."
        if on_sources:
            on_sources([{"name": Path(h["path"]).name, "path": h["path"], "part": h["part"],
                         "snippet": re.sub(r"\s+", " ", h["text"])[:160]} for h in hits])
        return "\n\n".join(f"From {Path(h['path']).name}, part {h['part']} (match {h['score']:.2f}):\n{h['text']}"
                           for h in hits)
