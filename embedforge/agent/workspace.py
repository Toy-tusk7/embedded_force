"""Project workspace: real files on disk, sandboxed paths, versioned build outputs."""

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Any

from embedforge.core.config import config

TEXT_EXTS = {
    ".c", ".h", ".cpp", ".hpp", ".cc", ".ino", ".ini", ".md", ".txt", ".json",
    ".py", ".s", ".ld", ".cmake", ".mmd", ".csv", ".yaml", ".yml", ".toml",
    ".sch", ".kicad_sch", ".kicad_pcb", ".brd", ".net",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".bmp", ".gif", ".ico"}
DOC_EXTS = {".pdf"}
ARCHIVE_EXTS = {".zip"}
ALLOWED_EXTS = TEXT_EXTS | IMAGE_EXTS | DOC_EXTS | ARCHIVE_EXTS
HIDDEN_DIRS = {".pio", "sources", "builds", "__pycache__", ".git", ".vscode"}
MAX_FILE_BYTES = 400_000
MAX_UPLOAD_BYTES = 50_000_000


def workspace_root() -> Path:
    p = Path(config.workspace_root) / "workspace" / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


class Project:
    def __init__(self, project_id: str, root: Path):
        self.id = project_id
        self.root = root
        for d in ("src", "include", "lib", "sources", "builds", "artifacts", "docs", "schematics"):
            (root / d).mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ meta
    @property
    def meta_path(self) -> Path:
        return self.root / "project.json"

    def meta(self) -> Dict[str, Any]:
        if self.meta_path.exists():
            try:
                return json.loads(self.meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"id": self.id, "name": self.id, "created": time.time()}

    def update_meta(self, **kw) -> Dict[str, Any]:
        m = self.meta()
        m.update(kw)
        self.meta_path.write_text(json.dumps(m, indent=2), encoding="utf-8")
        return m

    # ------------------------------------------------------------ files
    def safe_path(self, rel: str, allow_binary: bool = True) -> Path:
        rel = (rel or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            raise ValueError(f"Illegal path: {rel!r}")
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError(f"Path escapes project: {rel!r}")
        top = Path(rel).parts[0]
        if top in HIDDEN_DIRS:
            raise ValueError(f"'{top}/' is managed by EmbedForge and cannot be edited directly.")
        valid_exts = ALLOWED_EXTS if allow_binary else TEXT_EXTS
        if p.suffix.lower() not in valid_exts and p.name not in ("platformio.ini",):
            raise ValueError(f"Unsupported file type: {p.suffix}")
        return p

    def write_file(self, rel: str, content: str) -> Dict[str, Any]:
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("File too large.")
        p = self.safe_path(rel, allow_binary=False)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content, encoding="utf-8")
        return {"path": rel.replace("\\", "/"), "bytes": len(content.encode("utf-8")), "created": not existed}

    def write_bytes(self, rel: str, data: bytes) -> Dict[str, Any]:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(f"File exceeds maximum upload limit ({MAX_UPLOAD_BYTES // 1_000_000} MB).")
        p = self.safe_path(rel, allow_binary=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_bytes(data)
        return {"path": rel.replace("\\", "/"), "bytes": len(data), "created": not existed}

    def read_file(self, rel: str) -> str:
        p = self.safe_path(rel, allow_binary=False)
        if not p.is_file():
            raise FileNotFoundError(f"No such file: {rel}")
        return p.read_text(encoding="utf-8", errors="replace")

    def read_bytes(self, rel: str) -> bytes:
        p = self.safe_path(rel, allow_binary=True)
        if not p.is_file():
            raise FileNotFoundError(f"No such file: {rel}")
        return p.read_bytes()

    def list_files(self) -> List[Dict[str, Any]]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root)
            if rel.parts[0] in HIDDEN_DIRS or rel.name in ("project.json", "conversation.json", "trace.jsonl", "ledger.json"):
                continue
            suffix = p.suffix.lower()
            kind = "image" if suffix in IMAGE_EXTS else "pdf" if suffix in DOC_EXTS else "schematic" if suffix in {".sch", ".kicad_sch", ".kicad_pcb", ".brd"} else "text"
            out.append({"path": str(rel).replace("\\", "/"), "bytes": p.stat().st_size, "kind": kind})
        return out

    # ------------------------------------------------------------ conversation
    def load_conversation(self) -> List[Dict[str, Any]]:
        p = self.root / "conversation.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def save_conversation(self, messages: List[Dict[str, Any]]) -> None:
        (self.root / "conversation.json").write_text(json.dumps(messages, indent=1), encoding="utf-8")

    def trace(self, record: Dict[str, Any]) -> None:
        with self._lock, open(self.root / "trace.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), **record}, default=str) + "\n")

    # ------------------------------------------------------------ builds
    def builds(self) -> List[Dict[str, Any]]:
        out = []
        for d in sorted((self.root / "builds").glob("v*"), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
            mf = d / "manifest.json"
            if mf.exists():
                try:
                    out.append(json.loads(mf.read_text(encoding="utf-8")))
                except Exception:
                    pass
        return out

    def register_build(self, artifacts: Dict[str, str], manifest: Dict[str, Any]) -> Dict[str, Any]:
        n = len(self.builds()) + 1
        d = self.root / "builds" / f"v{n}"
        d.mkdir(parents=True, exist_ok=True)
        files = {}
        for kind, src in artifacts.items():
            dst = d / f"{self.meta().get('name', self.id)}_v{n}.{kind}".replace(" ", "_")
            shutil.copy2(src, dst)
            files[kind] = dst.name
        mf = {"version": n, "timestamp": time.time(), "files": files, **manifest}
        (d / "manifest.json").write_text(json.dumps(mf, indent=2), encoding="utf-8")
        return mf

    def build_file(self, version: int, kind: str) -> Optional[Path]:
        mf = self.root / "builds" / f"v{version}" / "manifest.json"
        if not mf.exists():
            return None
        data = json.loads(mf.read_text(encoding="utf-8"))
        name = data.get("files", {}).get(kind)
        return (self.root / "builds" / f"v{version}" / name) if name else None


class ProjectManager:
    @staticmethod
    def create(name: str = "") -> Project:
        pid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        p = Project(pid, workspace_root() / pid)
        p.update_meta(id=pid, name=name or f"project-{pid[-4:]}", created=time.time())
        return p

    @staticmethod
    def open(project_id: str) -> Project:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", project_id or ""):
            raise ValueError("Bad project id")
        root = workspace_root() / project_id
        if not root.is_dir():
            raise FileNotFoundError(project_id)
        return Project(project_id, root)

    @staticmethod
    def list() -> List[Dict[str, Any]]:
        out = []
        for d in sorted(workspace_root().iterdir(), reverse=True):
            if d.is_dir():
                try:
                    out.append(Project(d.name, d).meta())
                except Exception:
                    pass
        return out
