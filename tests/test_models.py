"""The selectable Claude models come from the API (GET /v1/models), the last good list is kept, and the static
list in llm.base is only the fallback before the first fetch."""
import httpx
import pytest
import respx

from app.db import get_setting
from app.llm import base, models, usage

API = {"data": [
    {"type": "model", "id": "claude-haiku-6", "display_name": "Claude Haiku 6", "line": "haiku", "lifecycle": "active",
     "created_at": "2027-01-10T00:00:00Z"},
    {"type": "model", "id": "claude-haiku-5-5", "display_name": "Claude Haiku 5.5", "line": "haiku", "lifecycle": "active",
     "created_at": "2026-10-07T18:00:00Z"},
    {"type": "model", "id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5", "line": "sonnet", "lifecycle": "active",
     "created_at": "2026-09-28T00:00:00Z"},
    {"type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "line": "opus", "lifecycle": "active",
     "created_at": "2026-09-21T16:24:00Z"},
    {"type": "model", "id": "claude-opus-4-1-20250805", "display_name": "Claude Opus 4.1", "line": "opus",
     "lifecycle": "retired", "created_at": "2025-08-05T00:00:00Z"},
    {"type": "model", "id": "not-a-claude-model", "display_name": "Other", "lifecycle": "active"},
]}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(models, "claude_token", lambda: "sk-ant-oat01-test")
    monkeypatch.setitem(models._cache, "models", None)
    monkeypatch.setitem(models._cache, "at", 0.0)
    yield


def test_static_fallback_before_first_fetch():
    assert models.choices() == base.MODELS
    assert models.label("claude-sonnet-5-5") == "Sonnet 5.5" and models.label("opus") == "Opus"
    assert models.label("gpt-9") == "gpt-9" and models.line_of("claude-sonnet-4-5-20250929") == "sonnet"
    assert models.resolve_alias("sonnet") == "claude-sonnet-5-5" and models.resolve_alias("claude-x") == "claude-x"


@respx.mock
async def test_fetch_filters_sorts_and_stores():
    route = respx.get(models.API_URL).mock(return_value=httpx.Response(200, json=API))
    got = await models.refresh()
    assert route.called and route.calls.last.request.headers["authorization"] == "Bearer sk-ant-oat01-test"
    assert [m["id"] for m in got] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-6", "claude-haiku-5-5"]
    assert got[2]["name"] == "Haiku 6" and models.label("claude-haiku-6") == "Haiku 6"
    assert models.newest("haiku") == "claude-haiku-6" and usage.haiku() == "claude-haiku-6"
    assert models.resolve_alias("haiku") == "claude-haiku-6"
    assert models.last_fetch["ok"] is True
    assert "claude-haiku-6" in get_setting("claude_models")           # persisted for the next start
    await models.refresh()                                          # within MAX_AGE_S: no second request
    assert route.call_count == 1


@respx.mock
async def test_fetch_failure_keeps_last_list(monkeypatch):
    respx.get(models.API_URL).mock(return_value=httpx.Response(200, json=API))
    await models.refresh()
    monkeypatch.setitem(models._cache, "models", None)               # a restart: only the stored list is left
    respx.get(models.API_URL).mock(return_value=httpx.Response(500))
    got = await models.refresh(max_age_s=0)
    assert [m["id"] for m in got][:1] == ["claude-opus-5-5"] and "claude-haiku-6" in models.ids()
    assert models.last_fetch["ok"] is False and "500" in models.last_fetch["error"]


@respx.mock
async def test_no_models_in_response_is_an_error():
    respx.get(models.API_URL).mock(return_value=httpx.Response(200, json={"data": [{"id": "x", "lifecycle": "active"}]}))
    assert models.choices() == base.MODELS
    assert await models.refresh() == [{"id": m, "name": n, "line": models.line_of(m)} for m, n in base.MODELS]
    assert models.last_fetch["ok"] is False


def test_settings_page_offers_fetched_models():
    from fastapi.testclient import TestClient

    from app.main import app
    with respx.mock(assert_all_called=False) as mock, TestClient(app) as c:
        mock.get(models.API_URL).mock(return_value=httpx.Response(200, json=API))
        page = c.get("/settings/claude").text
        assert 'value="claude-haiku-6"' in page and "Haiku 6" in page and 'value="claude-fable-5-1"' not in page
        assert "Model list from the Claude API" in page or "Modellliste von der Claude-API" in page
        r = c.post("/settings/claude", data={"model_screen": "haiku"}, follow_redirects=False)
        assert r.status_code == 303 and get_setting("llm_model_screen") == "claude-haiku-6"
        assert c.post("/settings/model", data={"purpose": "chat", "model": "claude-haiku-6"}, follow_redirects=False).status_code == 303
        assert c.post("/settings/model", data={"purpose": "chat", "model": "claude-fable-5-1"}).status_code == 400
        assert get_setting("llm_model_chat") == "claude-haiku-6"
        # the fetch failed on the next opening: the page says so and still offers the last list
        mock.get(models.API_URL).mock(return_value=httpx.Response(503))
        models._cache["models"] = None
        page = c.get("/settings/claude").text
        assert 'value="claude-haiku-6"' in page and ("could not be fetched" in page or "konnte nicht" in page)
