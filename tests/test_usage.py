import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from app.db import LLMCall, session, utcnow
from app.llm import usage
from app.llm.base import LLMError
from app.llm.claude_cli import ClaudeCLI

FAKE = [sys.executable, str(Path(__file__).parent / "fake_claude.py")]
CLI_RESULT = {"usage": {"input_tokens": 9, "output_tokens": 9},
              "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 1200, "outputTokens": 300, "cacheReadInputTokens": 5000,
                                                   "cacheCreationInputTokens": 40, "costUSD": 0.012},
                             "claude-haiku-5-5": {"inputTokens": 100, "outputTokens": 20, "costUSD": 0.001}},
              "total_cost_usd": 0.013}


def test_record_per_model_and_summary():
    usage.record("chat", "claude-sonnet-5-5", CLI_RESULT, ok=True, duration_ms=900)
    usage.record("ads", "claude-opus-5-5", {"usage": {"input_tokens": 2000, "output_tokens": 800}, "total_cost_usd": 0.05},
                 ok=True)
    usage.record("chat", "claude-sonnet-5-5", None, ok=False, error="timeout")
    with session() as db:                                      # an old call: counts for "all", not for 7 days / today
        db.add(LLMCall(ts=utcnow() - timedelta(days=10), purpose="chat", model="claude-sonnet-5-5", call_part=0,
                       input_tokens=1000, output_tokens=0))
        db.commit()
    s = usage.summary()
    g = {(x["purpose"], x["model"]): x for x in s["groups"]}
    chat = g[("chat", "claude-sonnet-5-5")]
    assert chat["calls"] == 3 and chat["errors"] == 1          # the haiku part of the same call is not a second call
    assert chat["today"] == 1500 and chat["week"] == 1500 and chat["all"] == 2500 and chat["cache_read"] == 5000
    assert g[("chat", "claude-haiku-5-5")]["calls"] == 0 and g[("chat", "claude-haiku-5-5")]["all"] == 120
    assert g[("ads", "claude-opus-5-5")]["all"] == 2800       # fallback to the snake_case usage block
    assert s["total"]["calls"] == 4 and s["total"]["all"] == 1500 + 120 + 2800 + 1000
    assert [x["purpose"] for x in s["groups"]][0] == "chat"


def test_month_by_model_and_task():
    usage.record("chat", "claude-sonnet-5-5", CLI_RESULT, ok=True)            # 1500 Sonnet + 120 Haiku
    usage.record("ads", "opus", {"usage": {"input_tokens": 2000, "output_tokens": 800}}, ok=True)   # alias as model id
    usage.record("chat", "claude-sonnet-5-5", None, ok=False, error="timeout")  # no tokens: not listed
    with session() as db:                                                       # last month: not this month's use
        db.add(LLMCall(ts=utcnow() - timedelta(days=40), purpose="chat", model="claude-fable-5-1", call_part=0,
                       input_tokens=9000, output_tokens=0))
        db.commit()
    by_model = usage.month_by_model()
    assert [(m["label"], m["tokens"]) for m in by_model] == [("Opus", 2800), ("Sonnet 5.5", 1500), ("Haiku 5.5", 120)]
    assert by_model[0]["color"] == usage.MODEL_COLORS["opus"] and by_model[2]["color"] == usage.MODEL_COLORS["haiku"]
    assert [(x["tokens"]) for x in usage.month_by_task()] == [2800, 1620]
    from fastapi.testclient import TestClient

    from app.db import set_setting
    from app.main import app
    set_setting("claude_token_limit_m", "10")                                   # the sidebar meter needs a limit
    with TestClient(app) as c:
        home = c.get("/").text
    pop = home[home.index('id="claude-pop"'):]
    assert ("By task" in pop or "Nach Aufgabe" in pop) and ("By model" in pop or "Nach Modell" in pop)
    assert pop.index("Opus") < pop.index("Sonnet 5.5") < pop.index("Haiku 5.5")   # biggest first, by model
    costs = usage.month_cost_by_model()                       # from the tokens: 2 × (1200 + 2 × 40 + 0.05 × 5000) + 10 × 300
    assert [(c["label"], round(c["cost_usd"], 5)) for c in costs] == [("Sonnet 5.5", 0.00606), ("Haiku 5.5", 0.00002)]
    assert ("API price" in pop or "API-Preis" in pop) and "$0.01" in pop and "$0.00" in pop   # total 0.013 and the Haiku part


async def test_cli_records_purpose_and_errors(monkeypatch):
    cli = ClaudeCLI(cmd=FAKE, model="claude-opus-5-5", purpose="screen")
    await cli.complete(system="s", prompt="p", schema={"type": "object"})
    monkeypatch.setenv("FAKE_CLAUDE_ERROR", "1")
    with pytest.raises(LLMError):
        await cli.complete(system="s", prompt="p", schema={"type": "object"})
    with session() as db:
        rows = list(db.exec(select(LLMCall)))
    assert [(r.purpose, r.ok) for r in rows] == [("screen", True), ("screen", False)]
    assert rows[1].error and "OAuth" in rows[1].error


def test_settings_panel_shows_usage(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    usage.record("chat", "claude-sonnet-5-5", CLI_RESULT, ok=True)
    with TestClient(app) as c:
        page = c.get("/settings/claude").text
    assert "Token usage" in page or "Token-Verbrauch" in page
    assert "claude-sonnet-5-5" in page and "1.200" in page   # tokens in of the chat call


def test_recording_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(usage, "session", boom)
    usage.record("chat", "m", CLI_RESULT, ok=True)            # must not raise


def test_cost_from_the_tokens_and_the_api_prices():
    from app.llm.base import cost_usd
    # Sonnet 5.5: $2 in, $10 out, cache reads 0.05x, cache writes (1 hour) 2x
    assert cost_usd("claude-sonnet-5-5", 1000, 1000, 10_000, 1000) == round((2000 + 10_000 + 1000 + 4000) / 1e6, 6)
    assert cost_usd("claude-opus-5-5[1m]", 0, 1_000_000) == 20.0                 # a suffixed id is still Opus
    assert cost_usd("claude-fable-5-1", 0, 0, 1_000_000) == 0.25                # 0.025x on Fable 5.1
    assert cost_usd("claude-haiku-5-5", 1000, 1000) == round((100 + 500) / 1e6, 6)
    assert cost_usd("claude-haiku-5-5", 0, 1000, 99_000, 2000) == round((0.5 * 99_000 * 0.1 + 0.5 * 2 * 2000 + 2.5 * 1000) / 1e6, 6)  # > 100K
    assert cost_usd("some-other-model", 10, 10) is None


def test_record_prices_the_call_and_old_rows_are_repriced():
    from sqlmodel import select

    from app.db import LLMCall, session
    from app.llm import usage
    data = {"modelUsage": {"claude-haiku-5-5": {"inputTokens": 1000, "outputTokens": 1000, "cacheReadInputTokens": 0,
                                               "cacheCreationInputTokens": 0, "costUSD": 0.012}}}   # the CLI's Sonnet price
    usage.record("screen", "claude-haiku-5-5", data, ok=True)
    with session() as db:
        db.add(LLMCall(purpose="chat", model="claude-sonnet-5-5", ok=True, input_tokens=0, output_tokens=0,
                       cache_read_tokens=1_000_000, cache_write_tokens=0, cost_usd=0.20))   # read at 0.1x
        db.add(LLMCall(purpose="chat", model="mystery", ok=True, input_tokens=5, output_tokens=5, cost_usd=0.5))
        db.commit()
    assert usage.reprice_calls() == 1 and usage.reprice_calls() == 0           # once
    with session() as db:
        costs = {r.model: r.cost_usd for r in db.exec(select(LLMCall))}
    assert costs == {"claude-haiku-5-5": 0.0006, "claude-sonnet-5-5": 0.1, "mystery": 0.5}


def test_the_token_limit_counts_a_rolling_period_a_week_by_default():
    from app.db import set_setting
    with session() as db:
        db.add(LLMCall(ts=utcnow() - timedelta(days=3), purpose="chat", model="m", call_part=0, input_tokens=1000, output_tokens=0))
        db.add(LLMCall(ts=utcnow() - timedelta(days=10), purpose="chat", model="m", call_part=0, input_tokens=5000, output_tokens=0))
        db.commit()
    set_setting("claude_token_limit_m", "0.002")
    st = usage.limit()
    assert st["days"] == 7 and st["used"] == 1000 and not st["over"]      # the 10-day-old call is outside the week
    set_setting("claude_limit_days", "30")
    st = usage.limit()
    assert st["days"] == 30 and st["used"] == 6000 and st["over"]
    set_setting("claude_limit_days", "500")
    assert usage.period_days() == usage.MAX_DAYS
