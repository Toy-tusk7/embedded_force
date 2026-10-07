"""Board connectivity model shared by every EDA importer (EasyEDA Standard/Pro, Protel, PADS, KiCad, BOM).

A Board is parts -> pins (number, name, net) plus nets -> nodes. Importers produce partial boards
(a netlist has connectivity but no pin names; a BOM has part numbers but no nets) and `merge`
combines them. `report()` renders a plain-text connectivity listing that is stored as a project
source, so the agent can quote wiring verbatim into the Fact Ledger like any datasheet line.
"""

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

MCU_PREFIXES = (
    "STM32", "AT32", "GD32", "APM32", "CH32", "HK32", "MM32", "N32", "ESP32", "ESP8266", "RP20", "RP23",
    "NRF51", "NRF52", "NRF53", "ATMEGA", "ATTINY", "ATSAM", "SAMD", "PIC", "DSPIC", "MSP430", "EFM32", "EFR32",
    "LPC", "MK", "MKL", "S32", "TM4C", "CC26", "CC13", "R7F", "RA", "PY32", "CW32", "HC32", "BL6", "W806",
)


# Tool-generated names for unlabelled nets (EasyEDA, Altium, KiCad, ours) — never a conflict worth reporting.
_AUTO_NET = re.compile(r"^(NET_|Net-|Net\$|\$|N\$|NetU|Net[A-Z]+\d|unconnected-|\$\$)", re.I)


def natural_key(s: str) -> List[Any]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


@dataclass
class Pin:
    number: str
    name: str = ""
    net: str = ""


@dataclass
class Part:
    ref: str
    value: str = ""
    footprint: str = ""
    mpn: str = ""
    manufacturer: str = ""
    lcsc: str = ""
    pins: Dict[str, Pin] = field(default_factory=dict)

    def pin(self, number: str) -> Pin:
        number = str(number).strip()
        if number not in self.pins:
            self.pins[number] = Pin(number)
        return self.pins[number]

    @property
    def label(self) -> str:
        return self.mpn or self.value

    def is_mcu(self) -> bool:
        txt = re.sub(r"[^A-Z0-9]", "", f"{self.mpn} {self.value}".upper())
        return any(txt.startswith(p) for p in MCU_PREFIXES) and len(self.pins) >= 8


@dataclass
class Board:
    name: str = "board"
    parts: Dict[str, Part] = field(default_factory=dict)
    imported: List[Dict[str, str]] = field(default_factory=list)   # [{file, format}]
    warnings: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ building
    def part(self, ref: str) -> Part:
        ref = str(ref).strip()
        if ref not in self.parts:
            self.parts[ref] = Part(ref)
        return self.parts[ref]

    def connect(self, net: str, ref: str, pin: str, pin_name: str = "") -> None:
        p = self.part(ref).pin(pin)
        p.net = net
        if pin_name and not p.name:
            p.name = pin_name

    def nets(self) -> Dict[str, List[Tuple[str, str]]]:
        out: Dict[str, List[Tuple[str, str]]] = {}
        for part in self.parts.values():
            for pin in part.pins.values():
                if pin.net:
                    out.setdefault(pin.net, []).append((part.ref, pin.number))
        for nodes in out.values():
            nodes.sort(key=lambda n: (natural_key(n[0]), natural_key(n[1])))
        return dict(sorted(out.items(), key=lambda kv: natural_key(kv[0])))

    def merge(self, other: "Board") -> None:
        """Combine another partial board into this one; existing connectivity wins, gaps are filled."""
        for ref, op in other.parts.items():
            mine = self.part(ref)
            for attr in ("value", "footprint", "mpn", "manufacturer", "lcsc"):
                if getattr(op, attr) and not getattr(mine, attr):
                    setattr(mine, attr, getattr(op, attr))
            for num, opin in op.pins.items():
                p = mine.pin(num)
                if opin.name and not p.name:
                    p.name = opin.name
                if opin.net:
                    if not p.net or (_AUTO_NET.match(p.net) and not _AUTO_NET.match(opin.net)):
                        p.net = opin.net
                    elif p.net != opin.net and not _AUTO_NET.match(opin.net):
                        src = other.imported[-1]["file"] if other.imported else "another file"
                        self.warnings.append(f"{ref} pin {num}: net '{p.net}' here but '{opin.net}' in {src}")
        self.imported += other.imported
        self.warnings += other.warnings

    # ------------------------------------------------------------------ queries
    def mcus(self) -> List[Part]:
        found = [p for p in self.parts.values() if p.is_mcu()]
        if not found:
            ics = [p for p in self.parts.values() if p.ref.upper().startswith(("U", "IC")) and len(p.pins) >= 16]
            found = sorted(ics, key=lambda p: -len(p.pins))[:1]
        return sorted(found, key=lambda p: natural_key(p.ref))

    def node_label(self, ref: str, number: str) -> str:
        part = self.parts.get(ref)
        name = part.pins[number].name if part and number in part.pins else ""
        return f"{ref}.{number}" + (f" ({name})" if name and name != number else "")

    def pin_line(self, part: Part, pin: Pin, nets: Dict[str, List[Tuple[str, str]]]) -> str:
        head = f"{part.ref} pin {pin.number}" + (f" ({pin.name})" if pin.name and pin.name != pin.number else "")
        if not pin.net:
            return f"{head} -> not connected"
        others = [self.node_label(r, n) for r, n in nets.get(pin.net, []) if not (r == part.ref and n == pin.number)]
        return f"{head} -> net {pin.net}" + (f" -> {', '.join(others)}" if others else "")

    _STOP = {"ALL", "AND", "THE", "FOR", "WITH", "LIST", "PIN", "PINS", "NET", "NETS", "PART", "PARTS", "SHOW", "GET",
             "CONNECTED", "CONNECTIONS", "NUMBER", "NUMBERS", "NAME", "NAMES", "WIRING", "OF", "TO", "ON", "IN", "MCU"}

    def query(self, q: str, limit: int = 120) -> Dict[str, Any]:
        """Look up designators, nets, pin names or part numbers. Sentence-style queries ('U3 BMP388 pins') are split
        into identifier-like terms and the matches merged."""
        res = self._query_one(q, limit)
        if "error" not in res or not q.strip():
            return res
        terms = [t for t in re.findall(r"[A-Za-z0-9_+./\-]+", q)
                 if t.upper() not in self._STOP and (re.search(r"\d|_", t) or (t.isupper() and len(t) >= 3))]
        lines: List[str] = []
        parts: List[Dict[str, Any]] = []
        matched: List[str] = []
        for t in dict.fromkeys(terms):
            r = self._query_one(t, limit)
            if "error" in r:
                continue
            matched.append(t)
            lines += [ln for ln in r.get("lines", []) if ln not in lines]
            parts += ([r["part"]] if "part" in r else []) + r.get("parts", [])
        if not lines:
            return res
        return {"matched_terms": matched, "lines": lines[:limit], **({"parts": parts[:10]} if parts else {})}

    @staticmethod
    def _clip(line: str, width: int = 170) -> str:
        """Shorten very long lines (power nets) at a node boundary; the kept prefix is still verbatim source text."""
        if len(line) <= width:
            return line
        cut = line.rfind(", ", 0, width)
        return line[: cut if cut > 0 else width] + " …"

    def _part_lines(self, part: Part, nets: Dict[str, List[Tuple[str, str]]], limit: int) -> List[str]:
        connected = [self._clip(self.pin_line(part, pin, nets)) for pin in self._sorted_pins(part) if pin.net]
        loose = [f"{pin.number}" + (f" ({pin.name})" if pin.name and pin.name != pin.number else "")
                 for pin in self._sorted_pins(part) if not pin.net]
        out = connected[:limit]
        if loose:
            out.append(f"{part.ref} pins not connected: " + ", ".join(loose))
        return out

    def _query_one(self, q: str, limit: int = 120) -> Dict[str, Any]:
        nets = self.nets()
        qs = q.strip()
        qu = qs.upper()
        if not qs:
            return {"summary": self.summary()}
        if qs in self.parts or qu in {r.upper() for r in self.parts}:
            part = next(p for r, p in self.parts.items() if r.upper() == qu)
            return {"part": self._part_dict(part), "lines": self._part_lines(part, nets, limit)}
        net = next((n for n in nets if n.upper() == qu), None)
        if net:
            return {"net": net, "lines": [f"net {net}: " + ", ".join(self.node_label(r, n) for r, n in nets[net])]}
        lines = []
        for part in self.parts.values():
            for pin in self._sorted_pins(part):
                if pin.name and pin.name.upper() == qu:
                    lines.append(self.pin_line(part, pin, nets))
        if lines:
            return {"pin_name": qs, "lines": lines[:limit]}
        hits = [p for p in self.parts.values() if qu and (qu in p.value.upper() or qu in p.mpn.upper() or qu in p.footprint.upper())]
        if hits:
            out = []
            for part in hits[:3]:
                out += self._part_lines(part, nets, limit)
            return {"parts": [self._part_dict(p) for p in hits[:10]], "lines": out[:limit]}
        fuzzy = [n for n in nets if qu in n.upper()]
        if not fuzzy and len(qu) >= 4:  # 'MOTOR' -> MOTO0..3, 'LEDS' -> LED1: match on a shared stem
            stem = qu[:4]
            fuzzy = [n for n in nets if stem in n.upper()]
        if fuzzy:
            return {"nets": fuzzy[:20], "lines": [f"net {n}: " + ", ".join(self.node_label(r, x) for r, x in nets[n]) for n in fuzzy[:20]]}
        return {"error": f"Nothing on the board matches '{qs}'. Try a designator (U1), net name, pin name (PA8) or part number."}

    # ------------------------------------------------------------------ rendering
    @staticmethod
    def _sorted_pins(part: Part) -> List[Pin]:
        return sorted(part.pins.values(), key=lambda p: natural_key(p.number))

    @staticmethod
    def _part_dict(p: Part) -> Dict[str, Any]:
        return {"ref": p.ref, "value": p.value, "footprint": p.footprint, "mpn": p.mpn, "manufacturer": p.manufacturer,
                "lcsc": p.lcsc, "pin_count": len(p.pins), "is_mcu": p.is_mcu()}

    def summary(self) -> Dict[str, Any]:
        nets = self.nets()
        return {
            "name": self.name, "parts": len(self.parts), "nets": len(nets),
            "mcus": [self._part_dict(p) for p in self.mcus()],
            "ics": [self._part_dict(p) for p in sorted(self.parts.values(), key=lambda p: natural_key(p.ref))
                    if p.ref.upper().startswith(("U", "IC", "Q", "J", "P", "CN"))][:40],
            "imported": self.imported, "warnings": self.warnings[:20],
            "pin_names_known": any(pin.name for p in self.parts.values() for pin in p.pins.values()),
        }

    def report(self) -> str:
        """Plain-text connectivity listing; every line is a quotable fact about the user's wiring."""
        nets = self.nets()
        lines = [f"Board connectivity: {self.name}",
                 "Imported from: " + ", ".join(f"{i['file']} ({i['format']})" for i in self.imported),
                 f"Parts: {len(self.parts)}  Nets: {len(nets)}", ""]
        mcu_refs = {p.ref for p in self.mcus()}
        ordered = sorted(self.parts.values(), key=lambda p: (p.ref not in mcu_refs, natural_key(p.ref)))
        for part in ordered:
            desc = " ".join(x for x in (part.label, f"[{part.footprint}]" if part.footprint else "", "(MCU)" if part.ref in mcu_refs else "") if x)
            lines.append(f"== {part.ref} {desc}".rstrip())
            if part.manufacturer or part.lcsc:
                lines.append(f"{part.ref} manufacturer: {part.manufacturer or '-'}  supplier part: {part.lcsc or '-'}")
            lines += [self.pin_line(part, pin, nets) for pin in self._sorted_pins(part)]
            lines.append("")
        lines.append("== Nets")
        lines += [f"net {n}: " + ", ".join(self.node_label(r, x) for r, x in nodes) for n, nodes in nets.items()]
        if self.warnings:
            lines += ["", "== Import warnings"] + self.warnings[:50]
        return "\n".join(lines) + "\n"

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "imported": self.imported, "warnings": self.warnings,
                "parts": {r: {**self._part_dict(p), "pins": [asdict(x) for x in self._sorted_pins(p)]}
                          for r, p in sorted(self.parts.items(), key=lambda kv: natural_key(kv[0]))},
                "nets": {n: [list(x) for x in v] for n, v in self.nets().items()}}

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Board":
        b = cls(name=d.get("name", "board"), imported=d.get("imported", []), warnings=d.get("warnings", []))
        for ref, pd in (d.get("parts") or {}).items():
            part = b.part(ref)
            for attr in ("value", "footprint", "mpn", "manufacturer", "lcsc"):
                setattr(part, attr, pd.get(attr, "") or "")
            for pin in pd.get("pins", []):
                part.pins[str(pin["number"])] = Pin(str(pin["number"]), pin.get("name", ""), pin.get("net", ""))
        return b

