import os
import sqlite3
import time
import zipfile

import pytest

from jarvis import laptop
from jarvis.laptop import LaptopIndex, extract_text, is_denied


def docx(path, text):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f"<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "SAINT"
    (root / "Documents/School").mkdir(parents=True)
    (root / "Documents/School/ClimateEssay_final.docx").parent.mkdir(exist_ok=True)
    docx(root / "Documents/School/ClimateEssay_final.docx", "Rising sea levels threaten Kigali wetlands")
    (root / "Documents/notes.md").write_text("Standup moved to 10:30. Ask Amani about the catalog agent.")
    (root / "code/app").mkdir(parents=True)
    (root / "code/app/main.py").write_text("def launch_rocket():\n    return 'whoosh'\n")
    (root / "code/app/node_modules/pkg").mkdir(parents=True)
    (root / "code/app/node_modules/pkg/index.js").write_text("launch_rocket")
    (root / "code/app/.env").write_text("GROQ_API_KEY=gsk_secret")
    (root / "Videos").mkdir()
    (root / "Videos/holiday.mp4").write_bytes(b"0" * 50_000)
    (root / ".ssh").mkdir()
    (root / ".ssh/id_ed25519").write_text("PRIVATE KEY")
    return root


def index(tmp_path, home):
    return LaptopIndex(tmp_path / "laptop.sqlite", [str(home)])


def test_scan_finds_files_by_name_and_by_the_words_inside(tmp_path, home):
    idx = index(tmp_path, home)
    result = idx.scan()
    assert result["added"] == 5  # node_modules and .ssh skipped; .env indexed by name only
    hit = idx.search("climate essay")[0]
    assert hit["path"].endswith("ClimateEssay_final.docx")
    hit = idx.search("sea levels kigali")[0]  # words inside the Word file
    assert hit["path"].endswith("ClimateEssay_final.docx") and "[" in hit["snippet"]
    assert idx.search("launch_rocket")[0]["path"].endswith("main.py")
    assert all("node_modules" not in h["path"] for h in idx.search("launch_rocket"))
    assert idx.search("gsk_secret") == []  # a .env file's contents are never indexed
    assert idx.search("holiday", kind="video")[0]["kind"] == "video"
    assert idx.search("holiday", kind="document") == []


def test_rescan_picks_up_changes_and_forgets_deleted_files(tmp_path, home):
    idx = index(tmp_path, home)
    idx.scan()
    (home / "Documents/notes.md").write_text("Dentist on Friday")
    future = time.time() + 10
    os.utime(home / "Documents/notes.md", (future, future))
    (home / "Videos/holiday.mp4").unlink()
    result = idx.scan()
    assert result["updated"] == 1 and result["removed"] == 1
    assert idx.search("dentist")[0]["path"].endswith("notes.md")
    assert idx.search("standup") == [] and idx.search("holiday") == []
    assert LaptopIndex(tmp_path / "laptop.sqlite", [str(home)]).count() == 4  # kept across restarts


def test_recent_largest_and_overview(tmp_path, home):
    idx = index(tmp_path, home)
    idx.scan()
    assert idx.largest(1)[0]["path"].endswith("holiday.mp4")
    assert len(idx.recent(days=1)) == 5
    o = idx.overview()
    assert o["files"] == 5 and o["kinds"][0]["kind"] == "video" and o["last_scan"]


def test_secrets_and_outside_folders_are_never_read(tmp_path, home):
    idx = index(tmp_path, home)
    assert is_denied(home / ".ssh/id_ed25519") and is_denied(home / "code/app/.env")
    assert is_denied(r"C:\Users\SAINT\AppData\Local\Google\Chrome\User Data\Default\Login Data")
    assert not is_denied(home / "Documents/notes.md")
    tools = {t.name: t for t in laptop.tools(idx, browser=False)}
    assert "Standup" in tools["read_file"].handler({"path": str(home / "Documents/notes.md")})
    assert "never read" in tools["read_file"].handler({"path": str(home / "code/app/.env")})
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("hidden")
    assert "can't open" in tools["read_file"].handler({"path": str(outside)})
    listing = tools["list_folder"].handler({"path": str(home / "code/app")})
    assert "main.py" in listing and ".env" not in listing


def test_tools_answer_in_plain_text(tmp_path, home):
    idx = index(tmp_path, home)
    tools = {t.name: t for t in laptop.tools(idx, browser=False)}
    assert "first scan" in tools["find_files"].handler({"query": "climate"})  # before the scan finishes
    idx.scan()
    out = tools["find_files"].handler({"query": "climate"})
    assert "ClimateEssay_final.docx" in out and "first scan" not in out
    assert "Indexed 5 files" in tools["laptop_overview"].handler({})
    assert "holiday.mp4" in tools["largest_files"].handler({})
    assert "Memory" in tools["laptop_details"].handler({})
    assert "Using the most memory" in tools["running_programs"].handler({})


def test_office_and_notebook_text(tmp_path):
    with zipfile.ZipFile(tmp_path / "deck.pptx", "w") as z:
        z.writestr("ppt/slides/slide1.xml", "<p:sld><a:p><a:t>Quarterly goals</a:t></a:p></p:sld>")
    with zipfile.ZipFile(tmp_path / "sheet.xlsx", "w") as z:
        z.writestr("xl/sharedStrings.xml", "<sst><si><t>Budget 2026</t></si></sst>")
    (tmp_path / "nb.ipynb").write_text('{"cells": [{"source": ["import pandas"]}]}')
    assert "Quarterly goals" in extract_text(tmp_path / "deck.pptx")
    assert "Budget 2026" in extract_text(tmp_path / "sheet.xlsx")
    assert extract_text(tmp_path / "nb.ipynb") == "import pandas"


def test_browser_history_and_bookmarks(tmp_path, monkeypatch):
    prof = tmp_path / "Default"
    prof.mkdir()
    (prof / "Bookmarks").write_text('{"roots": {"bookmark_bar": {"type": "folder", "name": "Bar", "children": ['
                                    '{"type": "url", "name": "Groq console", "url": "https://console.groq.com"}]}}}')
    db = sqlite3.connect(prof / "History")
    db.execute("CREATE TABLE urls (title TEXT, url TEXT, last_visit_time INTEGER, visit_count INTEGER)")
    now = int((time.time() + 11644473600) * 1_000_000)
    db.execute("INSERT INTO urls VALUES ('Tailscale download', 'https://tailscale.com/download', ?, 3)", (now,))
    db.execute("INSERT INTO urls VALUES ('Old page', 'https://old.example', ?, 1)", (now - 90 * 86400 * 10**6,))
    db.commit()
    db.close()
    monkeypatch.setattr(laptop, "browser_profiles", lambda: [("Chrome Default", prof)])
    assert laptop.bookmarks("groq")[0]["url"] == "https://console.groq.com"
    hits = laptop.history("tailscale")
    assert len(hits) == 1 and hits[0]["visits"] == 3
    assert laptop.history("old") == []  # older than 30 days


def test_instant_answers_speak_names_not_paths(tmp_path, home):
    from jarvis.online import KNOWLEDGE_SKIP
    from jarvis.router import Router

    idx = index(tmp_path, home)
    idx.scan()
    r = Router(routes=False)
    laptop.add_routes(r, idx)
    name, reply = r.answer("find my climate essay")
    assert name == "laptop" and reply.startswith("I found it: ClimateEssay_final.docx in School")
    assert r.answer("what's taking up space on my laptop")[1].startswith("Your biggest files: holiday.mp4 in Videos")
    assert r.answer("what did I work on today")[1].startswith("Most recently changed:")
    assert r.answer("look for zzqx nothing") is None  # nothing matched: the model gets a go
    # Questions about "my" things never go to the Wikipedia shortcut.
    assert KNOWLEDGE_SKIP.search("taking up space on my laptop") and not KNOWLEDGE_SKIP.search("capital of rwanda")


def test_a_broken_pdf_does_not_stop_the_scan(tmp_path, home):
    (home / "Documents/empty_scan.pdf").write_bytes(b"")
    (home / "Documents/corrupt.pdf").write_bytes(b"%PDF-1.4 garbage")
    idx = index(tmp_path, home)
    idx.scan()
    assert idx.search("climate")  # the rest of the laptop was still indexed
    assert idx.search("empty_scan")  # and the broken file is still findable by name
