"""Manufacturer reference code: a part's official driver sources from the vendor's own GitHub organisation.

Datasheet PDFs often lose formulas in text extraction (fraction bars, superscripts), while the vendor's reference driver
states the same conversions as plain C (`temp_var = 0.00390625f; par_t1 = reg_par_t1 / temp_var;`). Only repositories
of organisations in trust.OFFICIAL_GITHUB_ORGS are searched, so what is stored is classified OFFICIAL_REPO.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

import httpx

from embedforge.truth.source_store import Source, SourceStore

API = "https://api.github.com"
# vendor organisations that publish device drivers (framework orgs are excluded: they are not the part's maker)
VENDOR_ORGS = ["boschsensortec", "STMicroelectronics", "tdk-invensense", "Sensirion", "Infineon", "analogdevicesinc",
               "nxp-mcuxpresso", "renesas", "NordicSemiconductor", "espressif", "microchip-pic-avr-solutions", "raspberrypi"]
_VENDOR_HINT = {"bosch": "boschsensortec", "bmp": "boschsensortec", "bme": "boschsensortec", "bmi": "boschsensortec",
                "bma": "boschsensortec", "bmm": "boschsensortec", "bhi": "boschsensortec", "st": "STMicroelectronics",
                "lsm": "STMicroelectronics", "lis": "STMicroelectronics", "lps": "STMicroelectronics", "vl53": "STMicroelectronics",
                "icm": "tdk-invensense", "mpu": "tdk-invensense", "sht": "Sensirion", "scd": "Sensirion", "sgp": "Sensirion",
                "sps": "Sensirion", "dps": "Infineon", "tle": "Infineon", "adxl": "analogdevicesinc", "ad7": "analogdevicesinc"}
MAX_FILE_BYTES = 400_000


def _terms(part: str) -> List[str]:
    """BMP388 -> BMP388, BMP38, BMP3 (vendors name repositories after the family)."""
    p = re.sub(r"[^A-Za-z0-9]", "", part or "")
    out = []
    while len(p) >= 4 or (len(p) >= 3 and not out):
        out.append(p)
        if not p[-1].isdigit():
            break
        p = p[:-1]
    return out[:4]


def _orgs_for(part: str, vendor: str) -> List[str]:
    v = (vendor or "").strip().lower()
    hinted = [o for k, o in _VENDOR_HINT.items() if v.startswith(k) or (part or "").lower().startswith(k)]
    rest = [o for o in VENDOR_ORGS if o not in hinted]
    return list(dict.fromkeys(hinted + rest))


def find_reference_code(store: SourceStore, part: str, vendor: str = "", get: Optional[Callable[..., Any]] = None,
                        max_files: int = 3) -> Dict[str, Any]:
    """Search the vendors' official GitHub organisations for a repository named after the part (or its family), store
    its main C sources and return them as sources to read_source."""
    get = get or (lambda url, **kw: httpx.get(url, timeout=25, follow_redirects=True,
                                              headers={"Accept": "application/vnd.github+json", "User-Agent": "EmbedForge"}, **kw))
    terms = _terms(part)
    if not terms:
        return {"found": False, "error": "give the part number, e.g. 'BMP388'"}
    orgs = _orgs_for(part, vendor)
    repos: List[Dict[str, Any]] = []
    for term in terms:
        for chunk in (orgs[:6], orgs[6:]):
            if not chunk:
                continue
            q = f"{term} in:name " + " ".join(f"org:{o}" for o in chunk)
            try:
                r = get(f"{API}/search/repositories", params={"q": q, "per_page": 5})
            except httpx.HTTPError as e:
                return {"found": False, "error": f"GitHub search failed: {e}"}
            if r.status_code != 200:
                return {"found": False, "error": f"GitHub search failed ({r.status_code}): {r.text[:160]}"}
            repos += [it for it in r.json().get("items", []) if it.get("owner", {}).get("login", "").lower() in {o.lower() for o in orgs}]
        if repos:
            break
    if not repos:
        return {"found": False, "note": f"No repository named after {part} in the vendors' official GitHub organisations "
                                        f"({', '.join(orgs[:6])}, ...). Use the datasheet, or web_search the vendor's site."}
    stored: List[Source] = []
    for repo in repos[:2]:
        full, branch = repo["full_name"], repo.get("default_branch") or "master"
        files: List[Dict[str, Any]] = []
        for sub in ("", "src", "driver", "drivers"):
            try:
                r = get(f"{API}/repos/{full}/contents/{sub}".rstrip("/"))
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and isinstance(r.json(), list):
                files += [f for f in r.json() if f.get("type") == "file" and re.search(r"\.(c|h|cpp|hpp)$", f.get("name", ""), re.I)]
            if files:
                break
        fam = terms[-1].lower()
        files.sort(key=lambda f: (not f["name"].lower().startswith(fam), not f["name"].lower().endswith(".c"), -int(f.get("size") or 0)))
        for f in files[:max_files]:
            if int(f.get("size") or 0) > MAX_FILE_BYTES or not f.get("download_url"):
                continue
            try:
                raw = get(f["download_url"])
            except httpx.HTTPError:
                continue
            if raw.status_code == 200:
                url = f.get("html_url") or f"https://github.com/{full}/blob/{branch}/{f['path']}"
                stored.append(store.add(url, f"{full}/{f['path']} (manufacturer reference code)", raw.text, kind="code"))
        if len(stored) >= max_files:
            break
    if not stored:
        return {"found": False, "note": f"Found {repos[0]['full_name']} but could not download its sources."}
    return {"found": True, "repository": repos[0]["html_url"],
            "sources": [{"source_id": s.id, "url": s.url, "tier": s.tier, "chars": s.chars} for s in stored],
            "next": "read_source(source_id, '<what you need>', e.g. 'par_t1 temp_var' or 'compensate') for the conversions and "
                    "register handling; quote it in record_formula / record_registers like the datasheet."}
