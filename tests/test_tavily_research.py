"""Tavily in the source store: search tuned for datasheets, Extract for web pages, Map for a vendor's official
documents, and the usage log every stored source traces back to. Offline: the Tavily client is faked."""

import httpx
import pytest
import tavily

from embedforge.core.config import config
from embedforge.truth import SourceStore

PAGE = "BMP388 digital pressure sensor, register map and compensation. " * 10


class FakeTavily:
    calls = []

    def __init__(self, api_key=None):
        pass

    def search(self, **kw):
        FakeTavily.calls.append(("search", kw))
        return {"request_id": "req-s1", "usage": {"credits": 2}, "results": [
            {"url": "https://www.bosch-sensortec.com/bmp388", "title": "BMP388", "content": "snippet", "raw_content": PAGE, "score": 0.91},
            {"url": "https://www.lcsc.com/product/bmp388", "title": "shop", "content": "x", "raw_content": PAGE, "score": 0.5}]}

    def extract(self, **kw):
        FakeTavily.calls.append(("extract", kw))
        return {"request_id": "req-e1", "usage": {"credits": 0}, "failed_results": [],
                "results": [{"url": kw["urls"][0], "raw_content": "| Register | Name |\n|---|---|\n" + PAGE}]}

    def map(self, **kw):
        FakeTavily.calls.append(("map", kw))
        return {"base_url": kw["url"], "request_id": "req-m1", "usage": {"credits": 2}, "results": [
            "https://www.bosch-sensortec.com/products/environmental-sensors/pressure-sensors/bmp388/",
            "https://www.bosch-sensortec.com/media/boschsensortec/downloads/bmp388-errata.pdf",
            "https://www.bosch-sensortec.com/media/boschsensortec/downloads/bst-bmp388-ds001.pdf",
            "https://www.bosch-sensortec.com/about-us/our-company/"]}


@pytest.fixture
def store(tmp_path, monkeypatch):
    FakeTavily.calls = []
    monkeypatch.setattr(tavily, "TavilyClient", FakeTavily)
    return SourceStore(tmp_path)


def test_search_is_tuned_for_datasheets_and_logged(store):
    res = store.search('"BMP388" register map', domains=["bosch-sensortec.com"])
    assert len(FakeTavily.calls) == 1   # manufacturer domains preferred, not a restricted search plus a fallback search
    op, kw = FakeTavily.calls[0]
    assert op == "search" and kw["include_domains"] == ["bosch-sensortec.com"] and kw["include_domains_mode"] == "prefer"
    assert kw["include_raw_content"] == "markdown" and kw["exact_match"] is True and kw["include_usage"] is True
    assert kw["search_depth"] == config.tavily_search_depth
    assert [r["url"] for r in res] == ["https://www.bosch-sensortec.com/bmp388"] and res[0]["score"] == 0.91   # shop dropped
    src = store.get(res[0]["source_id"])
    assert (src.via, src.tavily_request, src.tier) == ("tavily:search", "req-s1", "OFFICIAL_MANUFACTURER")
    assert store.tavily_usage() == {"calls": {"search": 1}, "credits": 2.0}


def test_web_pages_are_read_with_extract_and_pdfs_downloaded_first(store, monkeypatch):
    page = store.fetch("https://www.bosch-sensortec.com/products/bmp388/")
    op, kw = FakeTavily.calls[-1]
    assert op == "extract" and kw["extract_depth"] == "advanced" and kw["format"] == "markdown"
    assert page.via == "tavily:extract" and page.tavily_request == "req-e1" and page.text.startswith("| Register |")

    class Offline:
        def __init__(self, *a, **k):
            raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "Client", Offline)
    n = len(FakeTavily.calls)
    pdf = store.fetch("https://www.bosch-sensortec.com/media/bst-bmp388-ds001.pdf")
    # a PDF is downloaded and converted locally first; Extract only when that yields no text
    assert pdf.via == "tavily:extract" and [c[0] for c in FakeTavily.calls[n:]] == ["extract"]


def test_find_documents_maps_the_vendor_site_and_puts_the_datasheet_first(store):
    res = store.find_documents("https://www.bosch-sensortec.com/products/environmental-sensors/pressure-sensors/bmp388/", "BMP388")
    op, kw = FakeTavily.calls[-1]
    assert op == "map" and "datasheet" in kw["instructions"] and "BMP388" in kw["instructions"] and kw["allow_external"] is False
    urls = [d["url"] for d in res["documents"]]
    assert urls[0].endswith("bst-bmp388-ds001.pdf") and urls[1].endswith("bmp388-errata.pdf")
    assert not any("about-us" in u for u in urls)
    assert res["documents"][0]["tier"] == "OFFICIAL_MANUFACTURER" and res["request_id"] == "req-m1" and res["pages_mapped"] == 4
