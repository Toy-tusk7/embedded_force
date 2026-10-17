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

PAGE1 = ("1. Overview\nThe sensor measures pressure. " + "It is small. " * 60 + "\nTable 3: Operating conditions\n\n"
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
    assert [(f.label, f.caption, f.section) for f in figs] == [("Table 3", "Operating conditions", "1"),
                                                             ("Figure 3", "Normal mode timing diagram", "2.1")]
    assert xrefs(text) == ["2.2"]
    need = [(next(s for s in secs if s.num == "2.1"), "the code sets mode = normal", "modes")]
    added = with_xrefs(text, secs, need)[1:]
    assert [(s.num, why, topic) for s, why, topic in added] == [("2.2", "section 2.1 refers to it", "modes")]
    pdf = tmp_path / "ds.pdf"
    _pdf(pdf)
    assert vision.find_page(pdf, "Figure 3", "Normal mode timing diagram") == 1
    assert vision.render_page(pdf, 1)[:4] == b"\x89PNG"


def test_a_figure_is_cut_from_a_page_it_shares(tmp_path):
    """Two timing diagrams on one page (the BMP388's forced and normal mode): each is cut out with its caption, without
    the other or the paragraph between them; a table's caption is over the table."""
    doc = pymupdf.open()
    page = doc.new_page()   # 595 x 842
    wide = "This paragraph of body text runs across the page, as datasheet paragraphs do, from margin to margin."
    page.insert_text((40, 60), wide, fontsize=10)
    page.draw_rect(pymupdf.Rect(150, 90, 400, 200))
    page.insert_text((160, 150), "Mode[1:0] = 01", fontsize=8)
    page.insert_text((220, 225), "Figure 2: Force mode timing diagram", fontsize=10)
    page.insert_text((40, 280), wide, fontsize=10)
    page.draw_rect(pymupdf.Rect(150, 310, 400, 420))
    page.insert_text((160, 370), "Mode[1:0] = 11", fontsize=8)
    page.insert_text((220, 445), "Figure 3: Normal mode timing diagram", fontsize=10)
    page.insert_text((220, 500), "Table 4: Settings", fontsize=10)
    page.insert_text((150, 530), "osr_p   x2", fontsize=9)
    page.insert_text((150, 545), "odr     50 Hz", fontsize=9)
    page.insert_text((220, 600), "Figure 5: Measurement cycle", fontsize=10)
    fig3, fig2, tab4 = (vision.figure_clip(page, x) for x in ("Figure 3", "Figure 2", "Table 4"))
    assert 280 < fig3.y0 < 310 and 445 < fig3.y1 < 460   # under the paragraph, down to its caption
    assert fig2.y0 < 90 and 225 < fig2.y1 < 240           # Figure 3's diagram is not in it
    assert 485 < tab4.y0 < 500 and 545 < tab4.y1 < 600     # from its caption to the next one
    assert vision.figure_clip(page, "Figure 9") is None
    path = tmp_path / "two.pdf"
    doc.save(str(path))
    whole, cut = vision.render_page(path, 1), vision.render_figure(path, 1, "Figure 3")
    assert cut[:4] == b"\x89PNG" and pymupdf.Pixmap(cut).height < pymupdf.Pixmap(whole).height / 2


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
    rec = belt.ledger.record("DM388", "mode_normal", "11", derived.id, "Write PWR_CTRL mode = 11 (normal)")
    assert rec["ok"] and not rec["fact"]["authoritative"] and "vision model" in " ".join(rec["warnings"])
    # Table 3 comes first in the document: asking for Figure 3 must not get it, asking for Table 3 must
    assert belt.call("read_figure", {"source_id": src.id, "figure": "Table 3"})["figure"] == "Table 3: Operating conditions"
    sec = belt.call("read_section", {"source_id": src.id, "section": "2.1"})
    assert sec["sections"][0]["figures"] == [f"Figure 3: Normal mode timing diagram — read_figure('{src.id}', 'Figure 3')"]
    missing = belt.call("read_figure", {"source_id": src.id, "figure": "Figure 9"})
    assert "Captions: Table 3: Operating conditions; Figure 3: Normal mode" in missing["error"]


def test_without_the_pdf_the_text_around_the_caption_is_returned(tmp_path):
    p = Project("t", tmp_path / "t")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/ds.pdf", "datasheet", PAGE1 + PAGE2, kind="pdf")
    belt = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store))
    r = belt.call("read_figure", {"source_id": src.id, "figure": "timing diagram"})
    assert "not stored" in r["error"] and "Write the settings while asleep" in r["text_around_caption"]
