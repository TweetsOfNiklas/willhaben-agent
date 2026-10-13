import sys
from pathlib import Path

import pytest

from app.llm import claude_login
from app.llm.claude_login import ClaudeLogin, claude_token, clean_terminal, token_source

FAKE = [sys.executable, str(Path(__file__).parent / "fake_setup_token.py")]


@pytest.fixture(autouse=True)
def _quick_enter(monkeypatch):
    monkeypatch.setattr(claude_login, "_ENTER_AFTER_PASTE_S", 0.05)   # the fake CLI reads line by line


def test_clean_terminal_keeps_spaces():
    assert clean_terminal("\x1b[1mWelcome\x1b[1Cto\x1b[2CX\x1b[22m\r\n") == "Welcome to  X\n"


async def test_login_flow_stores_token(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_login, "token_path", lambda: tmp_path / "claude-token")
    login = ClaudeLogin(cmd=FAKE)
    url = await login.start()
    assert url == "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz"
    assert login.state == "need_code" and login.active
    assert await login.submit_code(" good#state ")
    assert login.state == "done"
    assert claude_token() == "sk-ant-oat01-FAKEtoken_abcdefghijklmnopqrstuvwxyz0123"
    assert token_source() == "login"
    assert (tmp_path / "claude-token").stat().st_mode & 0o777 == 0o600


async def test_login_flow_bad_code(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_login, "token_path", lambda: tmp_path / "claude-token")
    login = ClaudeLogin(cmd=FAKE)
    await login.start()
    assert not await login.submit_code("wrong")
    assert login.state == "error" and "status code 400" in login.error
    assert claude_token() is None and token_source() is None


async def test_login_code_for_stale_attempt(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_login, "token_path", lambda: tmp_path / "claude-token")
    login = ClaudeLogin(cmd=FAKE)
    await login.start()
    old = login.attempt
    await login.start()                      # link regenerated, e.g. by a second click
    assert not await login.submit_code("good#state", old)
    assert "older sign-in attempt" in login.error and login.state == "need_code"
    assert await login.submit_code("good#state", login.attempt)
    await login.cancel()


def test_masked_hides_long_words():
    from app.llm.claude_login import _masked

    out = _masked("Your OAuth token:\nsk-ant-oat01-abcdefghijklmnop\ncode abcdefghijkl#mnopqrstu")
    assert "abcdefghijklmnop" not in out and "mnopqrstu" not in out and "Your OAuth token" in out


async def test_escape_split_across_reads(monkeypatch, tmp_path):
    login = ClaudeLogin(cmd=FAKE)
    raw = "x\x1b[1msk-ant-oat01-FAKEtoken_abcdefghijklmnopqrstuvwxyz0123\x1b[22m\r\n"
    cut = raw.index("[22m") - 1                    # split inside the escape sequence
    login._raw = ""
    for part in (raw[:cut], raw[cut:]):
        login._raw += part
        login._buf = claude_login.clean_terminal(login._raw)
    assert claude_login._TOKEN_RE.search(login._buf).group(0).endswith("xyz0123")
