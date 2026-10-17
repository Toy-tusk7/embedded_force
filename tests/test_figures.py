"""Datasheet figures and cross-references: captions are indexed with their section, the stored PDF is searched for
the caption's page and rendered, a vision model transcribes it (here a stand-in), and sections a required section
refers to become required too."""

import pytest

from embedforge.agent import vision
from embedforge.agent.events import RunSession
from embedforge.agent.tools import ToolBelt
from embedforge.agent.workspace import Project
from embedforge.truth import FactLedger, SourceStore
from embedforge.truth.outline import figures, sections, with_xrefs, xrefs

pymupdf = pytest.importorskip("pymupdf")   # the 'pdf' extra

PAGE1 = ("1. Overview\nThe sensor measures pressure. " + "It is small. " * 60 + "\n\n"
         "2. Operation\nThe device has two modes. " + "Details follow. " * 50 + "\n\n"
         "2.1. Normal mode\nWrite the settings while asleep. Data timing: see Sec. 2.2 for the conversion time. "
         + "More text here. " * 30 + "\nFigure 3: Normal mode timing diagram\n")
PAGE2 = ("2.2. Conversion time\nThe conversion takes at most 9 ms. " + "Table rows follow. " * 40 + "\n\n"
         "3. Registers\nRegister 0x1B PWR_CTRL. " + "Bit fields. " * 40 + "\n")


def _pdf(path):
    doc = pymupdf.open()
    for body in (PAGE1, PAGE2):
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 560, 800), body, fontsize=8)
    doc.save(str(path))


def test_captions_pages_and_cross_references(tmp_path):
    text = PAGE1 + PAGE2
    secs = sections(text)
    figs = figures(text, secs)
    assert [(f.label, f.caption, f.section) for f in figs] == [("Figure 3", "Normal mode timing diagram", "2.1")]
    assert xrefs(text) == ["2.2"]
    need = [(next(s for s in secs if s.num == "2.1"), "the code sets mode = normal", "modes")]
    added = with_xrefs(text, secs, need)[1:]
    assert [(s.num, why, topic) for s, why, topic in added] == [("2.2", "section 2.1 refers to it", "modes")]
    pdf = tmp_path / "ds.pdf"
    _pdf(pdf)
    assert vision.find_page(pdf, "Figure 3", "Normal mode timing diagram") == 1
    assert vision.render_page(pdf, 1)[:4] == b"\x89PNG"


def test_read_figure_transcribes_once_and_marks_its_source(tmp_path):
    p = Project("t", tmp_path / "t")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/ds.pdf", "datasheet", PAGE1 + PAGE2, kind="pdf")
    pdf = tmp_path / "ds.pdf"
    _pdf(pdf)
    store.keep_pdf(src.id, pdf.read_bytes())
    belt = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store))
    calls = []

    def fake(prompt, png):
        calls.append((prompt, png[:4]))
        return "1. Write OSR and ODR (sleep mode)\n2. Write PWR_CTRL mode = 11 (normal)\n3. Read data when needed"
    belt.vision = fake
    r = belt.call("read_figure", {"source_id": src.id, "figure": "Figure 3"})
    assert r["page"] == 1 and r["section"] == "2.1" and r["transcription"].startswith("1. Write OSR and ODR")
    assert calls[0][1] == b"\x89PNG" and "Normal mode timing diagram" in calls[0][0]
    derived = store.get(r["source_id"])
    assert derived.url == f"figure://{src.id}/figure-3" and derived.tier == "UNOFFICIAL" and "vision model" in r["note"]
    again = belt.call("read_figure", {"source_id": src.id, "figure": "fig 3"})
    assert again["transcription"] == r["transcription"] and len(calls) == 1   # cached: one model call per figure
    sec = belt.call("read_section", {"source_id": src.id, "section": "2.1"})
    assert sec["sections"][0]["figures"] == [f"Figure 3: Normal mode timing diagram — read_figure('{src.id}', 'Figure 3')"]
    missing = belt.call("read_figure", {"source_id": src.id, "figure": "Figure 9"})
    assert "Captions: Figure 3" in missing["error"]


def test_without_the_pdf_the_text_around_the_caption_is_returned(tmp_path):
    p = Project("t", tmp_path / "t")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/ds.pdf", "datasheet", PAGE1 + PAGE2, kind="pdf")
    belt = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store))
    r = belt.call("read_figure", {"source_id": src.id, "figure": "timing diagram"})
    assert "not stored" in r["error"] and "Write the settings while asleep" in r["text_around_caption"]
