"""Settings → Claude: the relative token cost per model comes from llm.base.PRICES (one place), cheapest first."""
from fastapi.testclient import TestClient

from app.llm import base


def _page() -> str:
    from app.main import app

    with TestClient(app) as c:
        return c.get("/settings/claude").text


def test_every_selectable_model_has_a_price():
    assert {m for m, _n in base.MODELS} <= set(base.PRICES)


def test_factors_from_list_prices():
    f = base.cost_factors()
    assert f == {"claude-haiku-5-5": 1, "claude-sonnet-5-5": 20, "claude-opus-5-5": 40, "claude-fable-5-1": 100}
    assert list(f) == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"]   # cheapest first


def test_factors_follow_the_price_table(monkeypatch):
    monkeypatch.setitem(base.PRICES, "claude-opus-5-5", (1.0, 5.0))     # Opus at 10× Haiku
    monkeypatch.delitem(base.PRICES, "claude-fable-5-1")                 # unpriced models are left out
    assert base.cost_factors() == {"claude-haiku-5-5": 1, "claude-opus-5-5": 10, "claude-sonnet-5-5": 20}
    page = _page()
    assert "Haiku 1× · Opus 10× · Sonnet 20×" in page and "Fable 100×" not in page


def test_settings_page_shows_all_factors():
    page = _page()
    assert "Haiku 1× · Sonnet 20× · Opus 40× · Fable 100×" in page
    assert "Sonnet ≈ 4×" not in page and "Opus ≈ 20×" not in page
    assert "100K" in page                                                # Haiku's price only holds up to 100K-token prompts
    assert '>Fable 5.1</option>' in page and '>Haiku 5.5</option>' in page   # the selects: just the model names
    assert "faster" not in page and "fastest" not in page and "newest model" not in page
