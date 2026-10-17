"""Reading datasheet figures: find the page of a figure in the stored PDF, render it, and have a vision model
transcribe it into text (sequences in order, labels, values, arrows). Datasheets put rules in figures that text
extraction loses (the BMP388's 'write settings, then select normal mode' is a timing diagram); only the figures of the
sections the code depends on are read, so a 100-page datasheet costs a few calls."""

import base64
import re
from pathlib import Path
from typing import Callable, Optional, Tuple

FIGURE_PROMPT = (
    "This is a page of a component datasheet. Transcribe the figure or table captioned \"{caption}\" into text, "
    "exactly as drawn: for a timing or sequence diagram, every step in order with its condition, register or field "
    "values and times; for a state or mode diagram, every state and every transition with what triggers it; for a "
    "table, its rows. Copy labels and numbers exactly. Do not add anything the figure does not show. Plain text or "
    "Markdown only.")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def find_page(pdf_path: Path, label: str, caption: str, hint: int = 0) -> int:
    """1-based page whose text carries the caption ('Figure 3' + its first words); 0 when none does."""
    import pymupdf
    want_label, want_cap = _norm(label), _norm(caption)[:24]
    with pymupdf.open(str(pdf_path)) as doc:
        order = ([hint - 1] if 0 < hint <= doc.page_count else []) + [i for i in range(doc.page_count) if i != hint - 1]
        for i in order:
            text = _norm(doc[i].get_text())
            if want_label in text and want_cap in text:
                return i + 1
    return 0


def render_page(pdf_path: Path, page: int, dpi: int = 130) -> bytes:
    import pymupdf
    with pymupdf.open(str(pdf_path)) as doc:
        return doc[page - 1].get_pixmap(dpi=dpi).tobytes("png")


def default_reader() -> Tuple[Callable[[str, bytes], str], str]:
    """(prompt, png) -> text with the configured vision model (EMBEDFORGE_VISION_PROVIDER / _MODEL), and its name."""
    from embedforge.agent.llm import AgentLLM
    from embedforge.core.config import config
    llm = AgentLLM(config.vision_model, config.vision_provider)

    def ask(prompt: str, png: bytes) -> str:
        url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        msg = {"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": url}}]}
        return llm.chat([msg], None, max_tokens=4096, retries=3)["content"]
    return ask, llm.model


def transcribe(pdf_path: Path, label: str, caption: str, hint: int = 0,
               reader: Optional[Callable[[str, bytes], str]] = None) -> Tuple[str, int]:
    """(transcription, page) of a figure; ('', 0) when its page is not found."""
    page = find_page(pdf_path, label, caption, hint)
    if not page:
        return "", 0
    ask = reader or default_reader()[0]
    return ask(FIGURE_PROMPT.format(caption=f"{label}: {caption}"), render_page(pdf_path, page)).strip(), page
