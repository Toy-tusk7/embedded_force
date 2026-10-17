"""Source Store: every document the agent reads is stored verbatim with url + sha256.

This is the corpus the Fact Ledger validates quotes against. Nothing in here is ever
synthesised: text comes from Tavily results, direct HTTP fetches, PDF extraction, or the
user's own messages.
"""

import hashlib
import io
import json
import re
import threading
import time
from dataclasses import dataclass, asdict, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Any
import urllib.parse

import httpx

from embedforge.core.config import config
from embedforge.core.files import load_json, write_atomic
from embedforge.core.logger import logger
from embedforge.truth.trust import TrustTier, classify_url

MAX_PDF_PAGES = 600
MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024
_UA = "Mozilla/5.0 (compatible; EmbedForge/1.0; +https://github.com/embedforge)"


@dataclass
class Source:
    id: str
    url: str
    title: str
    tier: str
    sha256: str
    chars: int
    kind: str = "web"  # web | pdf | user | figure (a vision model's transcription of a datasheet figure)
    via: str = ""             # how the text was obtained: tavily:search | tavily:extract | download | ...
    tavily_request: str = ""  # Tavily request_id of the call that returned it (provenance, support)
    text: str = field(default="", repr=False)

    def public(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("text", None)
        return d


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag in ("p", "br", "tr", "li", "div", "h1", "h2", "h3", "h4", "table"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    ex = _TextExtractor()
    try:
        ex.feed(html)
    except Exception:
        pass
    return re.sub(r"[ \t]+", " ", "".join(ex.parts)).strip()


def pdf_to_text(data: bytes) -> str:
    """Datasheet text for the analyzer: Markdown (headings, tables, reconstructed equations) when PyMuPDF/pymupdf4llm are
    installed (`pip install embedforge[pdf]`), otherwise pypdf's layout text."""
    try:
        from embedforge.truth.pdf_markdown import pdf_to_markdown
        md = pdf_to_markdown(data, max_pages=MAX_PDF_PAGES)
        if md and md.strip():
            return md
    except Exception:
        pass
    return _pypdf_text(data)


def _pypdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    out = []
    for i, page in enumerate(reader.pages):
        if i >= MAX_PDF_PAGES:
            break
        page_text = ""
        # 1. Primary: 2D Spatial Layout mode preserves table column alignment
        try:
            page_text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            pass

        # 2. Fallback: Standard stream extraction
        if not page_text.strip():
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""

        # 3. Detect Scanned / Diagram / Photo pages
        num_images = len(getattr(page, "images", []))
        if len(page_text.strip()) < 80 and num_images > 0:
            page_text = f"[PAGE {i+1}: RASTER_DIAGRAM_PAGE ({num_images} embedded image/diagrams)]\n" + page_text

        out.append(page_text)
    return "\n".join(out)


def _tokens(s: str) -> List[str]:
    raw = re.findall(r"[a-z0-9_]+", s.lower())
    res = []
    for r in raw:
        res.append(r)
        if "_" in r:
            res.extend([p for p in r.split("_") if p])
    return res


HARDWARE_SYNONYMS: Dict[str, List[str]] = {
    "tim1": ["tmr1"], "tmr1": ["tim1"],
    "tim2": ["tmr2"], "tmr2": ["tim2"],
    "tim3": ["tmr3"], "tmr3": ["tim3"],
    "tim4": ["tmr4"], "tmr4": ["tim4"],
    "tim": ["tmr", "timer"], "tmr": ["tim", "timer"],
    "who_am_i": ["whoami", "devid", "device_id"],
    "whoami": ["who_am_i", "devid"],
    "slave_addr": ["slave_address", "i2c_addr", "i2c_address", "device_address"],
    "slave_address": ["slave_addr", "i2c_addr", "i2c_address"],
    "fd6288": ["fd6288q", "fd6288t"],
    "fd6288q": ["fd6288", "fd6288t"],
}


EXCLUDED_STORE_DOMAINS = [
    "lcsc.com", "jlcpcb.com", "ampheo.com", "chipdip.ru", "xonelec.com",
    "aliexpress.com", "amazon.com", "ebay.com", "hqonline.com", "findchips.com",
    "octopart.com", "globalsources.com", "made-in-china.com", "alibaba.com", "robu.in", "szlcsc.com",
    "wch.com", "manualslib.com", "yumpu.com", "scribd.com"
]

PART_OFFICIAL_CHANNELS: Dict[str, List[str]] = {
    "AT32": ["arterytek.com", "arterychip.com", "alldatasheet.com", "datasheetspdf.com", "github.com"],
    "STM32": ["st.com", "alldatasheet.com", "github.com"],
    "ESP32": ["espressif.com", "github.com"],
    "ESP8266": ["espressif.com", "github.com"],
    "RP2040": ["raspberrypi.com", "github.com"],
    "RP2350": ["raspberrypi.com", "github.com"],
    "NRF52": ["nordicsemi.com", "github.com"],
    "NRF53": ["nordicsemi.com", "github.com"],
    "CH32": ["wch-ic.com", "github.com"],
    "FD6288": ["fortiortech.com", "fd-ic.com", "alldatasheet.com", "datasheetspdf.com", "github.com"],
    "MPU6050": ["invensense.tdk.com", "tdk.com", "github.com"],
    "MPU6000": ["invensense.tdk.com", "tdk.com", "github.com"],
    "BME280": ["bosch-sensortec.com", "github.com"],
    "BMP280": ["bosch-sensortec.com", "github.com"],
    "BMI270": ["bosch-sensortec.com", "github.com"],
    "LIS3DH": ["st.com", "github.com"],
    "INA219": ["ti.com", "github.com"],
    "ADS1115": ["ti.com", "github.com"],
}


class SourceStore:
    """Per-project persistent store of retrieved documents."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / "sources"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.sources: Dict[str, Source] = {}
        self._by_url: Dict[str, str] = {}
        self._load()

    # ---------- persistence ----------
    def _index_path(self) -> Path:
        return self.dir / "index.json"

    def _load(self) -> None:
        try:
            for item in load_json(self._index_path(), []):   # an index that does not parse is kept aside
                txt_path = self.dir / f"{item['id']}.txt"
                text = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""
                s = Source(**{**item, "text": text})
                self.sources[s.id] = s
                self._by_url[s.url] = s.id
        except Exception as e:  # corrupted index must never crash the agent
            logger.log_event("SOURCE_STORE_WARN", f"Could not load index: {e}")

    def _save(self) -> None:
        write_atomic(self._index_path(), json.dumps([s.public() for s in self.sources.values()], indent=2))

    # ---------- core API ----------
    def add(self, url: str, title: str, text: str, kind: str = "web", replace: bool = False, via: str = "",
            tavily_request: str = "") -> Source:
        """Store a document. Re-adding a URL keeps the longer text unless `replace` (e.g. a re-imported schematic)."""
        with self._lock:
            existing_id = self._by_url.get(url)
            if existing_id and not replace and len(self.sources[existing_id].text) >= len(text):
                return self.sources[existing_id]
            sid = existing_id or f"SRC-{len(self.sources) + 1:03d}"
            # Preserve full technical text for datasheets; cap only extreme outliers (>2MB)
            max_chars = 2_000_000 if kind == "pdf" or url.lower().endswith(".pdf") else 200_000
            if len(text) > max_chars:
                text = text[:max_chars] + "\n...[truncated extreme document]"
            src = Source(
                id=sid,
                url=url,
                title=(title or url)[:200],
                tier=classify_url(url).value,
                sha256=hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest(),
                chars=len(text),
                kind=kind,
                via=via,
                tavily_request=tavily_request,
                text=text,
            )
            self.sources[sid] = src
            self._by_url[url] = sid
            (self.dir / f"{sid}.txt").write_text(text, encoding="utf-8")
            self._save()
            return src

    def get(self, source_id: str) -> Optional[Source]:
        return self.sources.get(source_id)

    def add_user_text(self, text: str) -> Source:
        """User statements are a first-class source (tier USER). Always the full current text: rebuilt from the user's own
        messages, so it can also shrink (e.g. when earlier harness text is no longer included)."""
        return self.add("user://conversation", "User statements", text, kind="user", replace=True)

    # ---------- retrieval ----------
    def excerpts(self, source_id: str, query: str, max_chunks: int = 4, chunk_chars: int = 1400) -> List[Dict[str, Any]]:
        src = self.sources.get(source_id)
        if not src:
            return []
        text = src.text
        step = max(chunk_chars - 350, 200)
        raw_terms = [t for t in set(_tokens(query)) if len(t) > 1]
        if not raw_terms:
            return [{"offset": 0, "text": text[:chunk_chars]}]

        # Expand hardware synonyms for terms in query
        expanded_terms = set(raw_terms)
        for t in raw_terms:
            if t in HARDWARE_SYNONYMS:
                expanded_terms.update(HARDWARE_SYNONYMS[t])

        phrase = query.lower().strip()
        scored = []
        for off in range(0, max(len(text), 1), step):
            chunk = text[off : off + chunk_chars]
            low = chunk.lower()
            toks = _tokens(chunk)
            if not toks:
                continue
            tokset = set(toks)
            matched_terms = [t for t in expanded_terms if t in tokset]
            if not matched_terms:
                continue
            # Multi-term coverage: bonus for matching more of the query's distinct concepts
            coverage = len(matched_terms) / max(len(raw_terms), 1)
            score = sum(1.0 + min(toks.count(t), 4) * 0.2 for t in matched_terms)
            score += coverage * 4.0
            if phrase and phrase in low:
                score += 5.0
            if score > 0:
                scored.append((score, off, chunk))
        scored.sort(key=lambda x: -x[0])
        return [{"offset": off, "score": round(sc, 2), "text": ch} for sc, off, ch in scored[:max_chunks]]

    # ---------- acquisition ----------
    def search(self, query: str, domains: Optional[List[str]] = None, max_results: int = 3) -> List[Dict[str, Any]]:
        if not config.has_tavily:
            raise RuntimeError("Tavily API key is not configured; cannot research. Set TAVILY_API_KEY.")

        low_q = query.lower()
        # 1. Local Cache Check: ONLY return cached source if it ACTUALLY contains matching passages for this query
        for part in PART_OFFICIAL_CHANNELS:
            if part.lower() in low_q:
                matching_cached = []
                for s in self.sources.values():
                    if part.lower() in (s.title + " " + s.url).lower() and s.chars > 1000:
                        ex = self.excerpts(s.id, query, max_chunks=2)
                        if ex and ex[0].get("score", 0) >= 2.0:
                            matching_cached.append((s, ex))
                if matching_cached:
                    matching_cached.sort(key=lambda x: -x[1][0]["score"])
                    return [
                        {
                            "source_id": s.id,
                            "title": s.title,
                            "url": s.url,
                            "tier": s.tier,
                            "chars": s.chars,
                            "snippet": ex[0]["text"][:350],
                        }
                        for s, ex in matching_cached[:3]
                    ]
                # If cached documents don't answer the query, proceed to web search!
                break

        kwargs: Dict[str, Any] = dict(
            query=query, search_depth=config.tavily_search_depth, max_results=max(1, min(max_results or 3, 5)),
            # the whole page as Markdown: register tables keep their rows and columns, and every quote can be checked
            include_raw_content="markdown", exclude_domains=EXCLUDED_STORE_DOMAINS,
        )
        if config.tavily_search_depth == "advanced":
            kwargs["chunks_per_source"] = 3   # the most relevant passages of each page as its snippet
        if '"' in query:
            kwargs["exact_match"] = True      # a quoted part number must appear as written
        if domains:
            cleaned = [d.lower().strip() for d in domains if not any(bad in d.lower() for bad in EXCLUDED_STORE_DOMAINS)]
            # If searching for AT32 / Artery, do not restrict to ST domain
            if ("at32" in low_q or "artery" in low_q) and "st.com" in cleaned:
                cleaned = [d for d in cleaned if d != "st.com"]
            if cleaned:   # the manufacturer's domains first, other sites still allowed (one call instead of a fallback)
                kwargs["include_domains"] = cleaned
                kwargs["include_domains_mode"] = "prefer"

        resp = self._tavily("search", **kwargs)
        out = []
        for r in resp.get("results", []):
            url = r.get("url", "")
            host = urllib.parse.urlparse(url).netloc.lower()
            if any(bad in host for bad in EXCLUDED_STORE_DOMAINS):
                continue
            text = r.get("raw_content") or r.get("content") or ""
            if not text.strip():
                continue
            # If text is raw HTML, extract clean text
            if "<html" in text.lower() or "<body" in text.lower():
                text = html_to_text(text)
            src = self.add(url, r.get("title", ""), text, kind="pdf" if url.lower().endswith(".pdf") else "web",
                           via="tavily:search", tavily_request=str(resp.get("request_id") or ""))
            out.append(
                {
                    "source_id": src.id,
                    "title": src.title,
                    "url": src.url,
                    "tier": src.tier,
                    "chars": src.chars,
                    "score": round(float(r.get("score") or 0), 3),
                    "snippet": (r.get("content") or "")[:350],
                }
            )
        return out

    def _tavily(self, op: str, **kwargs: Any) -> Dict[str, Any]:
        """One Tavily call (search / extract / map), with its credit usage requested. Every call is logged to
        sources/tavily_usage.jsonl (endpoint, request id, credits, query or URL), so each stored source and the facts
        quoted from it trace back to the Tavily request that found them."""
        from tavily import TavilyClient
        resp = getattr(TavilyClient(api_key=config.tavily_api_key), op)(include_usage=True, **kwargs)
        entry = {"t": round(time.time(), 1), "op": op, "request_id": resp.get("request_id", ""),
                 "credits": (resp.get("usage") or {}).get("credits"),
                 "query": kwargs.get("query") or "", "url": kwargs.get("url") or kwargs.get("urls") or ""}
        with self._lock, open(self.dir / "tavily_usage.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return resp

    def tavily_usage(self) -> Dict[str, Any]:
        """Tavily calls and credits spent for this project, per endpoint."""
        p = self.dir / "tavily_usage.jsonl"
        calls: Dict[str, int] = {}
        credits = 0.0
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                calls[e.get("op", "?")] = calls.get(e.get("op", "?"), 0) + 1
                credits += float(e.get("credits") or 0)
        return {"calls": calls, "credits": round(credits, 2)}

    def find_documents(self, url: str, part: str = "", limit: int = 40) -> Dict[str, Any]:
        """Tavily Map over a manufacturer's product or documentation page: the official documents it links to
        (datasheet, reference manual, register map, errata, application notes), PDFs and pages naming the part first."""
        what = f" for {part}" if part else ""
        resp = self._tavily("map", url=url, max_depth=2, limit=max(10, min(int(limit or 40), 100)), allow_external=False,
                            instructions=f"Find the official datasheet, reference manual, register map, application notes "
                                         f"and errata{what}.")
        part_l = (part or "").lower().replace(" ", "")
        doc_word = re.compile(r"datasheet|data-sheet|reference|manual|register|errata|app(lication)?[-_ ]?note|\.pdf", re.I)

        def rank(u: str) -> tuple:   # PDFs, then the part's own, then datasheets, then other documents
            low = u.lower()
            return (not low.split("?")[0].endswith(".pdf"), not (part_l and part_l in low.replace("-", "").replace("_", "")),
                    not re.search(r"datasheet|data-sheet|[-_]ds\d", low), not doc_word.search(low), len(u))
        urls = sorted({u for u in resp.get("results") or [] if isinstance(u, str)}, key=rank)
        docs = [{"url": u, "tier": classify_url(u).value, "pdf": u.lower().split("?")[0].endswith(".pdf")}
                for u in urls if doc_word.search(u) or (part_l and part_l in u.lower())]
        return {"base_url": resp.get("base_url", url), "documents": docs[:15], "pages_mapped": len(urls),
                "request_id": resp.get("request_id", "")}

    def fetch(self, url: str) -> Source:
        """Store the full text of a document. Web pages are read with Tavily Extract (advanced, Markdown: rendered pages,
        tables kept); PDFs are downloaded and converted locally (tables and equations kept), with Extract as the fallback."""
        text, kind, via, request = "", "web", "", ""
        pdf_data = b""
        err: Optional[str] = None
        is_pdf_url = url.lower().split("?")[0].endswith(".pdf")

        def extract() -> None:
            nonlocal text, via, request, err
            try:
                r = self._tavily("extract", urls=[url], extract_depth="advanced", format="markdown")
                items = r.get("results", [])
                if items and len((items[0].get("raw_content") or "").strip()) >= 200:
                    text, via, request, err = items[0]["raw_content"], "tavily:extract", str(r.get("request_id") or ""), None
                elif r.get("failed_results"):
                    err = f"tavily extract: {r['failed_results'][0].get('error', 'failed')}"
            except Exception as e:
                err = f"{err}; tavily extract: {e}" if err else f"tavily extract: {e}"

        if not is_pdf_url and config.has_tavily:
            extract()
        if len(text.strip()) < 200:
            try:
                with httpx.Client(follow_redirects=True, timeout=60.0, headers={"User-Agent": _UA}) as c:
                    with c.stream("GET", url) as resp:
                        resp.raise_for_status()
                        buf = bytearray()
                        for chunk in resp.iter_bytes():
                            buf.extend(chunk)
                            if len(buf) > MAX_DOWNLOAD_BYTES:
                                break
                        ctype = resp.headers.get("content-type", "").lower()
                data = bytes(buf)
                if "pdf" in ctype or is_pdf_url or data[:5] == b"%PDF-":
                    text, kind, pdf_data = pdf_to_text(data), "pdf", data
                else:
                    text = html_to_text(data.decode("utf-8", "ignore"))
                via = "download"
            except Exception as e:
                err = f"{err}; {e}" if err else str(e)
        if len(text.strip()) < 200 and is_pdf_url and config.has_tavily:
            extract()

        if len(text.strip()) < 200:
            raise RuntimeError(f"Could not retrieve readable content from {url}: {err or 'empty document'}")
        src = self.add(url, url.rsplit("/", 1)[-1] or url, text, kind=kind, via=via, tavily_request=request)
        if kind == "pdf" and via == "download" and pdf_data[:5] == b"%PDF-":
            self.keep_pdf(src.id, pdf_data)
        return src

    # ---------- the original document ----------
    def pdf_path(self, source_id: str) -> Path:
        return self.dir / f"{source_id}.pdf"

    def keep_pdf(self, source_id: str, data: bytes) -> None:
        """Keep a datasheet's original PDF next to its text: its figures and tables can be rendered and read later."""
        if data[:5] == b"%PDF-":
            self.pdf_path(source_id).write_bytes(data)
