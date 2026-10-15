"""Unit tests for Stage 17: FastAPI Web Server and Endpoints."""

from fastapi.testclient import TestClient
from embedforge.server import app

def test_web_server_index():
    client = TestClient(app)
    res = client.get("/")
    assert res.status_code == 200
    assert "EmbedForge" in res.text

def test_web_server_config():
    client = TestClient(app)
    res = client.get("/api/config")
    assert res.status_code == 200
    data = res.json()
    assert "nebius_model" in data
    assert "langsmith_active" in data

def test_upload_image_schematic_and_zip():
    import io
    import zipfile
    client = TestClient(app)

    # 1. Create a project
    p_res = client.post("/api/projects", json={"name": "test-upload-suite"})
    assert p_res.status_code == 200
    pid = p_res.json()["id"]

    # 2. Build dummy zip with src/driver.c and schematics/board.sch
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as z:
        z.writestr("src/driver.c", "/* Driver Code */\n#include <stdint.h>\n")
        z.writestr("schematics/board.sch", "(schematic (version 20240101) (net PB6))")
    zip_bytes = zip_buf.getvalue()

    # 3. Upload image, schematic CAD, and zip archive
    files = [
        ("files", ("pinout.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRdummy", "image/png")),
        ("files", ("motor_control.sch", b"(kicad_sch (version 2023) (component ESC))", "text/plain")),
        ("files", ("project_bundle.zip", zip_bytes, "application/zip")),
    ]
    up_res = client.post(f"/api/projects/{pid}/upload", files=files)
    assert up_res.status_code == 200
    data = up_res.json()
    assert data["ok"] is True
    assert len(data["uploaded"]) == 3

    # 4. Verify file retrieval for uploaded image
    img_meta = client.get(f"/api/projects/{pid}/file?path=docs/images/pinout.png")
    assert img_meta.status_code == 200
    assert img_meta.json()["is_binary"] is True
    assert img_meta.json()["kind"] == "image"

    # 5. Verify raw file retrieval
    raw_res = client.get(f"/api/projects/{pid}/file/raw?path=docs/images/pinout.png")
    assert raw_res.status_code == 200
    assert raw_res.content == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRdummy"

    # 6. Verify zip extracted files
    c_res = client.get(f"/api/projects/{pid}/file?path=src/driver.c")
    assert c_res.status_code == 200
    assert "Driver Code" in c_res.json()["content"]

    # 7. Verify schematic was indexed into sources
    sources_urls = [s["url"] for s in data["sources"]]
    assert any("motor_control.sch" in u or "board.sch" in u for u in sources_urls)


def test_upload_pdf_schematic_extraction():
    import io
    import pypdf
    client = TestClient(app)

    # 1. Create project
    p_res = client.post("/api/projects", json={"name": "test-pdf-schematic"})
    pid = p_res.json()["id"]

    # 2. Create minimal PDF bytes using pypdf
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    pdf_buf = io.BytesIO()
    writer.write(pdf_buf)
    pdf_bytes = pdf_buf.getvalue()

    # 3. Upload PDF schematic
    files = [("files", ("stm32_schematic.pdf", pdf_bytes, "application/pdf"))]
    up_res = client.post(f"/api/projects/{pid}/upload", files=files)
    assert up_res.status_code == 200
    data = up_res.json()
    assert data["ok"] is True
    assert data["uploaded"][0]["kind"] == "pdf"
    assert data["uploaded"][0]["path"] == "docs/schematics/stm32_schematic.pdf"

    # 4. Verify PDF metadata & raw access
    pdf_meta = client.get(f"/api/projects/{pid}/file?path=docs/schematics/stm32_schematic.pdf")
    assert pdf_meta.status_code == 200
    assert pdf_meta.json()["is_binary"] is True
    assert pdf_meta.json()["kind"] == "pdf"

    raw_res = client.get(f"/api/projects/{pid}/file/raw?path=docs/schematics/stm32_schematic.pdf")
    assert raw_res.status_code == 200
    assert raw_res.headers["content-type"] == "application/pdf"
    assert len(raw_res.content) == len(pdf_bytes)
