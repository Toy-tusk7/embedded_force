"""Firmware file formats: read the program image out of an ELF or UF2, and write Intel HEX.

Pure Python, so every build can ship a .hex even when the target's own build only makes .uf2/.elf (Betaflight on
RP2350) and no objcopy is installed on the host. The image is the ELF's loadable segments at their load (flash)
addresses — exactly what objcopy -O ihex and the UF2 converter put on the chip.
"""

import struct
from pathlib import Path
from typing import List, Optional, Tuple

Segment = Tuple[int, bytes]  # (load address, bytes)


def elf_segments(data: bytes) -> Tuple[List[Segment], int]:
    """Loadable segments (PT_LOAD with file bytes, at their physical/load address) and the entry point."""
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise ValueError("not a 32-bit little-endian ELF file")
    entry, phoff = struct.unpack_from("<II", data, 24)
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    segs: List[Segment] = []
    for i in range(phnum):
        p_type, p_off, _vaddr, p_paddr, p_filesz = struct.unpack_from("<5I", data, phoff + i * phentsize)
        if p_type == 1 and p_filesz:
            segs.append((p_paddr, data[p_off:p_off + p_filesz]))
    if not segs:
        raise ValueError("ELF has no loadable segments")
    return sorted(segs), entry


def uf2_segments(data: bytes) -> Tuple[List[Segment], Optional[int]]:
    """Payload blocks of a UF2 file (merged where contiguous) and its family id."""
    if len(data) % 512:
        raise ValueError("UF2 size is not a multiple of 512")
    blocks, family = [], None
    for i in range(len(data) // 512):
        b = data[i * 512:(i + 1) * 512]
        m0, m1, flags, addr, size, _bno, _n, fam = struct.unpack_from("<8I", b, 0)
        if (m0, m1, struct.unpack_from("<I", b, 508)[0]) != (0x0A324655, 0x9E5D5157, 0x0AB16F30):
            raise ValueError(f"UF2 block {i} has bad magic numbers")
        if flags & 0x1:  # 'not main flash' blocks carry no program bytes
            continue
        if flags & 0x2000:
            family = fam
        blocks.append((addr, b[32:32 + size]))
    blocks.sort()
    merged: List[Segment] = []
    for addr, payload in blocks:
        if merged and merged[-1][0] + len(merged[-1][1]) == addr:
            merged[-1] = (merged[-1][0], merged[-1][1] + payload)
        else:
            merged.append((addr, payload))
    return merged, family


def to_ihex(segments: List[Segment], entry: Optional[int] = None, record: int = 16) -> str:
    """Intel HEX with extended linear address (type 04) records, optional start address (type 05), EOF."""
    def rec(rtype: int, addr16: int, payload: bytes) -> str:
        body = bytes([len(payload), (addr16 >> 8) & 0xFF, addr16 & 0xFF, rtype]) + payload
        return ":" + body.hex().upper() + f"{(-sum(body)) & 0xFF:02X}"

    lines, upper = [], None
    for base, data in segments:
        off = 0
        while off < len(data):
            addr = base + off
            if addr >> 16 != upper:
                upper = addr >> 16
                lines.append(rec(0x04, 0, upper.to_bytes(2, "big")))
            n = min(record, len(data) - off, 0x10000 - (addr & 0xFFFF))  # a record never crosses a 64 KB boundary
            lines.append(rec(0x00, addr & 0xFFFF, data[off:off + n]))
            off += n
    if entry is not None:
        lines.append(rec(0x05, 0, entry.to_bytes(4, "big")))
    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"


def parse_ihex(text: str) -> Tuple[List[Segment], Optional[int]]:
    """Decode Intel HEX back into merged segments (used to check a .hex against its .elf/.uf2)."""
    upper, entry, chunks = 0, None, []
    for n, line in enumerate(text.split(), start=1):
        if not line.startswith(":"):
            raise ValueError(f"line {n}: not an Intel HEX record")
        raw = bytes.fromhex(line[1:])
        if sum(raw) & 0xFF:
            raise ValueError(f"line {n}: checksum error")
        length, addr, rtype, payload = raw[0], (raw[1] << 8) | raw[2], raw[3], raw[4:4 + raw[0]]
        if rtype == 0x00:
            chunks.append(((upper << 16) + addr, payload))
        elif rtype == 0x04:
            upper = int.from_bytes(payload, "big")
        elif rtype == 0x05:
            entry = int.from_bytes(payload, "big")
        elif rtype == 0x01:
            break
    chunks.sort()
    merged: List[Segment] = []
    for addr, payload in chunks:
        if merged and merged[-1][0] + len(merged[-1][1]) == addr:
            merged[-1] = (merged[-1][0], merged[-1][1] + payload)
        else:
            merged.append((addr, payload))
    return merged, entry


def elf_to_hex(elf_path: Path, hex_path: Path) -> bool:
    try:
        segs, entry = elf_segments(Path(elf_path).read_bytes())
        Path(hex_path).write_text(to_ihex(segs, entry), encoding="ascii", newline="\n")
        return True
    except (OSError, ValueError, struct.error):
        return False
