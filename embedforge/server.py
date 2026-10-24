"""FastAPI server for the EmbedForge IDE: agent runs (SSE), projects, real build artifacts."""

import io
import json
import mimetypes
import os
import re
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import time

from embedforge.core.config import config
from embedforge.agent import AgentRunner, ProjectManager, RunSession
from embedforge.agent.llm import default_model, default_provider, list_models, providers
from embedforge.agent.loop import visible_user_text
from embedforge.agent.workspace import (
    HIDDEN_DIRS, TEXT_EXTS, IMAGE_EXTS, DOC_EXTS, ARCHIVE_EXTS, ALLOWED_EXTS
)
from embedforge import eda
from embedforge.truth import SourceStore
from embedforge.toolchain import pio

app = FastAPI(title="EmbedForge IDE API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _require_password(request, call_next):
    """With EMBEDFORGE_PASSWORD set (a hosted demo), every request needs it: HTTP Basic auth, any user name. The
    browser asks once and sends it with every later request of the page, the event stream included."""
    import base64
    import hmac
    password = config.server_password
    if password:
        auth = request.headers.get("authorization", "")
        given = ""
        if auth[:6].lower() == "basic ":
            try:
                given = base64.b64decode(auth[6:]).decode("utf-8", "replace").partition(":")[2]
            except ValueError:
                given = ""
        if not hmac.compare_digest(given.encode(), password.encode()):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="EmbedForge"'})
    return await call_next(request)

WEB_DIR = Path(__file__).parent / "web"

_RUNS: Dict[str, RunSession] = {}
_ACTIVE: Dict[str, str] = {}  # project_id -> run_id
_LOCK = threading.Lock()


def _extract_pdf_text(data: bytes) -> str:
    try:   # Markdown for the datasheet analyzer when PyMuPDF/pymupdf4llm are installed
        from embedforge.truth.pdf_markdown import pdf_to_markdown
        md = pdf_to_markdown(data)
        if md and md.strip():
            return md
    except Exception:
        pass
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = []
        for idx, page in enumerate(reader.pages):
            txt = page.extract_text() or ""
            if txt.strip():
                pages.append(f"--- Page {idx + 1} ---\n{txt.strip()}")
        return "\n\n".join(pages)
    except Exception as e:
        return f"[PDF parsing note: {e}]"


class CreateProject(BaseModel):
    name: str = ""


class ChatRequest(BaseModel):
    message: str
    attachments: Optional[List[str]] = None


class DebugRequest(BaseModel):
    symptom: str
    serial_log: Optional[str] = None
    measured_voltage: Optional[float] = None
    wiring_notes: Optional[str] = None
    scope_notes: Optional[str] = None


from embedforge.agent.prompts import (
    list_prompts,
    set_active_prompt,
    set_custom_prompt,
    get_active_prompt_id,
    get_active_prompt_text,
)

class SelectPromptRequest(BaseModel):
    id: str


class SelectModelRequest(BaseModel):
    model: str
    provider: str = "nebius"


class EditPromptRequest(BaseModel):
    text: str


# ------------------------------------------------------------------ static
@app.get("/")
def serve_index():
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Web assets not found")
    return FileResponse(index_file)


@app.get("/style.css")
def serve_css():
    return FileResponse(WEB_DIR / "style.css", media_type="text/css")


@app.get("/app.js")
def serve_js():
    return FileResponse(WEB_DIR / "app.js", media_type="application/javascript")


# ------------------------------------------------------------------ status
_PIO_STATUS: Dict[str, Any] = {"t": 0.0, "ok": False}


def _pio_available() -> bool:
    """`pio --version` spawns a Python process; cache it so the UI's periodic status poll stays cheap."""
    if time.time() - _PIO_STATUS["t"] > 300:
        _PIO_STATUS.update(t=time.time(), ok=pio.is_available())
    return _PIO_STATUS["ok"]


@app.get("/api/config")
def get_system_config():
    return {
        "nebius_model": config.agent_model,
        "nebius_active": config.has_nebius,
        "llm_active": config.has_llm,
        "default_provider": default_provider(),
        "tavily_active": config.has_tavily,
        "langsmith_active": bool(os.getenv("LANGCHAIN_API_KEY")),
        "langsmith_project": os.getenv("LANGCHAIN_PROJECT", "EmbedForge"),
        "platformio_available": _pio_available(),
        "toolchains": sorted(p.name for p in (pio.core_dir() / "packages").glob("*")) if (pio.core_dir() / "packages").is_dir() else [],
    }


# ------------------------------------------------------------------ models
@app.get("/api/models")
def get_models(refresh: bool = False):
    models: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    provs = []
    for pid, p in providers().items():
        provs.append({"id": pid, "name": p.name, "configured": p.configured})
        if p.configured:
            res = list_models(pid, force=refresh)
            models += res["models"]
            if res["error"]:
                errors[pid] = res["error"]
    return {"default": {"provider": default_provider(), "model": default_model(default_provider())},
            "providers": provs, "models": models, "errors": errors}


@app.post("/api/projects/{project_id}/model")
def set_project_model(project_id: str, req: SelectModelRequest):
    p = _project(project_id)
    prov = providers().get(req.provider)
    if prov is None or not prov.configured:
        raise HTTPException(status_code=400, detail=f"Provider '{req.provider}' is not configured.")
    listed = {m["id"] for m in list_models(req.provider)["models"]}
    if listed and req.model not in listed:
        raise HTTPException(status_code=400, detail=f"Model '{req.model}' is not offered by {prov.name}.")
    return p.update_meta(model=req.model, provider=req.provider)


# ------------------------------------------------------------------ prompts
@app.get("/api/prompts")
def get_prompts():
    return {
        "active": get_active_prompt_id(),
        "prompts": list_prompts(),
        "active_text": get_active_prompt_text(),
    }


@app.post("/api/prompts/select")
def select_prompt_endpoint(req: SelectPromptRequest):
    new_id = set_active_prompt(req.id)
    return {"ok": True, "active": new_id, "prompts": list_prompts(), "active_text": get_active_prompt_text()}


@app.post("/api/prompts/edit")
def edit_prompt_endpoint(req: EditPromptRequest):
    set_custom_prompt(req.text)
    return {"ok": True, "active": "custom", "prompts": list_prompts(), "active_text": get_active_prompt_text()}


# ------------------------------------------------------------------ projects
def _project(project_id: str):
    try:
        return ProjectManager.open(project_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="Project not found")


@app.get("/api/projects")
def list_projects():
    return ProjectManager.list()


@app.post("/api/projects")
def create_project(req: CreateProject):
    return ProjectManager.create(req.name).meta()


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    p = _project(project_id)
    ledger = []
    lp = p.root / "ledger.json"
    if lp.exists():
        ledger = json.loads(lp.read_text(encoding="utf-8"))
    sources = []
    sp = p.root / "sources" / "index.json"
    if sp.exists():
        sources = json.loads(sp.read_text(encoding="utf-8"))
    plan = (p.root / "artifacts" / "task.md")
    walk = (p.root / "artifacts" / "walkthrough.md")
    chat = []
    for m in p.load_conversation():
        if m.get("role") == "user":
            chat.append({"role": "user", "text": visible_user_text(m["content"])})
        elif m.get("role") == "assistant" and m.get("content"):
            chat.append({"role": "assistant", "text": m["content"]})
    with _LOCK:
        active = _ACTIVE.get(project_id)
        if active and (active not in _RUNS or _RUNS[active].done):
            _ACTIVE.pop(project_id, None)
            active = None
    board = eda.load_board(p.root)
    return {
        "meta": p.meta(), "files": p.list_files(), "builds": p.builds(), "ledger": ledger, "sources": sources,
        "board": board.summary() if board else None,
        "plan": plan.read_text(encoding="utf-8") if plan.exists() else "",
        "walkthrough": walk.read_text(encoding="utf-8") if walk.exists() else "",
        "chat": chat,
        "active_run": active,
        "verify": _last_verify(p.root),
    }


def _last_verify(root: Path) -> Optional[Dict[str, Any]]:
    """The last verify report of a project, and whether it still describes the current sources."""
    from embedforge.truth.verifier import build_inputs_hash
    vr = root / "verify_report.json"
    if not vr.exists():
        return None
    try:
        v = json.loads(vr.read_text(encoding="utf-8"))
        return {**v["report"], "summary": v.get("summary", ""), "current": v.get("hash") == build_inputs_hash(root)}
    except (ValueError, KeyError, OSError):
        return None


@app.get("/api/projects/{project_id}/file")
def get_file(project_id: str, path: str):
    p = _project(project_id)
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_EXTS:
        return {"path": path, "is_binary": True, "kind": "image", "raw_url": f"/api/projects/{project_id}/file/raw?path={path}"}
    if suffix in DOC_EXTS:
        return {"path": path, "is_binary": True, "kind": "pdf", "raw_url": f"/api/projects/{project_id}/file/raw?path={path}"}
    try:
        return {"path": path, "content": p.read_file(path), "is_binary": False}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/projects/{project_id}/file/raw")
def get_raw_file(project_id: str, path: str):
    p = _project(project_id)
    try:
        f = p.safe_path(path, allow_binary=True)
        if not f.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        media_type, _ = mimetypes.guess_type(str(f))
        return FileResponse(f, media_type=media_type or "application/octet-stream", filename=f.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/projects/{project_id}/builds/{version}/{kind}")
def download_build(project_id: str, version: int, kind: str):
    p = _project(project_id)
    if kind not in ("hex", "bin", "elf", "uf2"):
        raise HTTPException(status_code=400, detail="Unknown artifact kind")
    f = p.build_file(version, kind)
    if not f or not f.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(f, filename=f.name, media_type="application/octet-stream")


@app.get("/api/projects/{project_id}/download.zip")
def download_zip(project_id: str):
    p = _project(project_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in p.root.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(p.root)
            if rel.parts[0] in (HIDDEN_DIRS - {"builds"}) or rel.name in ("conversation.json", "trace.jsonl"):
                continue
            z.write(f, str(rel))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{project_id}.zip"'})


@app.post("/api/projects/{project_id}/upload")
async def upload_files(
    project_id: str,
    files: List[UploadFile] = File(...),
    extract_zip: bool = Form(True),
):
    p = _project(project_id)
    store = SourceStore(p.root)
    uploaded_records = []
    eda_imported = False

    for f in files:
        raw_name = Path(f.filename or "uploaded_file").name
        clean_name = re.sub(r"[^\w\.\-\+]", "_", raw_name)
        suffix = Path(clean_name).suffix.lower()
        content = await f.read()
        if not content:
            continue

        # EasyEDA / netlist / BOM exports are recognised by content and feed the board model.
        eda_kind = eda.detect(clean_name, content)
        if eda_kind:
            dest = f"{eda.EDA_DIR}/{clean_name}"
            p.write_bytes(dest, content)
            eda_imported = True
            uploaded_records.append({
                "name": clean_name, "path": dest, "kind": "eda", "format": eda.FORMAT_LABELS[eda_kind], "size": len(content),
                "summary": f"Imported {eda.FORMAT_LABELS[eda_kind]} {clean_name}",
            })
            continue

        if suffix == ".zip" and extract_zip:
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as z:
                    total_uncompressed = sum(info.file_size for info in z.infolist())
                    if total_uncompressed > 100_000_000:
                        raise HTTPException(status_code=400, detail="Zip file uncompressed contents exceed 100MB safety limit")

                    extracted_count = 0
                    for info in z.infolist():
                        if info.is_dir():
                            continue
                        norm = info.filename.replace("\\", "/").lstrip("/")
                        if ".." in Path(norm).parts or norm.startswith("/") or norm.startswith("__MACOSX"):
                            continue
                        if any(part in HIDDEN_DIRS for part in Path(norm).parts):
                            continue

                        entry_suffix = Path(norm).suffix.lower()
                        if entry_suffix in ALLOWED_EXTS:
                            data = z.read(info)
                            entry_kind = eda.detect(norm, data)
                            if entry_kind:  # e.g. an EasyEDA Standard project download (zip of JSON sources)
                                rel = norm if norm.startswith(eda.EDA_DIR + "/") else f"{eda.EDA_DIR}/{norm}"
                                p.write_bytes(rel, data)
                                eda_imported = True
                                extracted_count += 1
                                continue
                            p.write_bytes(norm, data)
                            extracted_count += 1

                            if entry_suffix == ".pdf":
                                pdf_txt = _extract_pdf_text(data)
                                s = store.add(f"user://upload/{norm}", f"Schematic/Datasheet: {Path(norm).name}", pdf_txt, kind="pdf")
                                store.keep_pdf(s.id, data)   # its figures can be read later (read_figure)
                            elif entry_suffix in {".sch", ".kicad_sch", ".kicad_pcb", ".brd"}:
                                sch_txt = data.decode("utf-8", errors="replace")
                                store.add(f"user://upload/{norm}", f"Schematic: {Path(norm).name}", sch_txt, kind="schematic")
                            elif entry_suffix in IMAGE_EXTS:
                                store.add(f"user://upload/{norm}", f"Image: {Path(norm).name}", f"Project image: {norm}", kind="image")

                    uploaded_records.append({
                        "name": clean_name,
                        "kind": "archive",
                        "size": len(content),
                        "extracted": extracted_count,
                        "summary": f"Extracted {extracted_count} file(s) from {clean_name}"
                    })
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail=f"Invalid or corrupted zip file: {clean_name}")
        elif suffix in IMAGE_EXTS:
            dest = f"docs/images/{clean_name}"
            p.write_bytes(dest, content)
            store.add(f"user://upload/{dest}", f"Image/Schematic: {clean_name}", f"Uploaded diagram/photo at {dest}.", kind="image")
            uploaded_records.append({
                "name": clean_name,
                "path": dest,
                "kind": "image",
                "size": len(content),
                "summary": f"Uploaded image {clean_name} to docs/images/"
            })
        elif suffix == ".pdf":
            dest = f"docs/schematics/{clean_name}"
            p.write_bytes(dest, content)
            pdf_txt = _extract_pdf_text(content)
            s = store.add(f"user://upload/{dest}", f"Schematic/Datasheet: {clean_name}", pdf_txt if pdf_txt.strip() else f"Schematic PDF: {clean_name}", kind="pdf")
            store.keep_pdf(s.id, content)   # its figures can be read later (read_figure)
            uploaded_records.append({
                "name": clean_name,
                "path": dest,
                "kind": "pdf",
                "size": len(content),
                "summary": f"Uploaded schematic/datasheet {clean_name} (indexed into Sources)"
            })
        elif suffix in {".sch", ".kicad_sch", ".kicad_pcb", ".brd", ".net"}:
            dest = f"schematics/{clean_name}"
            p.write_bytes(dest, content)
            sch_txt = content.decode("utf-8", errors="replace")
            store.add(f"user://upload/{dest}", f"Schematic CAD: {clean_name}", sch_txt, kind="schematic")
            uploaded_records.append({
                "name": clean_name,
                "path": dest,
                "kind": "schematic",
                "size": len(content),
                "summary": f"Uploaded CAD schematic {clean_name} to schematics/"
            })
        elif suffix in TEXT_EXTS:
            if clean_name.endswith((".c", ".cpp", ".cc", ".ino")):
                dest = f"src/{clean_name}"
            elif clean_name.endswith((".h", ".hpp")):
                dest = f"include/{clean_name}"
            else:
                dest = clean_name
            p.write_bytes(dest, content)
            if suffix in (".md", ".txt"):   # a document (e.g. a datasheet converted to Markdown): quotable like a PDF
                store.add(f"user://upload/{dest}", f"Document: {clean_name}", content.decode("utf-8", errors="replace"), kind="pdf")
            uploaded_records.append({
                "name": clean_name,
                "path": dest,
                "kind": "text",
                "size": len(content),
                "summary": f"Uploaded file {clean_name} to {dest}" + (" (indexed into Sources)" if suffix in (".md", ".txt") else "")
            })
        else:
            dest = f"docs/{clean_name}"
            p.write_bytes(dest, content)
            uploaded_records.append({
                "name": clean_name,
                "path": dest,
                "kind": "other",
                "size": len(content),
                "summary": f"Uploaded file {clean_name} to docs/"
            })

    board = None
    if eda_imported:
        board = eda.rebuild_project_board(p.root, store, name=p.meta().get("name") or project_id)

    sp = p.root / "sources" / "index.json"
    sources_data = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else []

    return {
        "ok": True,
        "uploaded": uploaded_records,
        "files": p.list_files(),
        "sources": sources_data,
        "count": len(uploaded_records),
        "board": board,
    }


@app.get("/api/projects/{project_id}/board")
def get_board(project_id: str):
    p = _project(project_id)
    board = eda.load_board(p.root)
    if board is None:
        return {"board": None, "summary": None}
    return {"board": board.to_json(), "summary": board.summary()}


@app.post("/api/projects/{project_id}/board/rebuild")
def rebuild_board(project_id: str):
    p = _project(project_id)
    summary = eda.rebuild_project_board(p.root, SourceStore(p.root), name=p.meta().get("name") or project_id)
    return {"summary": summary}


# ------------------------------------------------------------------ agent runs
def _start_run(project_id: str, message: str) -> Dict[str, Any]:
    project = _project(project_id)
    with _LOCK:
        active = _ACTIVE.get(project_id)
        if active and not _RUNS[active].done:
            raise HTTPException(status_code=409, detail="The agent is already working on this project.")
        run_id = uuid.uuid4().hex[:10]
        session = RunSession(run_id, project_id)
        _RUNS[run_id] = session
        _ACTIVE[project_id] = run_id

    def work():
        try:
            AgentRunner(project, session).run(message)
        except Exception as e:  # e.g. the project's saved provider was removed from .env
            session.emit("error", message=f"{type(e).__name__}: {e}")
            session.finish("failed")
        finally:
            with _LOCK:
                if _ACTIVE.get(project_id) == run_id:
                    _ACTIVE.pop(project_id, None)

    threading.Thread(target=work, name=f"agent-{run_id}", daemon=True).start()
    return {"run_id": run_id, "project_id": project_id}


@app.post("/api/projects/{project_id}/chat")
def chat(project_id: str, req: ChatRequest):
    msg = req.message.strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Empty message")
    if not config.has_llm:
        raise HTTPException(status_code=503, detail="No LLM provider configured (set SELFHOSTED_BASE_URL, NEBIUS_API_KEY, NVIDIA_API_KEY, ANTHROPIC_API_KEY or GEMINI_API_KEY)")
    if req.attachments:
        msg += f"\n\n[User uploaded attachments: {', '.join(req.attachments)}]"
    return _start_run(project_id, msg)


@app.post("/api/projects/{project_id}/debug")
def debug(project_id: str, req: DebugRequest):
    parts = ["[HARDWARE DEBUG REPORT from the bench]", f"Symptom: {req.symptom}"]
    if req.serial_log:
        parts.append(f"Serial log:\n{req.serial_log}")
    if req.measured_voltage is not None:
        parts.append(f"Measured supply voltage: {req.measured_voltage} V")
    if req.wiring_notes:
        parts.append(f"Wiring: {req.wiring_notes}")
    if req.scope_notes:
        parts.append(f"Scope/logic analyzer: {req.scope_notes}")
    parts.append("Diagnose using the existing project and ledger, research the datasheet/errata as needed, "
                 "fix the firmware if it is a software cause, rebuild and verify; otherwise tell me exactly what to check.")
    if not config.has_llm:
        raise HTTPException(status_code=503, detail="No LLM provider configured (set SELFHOSTED_BASE_URL, NEBIUS_API_KEY, NVIDIA_API_KEY, ANTHROPIC_API_KEY or GEMINI_API_KEY)")
    return _start_run(project_id, "\n".join(parts))


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str, start: int = 0):
    session = _RUNS.get(run_id)
    if not session:
        raise HTTPException(status_code=404, detail="Run not found")

    def gen():
        idx = start
        while True:
            evs = session.read_from(idx, timeout=15)
            if evs is None:
                yield f"event: end\ndata: {json.dumps({'status': session.status})}\n\n"
                return
            if not evs:
                yield ": keep-alive\n\n"
                continue
            for e in evs:
                idx = e["i"] + 1
                yield f"data: {json.dumps(e, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    session = _RUNS.get(run_id)
    if not session:
        raise HTTPException(status_code=404, detail="Run not found")
    session.cancel_requested = True
    return {"ok": True}


def main():
    import uvicorn
    host = os.getenv("EMBEDFORGE_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1") and not config.server_password:
        # reachable from other machines: without a password anyone could run the agent on your API credits
        raise SystemExit(f"EMBEDFORGE_HOST={host} makes the IDE reachable from other machines; set EMBEDFORGE_PASSWORD first.")
    port = int(os.getenv("EMBEDFORGE_PORT") or os.getenv("PORT") or "8000")   # PORT: the one a host like Render assigns
    if not port_free(host, port):
        # another server owns the port and may be running agents: marking its runs interrupted would be false
        raise SystemExit(f"Port {port} is already in use: an EmbedForge server is probably running already "
                         f"(open http://{host}:{port}). This one did not start and left every run as it is.")
    resume_interrupted_runs()
    uvicorn.run("embedforge.server:app", host=host, port=port, reload=False)


def port_free(host: str, port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def resume_interrupted_runs() -> List[str]:
    """Runs cut off by a server stop are recorded as interrupted; with EMBEDFORGE_RESUME_INTERRUPTED=1 they continue at
    once from their last saved step (off by default: a restart should not spend model credits unasked)."""
    from embedforge.agent.workspace import Project, workspace_root
    from embedforge.core.logger import logger
    cut = []
    for d in sorted(workspace_root().iterdir()):
        if d.is_dir() and (d / "trace.jsonl").exists() and Project(d.name, d).close_interrupted_run():
            cut.append(d.name)
    if cut:
        logger.log_event("RUNS_INTERRUPTED", f"{len(cut)} run(s) stopped with the server: {', '.join(cut)}")
        if os.getenv("EMBEDFORGE_RESUME_INTERRUPTED", "").strip() == "1" and config.has_llm:
            for pid in cut:
                _start_run(pid, "continue")
    return cut


if __name__ == "__main__":
    main()
