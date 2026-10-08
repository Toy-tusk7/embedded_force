"""Datasheet PDF -> Markdown: table flattening, equation reconstruction from glyph geometry, and the end-to-end path."""

import struct

import pytest

from embedforge.truth.ledger import _squash
from embedforge.truth.pdf_markdown import (Run, _math_variants, _plain_markup, _rebuild_page, _render_line, formulas_from_runs,
                                           html_table_to_markdown, html_tables_to_markdown)


def test_merged_cells_are_repeated_in_every_row():
    html = ("<table><tr><th>Addr</th><th>Content</th><th>Size</th><th>Sign</th></tr>"
            "<tr><td>0x43</td><td>NVM_PAR_P9&lt;15:8&gt;</td><td rowspan=\"2\">16<br/>Bit</td><td rowspan=\"2\">signed</td></tr>"
            "<tr><td>0x42</td><td>NVM_PAR_P9&lt;7:0&gt;</td></tr></table>")
    md = html_table_to_markdown(html)
    assert md.splitlines() == ["| Addr | Content | Size | Sign |", "|---|---|---|---|",
                               "| 0x43 | NVM_PAR_P9<15:8> | 16 Bit | signed |", "| 0x42 | NVM_PAR_P9<7:0> | 16 Bit | signed |"]
    assert "| a \\| b |" in html_tables_to_markdown("x <table><tr><td>a | b</td></tr></table> y")


def _r(text, x0, base, size=10.0, width=None):
    return Run(text, x0, x0 + (width if width is not None else 5.0 * len(text)), base, size)


def test_fraction_with_exponent_and_offset():
    # PAR_P1 = (NVM_PAR_P1 - 2^14) / 2^20, laid out as Word draws it: numerator above, denominator below the '=' line
    runs = [_r("PAR_P1 =", 90, 276.4), _r("NVM_PAR_P1 - 2", 138, 268.7), _r("14", 210, 265.0, 7.0),
            _r("2", 170, 283.1), _r("20", 176, 280.1, 7.0)]
    assert [f for _, _, f in formulas_from_runs(runs)] == ["PAR_P1 = (NVM_PAR_P1 - 2^14) / 2^20"]


def test_two_equations_in_a_row_and_subscripts():
    runs = [_r("PAR_T1 =", 101, 276.4), _r("NVM_PAR_T1", 149, 268.7), _r("2", 171, 283.1), _r("-8", 177, 280.1, 7.0),
            _r("PAR_T2 =", 243, 276.4), _r("NVM_PAR_T2", 290, 268.7), _r("2", 314, 283.1), _r("30", 319, 280.1, 7.0),
            _r("τ", 200, 339.0, width=4), _r("sampling", 205, 341.0, 7.0), _r("= prescaler/f", 240, 339.0),
            _r("sampling", 305, 341.0, 7.0)]
    got = sorted(f for _, _, f in formulas_from_runs(runs))
    assert got == ["PAR_T1 = NVM_PAR_T1 / 2^-8", "PAR_T2 = NVM_PAR_T2 / 2^30", "τ_sampling = prescaler/f_sampling"]


def test_denominator_wider_than_the_numerator():
    # f_max = 1 / (T_conv [μs] * 10^6): the '1' sits over the right part of the denominator only
    runs = [_r("1", 310.9, 249.0), _r("f", 250.9, 256.7), _r("max", 254.5, 258.7, 7.0), _r("=", 272.9, 256.7),
            _r("T", 283.2, 263.4), _r("conv", 287.8, 265.4, 7.0), _r("[μs] * 10", 304.6, 263.4, width=35.1),
            _r("6", 339.7, 260.4, 7.0)]
    assert [f for _, _, f in formulas_from_runs(runs)] == ["f_max = 1 / (T_conv [μs] * 10^6)"]


def test_stretchy_glyph_variants_from_the_math_table():
    # a minimal font: one MATH table whose '(' (gid 5) has a size variant (300) and an assembly of two parts (301, 302)
    u = lambda *v: struct.pack(f">{len(v)}H", *v)
    variants = (u(0, 12, 0, 1, 0, 18) + u(1, 1, 5) + u(12, 2, 5, 100, 300, 200)
                + u(0, 0, 2) + u(301, 0, 0, 0, 0) + u(302, 0, 0, 0, 0))
    table = u(1, 0, 0, 0, 10) + variants
    face = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0) + b"MATH" + struct.pack(">III", 0, 28, len(table)) + table
    assert _math_variants(face, {5: ord("(")}) == {5: ord("("), 300: ord("("), 301: ord("("), 302: ord("(")}


def _chars(text, x0, base, size=10.0):
    return [(c, size, base, (x0 + 5 * i, base - size, x0 + 5 * i + 5, base)) for i, c in enumerate(text)]


def test_rendered_line_marks_exponents_and_indices():
    line = _chars("2", 10, 100) + _chars("osr_p", 15, 96, 7.0) + _chars(" = T", 40, 100) + _chars("conv", 60, 102, 7.0)
    assert _render_line(line) == "2^osr_p = T_conv"


def test_garbled_box_is_rewritten_and_formula_follows_its_text():
    md = "# Title\n\nThe time 𝑝𝑝𝑝 is set.\n\nNext paragraph.\n\n"
    p1, p2 = md.index("The"), md.index("Next")
    boxes = [{"class": "section-header", "bbox": (0, 0, 100, 10), "pos": (0, p1)},
             {"class": "text", "bbox": (0, 20, 200, 30), "pos": (p1, p2)},
             {"class": "text", "bbox": (0, 60, 200, 70), "pos": (p2, len(md))}]
    lines = [_chars("The time T", 0, 30) + _chars("conv", 50, 32, 7.0) + _chars(" is set.", 70, 30)]
    out = _rebuild_page(md, boxes, lines, [(35.0, 10.0, "T_conv = 1")])
    assert out == ("# Title\n\nThe time T_conv is set.\n\n*Formulas (reconstructed from the PDF's equation glyphs):*\n\n"
                   "`T_conv = 1`  \n\nNext paragraph.\n\n")


def test_markup_that_would_change_names_is_removed():
    md = "burst read from register _press_xlsb_ to _temp_msb_ . <mark>x = 1;</mark> <u>Table 24</u>"
    assert _plain_markup(md) == "burst read from register press_xlsb to temp_msb . x = 1; Table 24"
    row = "| 50 | 0x32 | NVM_PAR _T1<15:8> | NVM_PAR _T1 | 16 Bit | unsigned |"   # the PDF's own spacing stays
    assert _plain_markup(row) == row
    # quotes match with or without the converter's markup
    assert _squash("2^osr_p * 2000µs") == _squash("2<sup>osr_p</sup> ∙ 2000μs".replace("∙", "*"))
    assert _squash("| Bit 5..4 | mode | 01/10 | forced mode |") == _squash("Bit 5..4 mode 01/10 forced mode")


def test_pdf_to_markdown_end_to_end(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    pytest.importorskip("pymupdf4llm")
    from embedforge.truth.pdf_markdown import pdf_to_markdown
    from embedforge.truth.source_store import pdf_to_text
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "4.3.16 Register 0x1B PWR_CTRL", fontsize=14)
    page.insert_text((72, 100), "Bit 5..4 mode: 00 sleep mode, 11 normal mode.", fontsize=10)
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="")   # like Bosch datasheets
    md = pdf_to_markdown(data)
    assert md.startswith("<!-- PDF page 1 -->") and "Register 0x1B PWR_CTRL" in md and "11 normal mode" in md
    assert "Register 0x1B" in pdf_to_text(data)
