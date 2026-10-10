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
