"""search_datasheet: passages along the sections, embedded once (cached with the project), ranked by a query; the
embedding model is faked here (a bag of words), the NVIDIA call itself is not made."""

import math
import re

from embedforge.agent import retrieval
from embedforge.agent.events import RunSession
from embedforge.agent.tools import ToolBelt
from embedforge.agent.workspace import Project
from embedforge.truth import FactLedger, SourceStore
from test_outline import DS

VOCAB = ["normal", "sleep", "mode", "measurement", "time", "drdy", "fifo", "register", "supply", "voltage", "osr", "wait"]


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def __call__(self, texts, kind):
        self.calls.append((kind, len(texts)))
        out = []
        for t in texts:
            words = re.findall(r"[a-z]+", t.lower())
            out.append([float(words.count(w)) + 1e-3 for w in VOCAB])
        return out


def test_passages_follow_sections_and_keep_offsets():
    from embedforge.truth.outline import sections
    secs = sections(DS)
    ps = retrieval.passages(DS, secs)
    assert all(DS[p["start"]:p["end"]] == p["text"] for p in ps)
    assert all(len(p["text"]) <= retrieval.PASSAGE for p in ps)
    assert {p["section"].split(" ")[0] for p in ps} >= {"2.1.2", "2.2", "3.1"}
    assert not any("Table of contents" in p["text"] for p in ps)   # the contents page answers nothing


def test_search_ranks_the_passage_that_answers_and_caches_the_vectors(tmp_path):
    p = Project("t", tmp_path / "t")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/dm388.pdf", "DM388 datasheet", DS, kind="pdf")
    belt = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store))
    belt.embedder = fake = FakeEmbedder()
    r = belt.call("search_datasheet", {"source_id": src.id, "question": "measurement time wait", "k": 2})
    assert r["passages"][0]["section"].startswith("2.2 Measurement time"), r
    top = r["passages"][0]
    assert belt.reads.seen(src.id, top["start"], top["end"]) == 1.0   # what search showed counts as read
    passage_calls = [n for kind, n in fake.calls if kind == "passage"]
    belt.call("search_datasheet", {"source_id": src.id, "question": "data ready drdy"})
    assert [n for kind, n in fake.calls if kind == "passage"] == passage_calls   # embedded once, then cached
    assert (store.dir / "embeddings").exists()


def test_search_without_an_embedding_provider_points_to_reading(tmp_path, monkeypatch):
    from embedforge.core.config import config
    monkeypatch.setattr(config, "nvidia_api_key", None)
    p = Project("t", tmp_path / "t")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/dm388.pdf", "DM388 datasheet", DS, kind="pdf")
    r = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store)).call(
        "search_datasheet", {"source_id": src.id, "question": "normal mode"})
    assert "unavailable" in r["error"] and "read_section" in r["error"]


def test_unit_vectors():
    v = retrieval._unit([3.0, 4.0])
    assert math.isclose(v[0], 0.6) and math.isclose(v[1], 0.8)
