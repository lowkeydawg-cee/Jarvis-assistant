import os
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


def hybrid_brain(tmp_path, embed=None):
    """Three files; a fake embedding that only knows two topics, so exact words need the keyword index."""
    from jarvis.knowledge import KnowledgeBase

    def fake_embed(texts):
        return [[1.0, 0.0] if ("sea" in t or "ocean" in t) else [0.0, 1.0] for t in texts]

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "ocean.md").write_text("The sea levels are rising along the coast.")
    (notes / "errors.md").write_text("Build failed with error E4021 in the payment module.")
    (notes / "misc.md").write_text("Groceries and a haircut on Saturday.")
    kb = KnowledgeBase(tmp_path / "b.sqlite", embed or fake_embed)
    kb.index([notes], log=lambda m: None)
    return kb


def test_hybrid_search_finds_exact_terms_that_meaning_misses(tmp_path):
    kb = hybrid_brain(tmp_path)
    hits = kb.hits("E4021", k=2)
    assert hits[0]["path"].endswith("errors.md") and hits[0]["part"] == 1  # keyword hit outranks the blur
    assert kb.hits("ocean", k=1)[0]["path"].endswith("ocean.md")  # meaning still works


def test_keywords_still_answer_when_the_embedding_model_is_down(tmp_path):
    kb = hybrid_brain(tmp_path)

    def down(texts):
        raise ConnectionError("ollama is off")

    kb.embed = down
    assert "Groceries" in kb.answer_context("haircut groceries")


def test_sources_are_reported_and_updates_reach_the_keyword_index(tmp_path):
    kb = hybrid_brain(tmp_path)
    got = []
    text = kb.answer_context("E4021", on_sources=got.extend)
    assert "errors.md, part 1" in text and got[0]["name"] == "errors.md" and "E4021" in got[0]["snippet"]
    f = tmp_path / "notes" / "errors.md"
    f.write_text("All fixed now, nothing to see.")
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))
    kb.index([tmp_path / "notes"], log=lambda m: None)
    assert kb._keyword_ranks("E4021") == []


def test_an_older_database_gets_its_keyword_index_built(tmp_path):
    from jarvis.knowledge import KnowledgeBase

    kb = hybrid_brain(tmp_path)
    kb.db.execute("DELETE FROM chunks_fts")
    kb.db.commit()
    kb.db.close()
    again = KnowledgeBase(tmp_path / "b.sqlite", kb.embed)
    assert again._keyword_ranks("E4021")
