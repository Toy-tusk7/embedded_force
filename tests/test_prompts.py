from fastapi.testclient import TestClient
from embedforge.server import app
from embedforge.agent.prompts import (
    list_prompts,
    set_active_prompt,
    set_custom_prompt,
    get_active_prompt_id,
    get_active_prompt_text,
    PROMPT_STRICT,
    PROMPT_FAST,
)


def test_prompt_presets_and_switching():
    # Reset to strict
    set_active_prompt("strict")
    assert get_active_prompt_id() == "strict"
    assert "TRUTH RULES" in get_active_prompt_text()

    # Switch to fast
    set_active_prompt("fast")
    assert get_active_prompt_id() == "fast"
    assert "Fast Prototype" in [p["name"] for p in list_prompts()]
    assert "agile embedded-firmware" in get_active_prompt_text()

    # Custom prompt
    set_custom_prompt("Custom test prompt for STM32 and ESC")
    assert get_active_prompt_id() == "custom"
    assert get_active_prompt_text() == "Custom test prompt for STM32 and ESC"

    # Reset back to strict
    set_active_prompt("strict")
    assert get_active_prompt_id() == "strict"


def test_prompt_api_endpoints():
    client = TestClient(app)

    # GET /api/prompts
    r = client.get("/api/prompts")
    assert r.status_code == 200
    data = r.json()
    assert "active" in data
    assert len(data["prompts"]) >= 2

    # POST /api/prompts/select
    r = client.post("/api/prompts/select", json={"id": "fast"})
    assert r.status_code == 200
    assert r.json()["active"] == "fast"

    # POST /api/prompts/edit
    r = client.post("/api/prompts/edit", json={"text": "My custom prompt"})
    assert r.status_code == 200
    assert r.json()["active"] == "custom"
    assert r.json()["active_text"] == "My custom prompt"

    # Restore strict
    client.post("/api/prompts/select", json={"id": "strict"})
