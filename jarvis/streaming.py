"""Turn a stream of LLM tokens into speakable sentences as early as possible.

Speaking the first sentence while the model is still writing the rest is what
makes Jarvis feel instant: the delay you hear is one sentence, not a paragraph.
"""

from __future__ import annotations

import re

from jarvis.persona import speakable

_BOUNDARY = re.compile(r"(?<=[.!?])[\"')\]*_]*\s+|\n\s*\n|\n(?=\s*(?:[-*+•]|\d+[.)])\s)")
_CODE_NOTE = "I've left the code on screen."


class SentenceStream:
    """Feed token deltas in, get finished sentences out.

    Code fences are swallowed (with a single spoken note), since nobody wants
    Python read aloud.
    """

    def __init__(self, min_chars: int = 8):
        self.buf = ""
        self.min_chars = min_chars
        self.in_code = False
        self.code_noted = False

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out: list[str] = []
        while True:
            if self.in_code:
                end = self.buf.find("```")
                if end == -1:
                    return out
                self.buf = self.buf[end + 3 :]
                self.in_code = False
                continue
            fence = self.buf.find("```")
            text = self.buf if fence == -1 else self.buf[:fence]
            match = None
            for m in _BOUNDARY.finditer(text):
                if m.start() >= self.min_chars:
                    match = m
                    break
            if match:
                out += self._emit(text[: match.start()])
                self.buf = self.buf[match.end() :]
                continue
            if fence != -1:
                out += self._emit(text)
                if not self.code_noted:
                    out.append(_CODE_NOTE)
                    self.code_noted = True
                self.buf = self.buf[fence + 3 :]
                self.in_code = True
                continue
            return out

    def flush(self) -> list[str]:
        rest = "" if self.in_code else self.buf
        self.buf = ""
        return self._emit(rest)

    @staticmethod
    def _emit(text: str) -> list[str]:
        clean = speakable(text)
        return [clean] if clean else []
