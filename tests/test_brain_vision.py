import base64
import hashlib

import numpy as np

from jarvis.knowledge import KnowledgeBase, chunk_text
from jarvis.vision import ScreenVision, encode_png

TOPICS = ["auth", "token", "login", "recursion", "factorial", "base case", "pizza", "dough", "oven"]


def fake_embed(texts):
    """Bag-of-topics embedding: similar words land close together, no model needed."""
    out = []
    for t in texts:
        v = np.array([t.lower().count(w) for w in TOPICS], dtype=np.float32)
        h = int(hashlib.md5(t.encode()).hexdigest(), 16) % 1000 / 1e5
        out.append((v + h).tolist())
    return out


def test_chunking_respects_size_and_keeps_everything():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 40 for i in range(20))
    chunks = chunk_text(text, size=500, overlap=50)
    assert len(chunks) > 3
    assert all(len(c) <= 700 for c in chunks)
    assert "Paragraph 19" in chunks[-1]


def test_brain_indexes_incrementally_and_finds_by_meaning(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "auth.md").write_text("Fixed the login bug: the auth token expired too early.")
    (notes / "cs.md").write_text("Recursion needs a base case, like factorial of zero.")
    (notes / "food.txt").write_text("Pizza dough rests overnight, then the oven goes hot.")
    (notes / "node_modules").mkdir()
    (notes / "node_modules" / "junk.js").write_text("auth auth auth")

    kb = KnowledgeBase(tmp_path / "brain.sqlite", fake_embed)
    assert kb.index([notes], log=lambda m: None) == 3
    assert kb.index([notes], log=lambda m: None) == 0  # nothing changed, nothing re-embedded

    score, path, text = kb.search("where did I fix the login token thing", k=1)[0]
    assert path.endswith("auth.md")
    assert "From auth.md" in kb.answer_context("auth token login", k=1)

    (notes / "food.txt").unlink()
    kb.index([notes], log=lambda m: None)
    assert all(not p.endswith("food.txt") for _, p, _ in kb.search("pizza oven", k=5))


def test_screen_vision_sends_a_downscaled_png_to_the_vision_model():
    from PIL import Image

    sent = {}

    class FakeLLM:
        def vision(self, model, prompt, image_b64):
            sent.update(model=model, prompt=prompt, png=base64.b64decode(image_b64))
            return "VS Code with a red TypeError in the terminal."

    def capture(max_width, monitor):
        return encode_png(Image.new("RGB", (3200, 1800), "white"), max_width)

    vision = ScreenVision(FakeLLM(), {"model": "qwen2.5vl:7b", "max_width": 1600}, capture=capture)
    assert "TypeError" in vision.look("why is this failing")
    assert sent["model"] == "qwen2.5vl:7b"
    assert "why is this failing" in sent["prompt"]
    import io
    assert Image.open(io.BytesIO(sent["png"])).size == (1600, 900)
