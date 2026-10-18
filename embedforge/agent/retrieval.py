"""Semantic search over a stored datasheet with an NVIDIA Nemotron embedding model (nvidia/nemotron-3-embed-1b on
NVIDIA's API by default). A 100-page datasheet is split into passages along its sections, embedded once and cached
with the project; a question then returns the passages that answer it, with their section and offset, so the agent
reads those instead of whole chapters. Required reading (verify's DATASHEET_UNREAD) still applies: search finds, the
sections the code depends on must still be read."""

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from embedforge.core.config import config
from embedforge.core.files import load_json, write_atomic

PASSAGE = 1500   # characters per passage: about a paragraph and a table, under the embedding model's input limit
BATCH = 32       # passages per embeddings request

# (texts, 'passage' | 'query') -> one vector per text
Embedder = Callable[[Sequence[str], str], List[List[float]]]


def passages(text: str, secs: List[Any]) -> List[Dict[str, Any]]:
    """The text in passages of up to PASSAGE characters, cut at line ends inside each section, with offsets."""
    bounds = [(s.start, s.end, getattr(s, "label", "")) for s in secs] or [(0, len(text), "")]
    if bounds[0][0] > 0:
        bounds.insert(0, (0, bounds[0][0], ""))
    out: List[Dict[str, Any]] = []
    for a, b, label in bounds:
        i = a
        while i < b:
            j = min(b, i + PASSAGE)
            if j < b:   # end at a line break when one is near
                nl = text.rfind("\n", i + PASSAGE // 2, j)
                j = nl + 1 if nl > i else j
            chunk = text[i:j]
            # a table of contents ('3.2. Power management ......... 11') matches every question and answers none
            if chunk.strip() and len(re.findall(r"\.{6,}", chunk)) < 3:
                out.append({"start": i, "end": j, "section": label, "text": chunk})
            i = j
    return out


def default_embedder() -> Embedder:
    """Embeddings from the configured provider's OpenAI-compatible /embeddings endpoint. NVIDIA's retrieval models are
    asymmetric: passages and queries are embedded with input_type 'passage' / 'query'."""
    import httpx
    from embedforge.agent.llm import LLMError, providers
    p = providers().get(config.embed_provider)
    if p is None or not p.configured:
        raise LLMError(f"No {config.embed_provider} key configured for datasheet search (EMBEDFORGE_EMBED_PROVIDER).")

    def embed(texts: Sequence[str], kind: str) -> List[List[float]]:
        payload = {"model": config.embed_model, "input": list(texts), "input_type": kind, "encoding_format": "float",
                   "truncate": "END"}
        with httpx.Client(timeout=120.0) as c:
            r = c.post(f"{p.base_url}/embeddings", headers={"Authorization": f"Bearer {p.api_key}"}, json=payload)
        if r.status_code >= 400:
            raise LLMError(f"embeddings failed ({config.embed_model}): HTTP {r.status_code}: {r.text[:300]}",
                           transient=r.status_code in (429, 500, 502, 503, 504))
        data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]
    return embed


def _unit(v: Sequence[float]) -> List[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class DatasheetIndex:
    """Passage vectors of one source, cached in sources/embeddings/<source>-<model>.json and rebuilt when the text or
    the model changes."""

    def __init__(self, sources_dir: Path, source_id: str, text: str, secs: List[Any], embedder: Embedder):
        slug = re.sub(r"\W+", "-", config.embed_model.lower()).strip("-")
        self.path = Path(sources_dir) / "embeddings" / f"{source_id}-{slug}.json"
        self.text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        self.embedder = embedder
        cached = load_json(self.path, {})
        if isinstance(cached, dict) and cached.get("text_hash") == self.text_hash and cached.get("passages"):
            self.passages = cached["passages"]
            return
        self.passages = passages(text, secs)
        for k in range(0, len(self.passages), BATCH):
            group = self.passages[k:k + BATCH]
            for p, v in zip(group, embedder([p["text"] for p in group], "passage")):
                p["vector"] = _unit(v)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self.path, json.dumps({"model": config.embed_model, "text_hash": self.text_hash,
                                            "passages": self.passages}))

    def search(self, question: str, k: int = 5) -> List[Dict[str, Any]]:
        q = _unit(self.embedder([question], "query")[0])
        scored = sorted(((sum(a * b for a, b in zip(q, p["vector"])), p) for p in self.passages),
                        key=lambda sp: -sp[0])
        return [{"score": round(s, 3), **{x: p[x] for x in ("section", "start", "end", "text")}} for s, p in scored[:k]]


def search(sources_dir: Path, source_id: str, text: str, secs: List[Any], question: str, k: int = 5,
           embedder: Optional[Embedder] = None) -> List[Dict[str, Any]]:
    return DatasheetIndex(sources_dir, source_id, text, secs, embedder or default_embedder()).search(question, k)
