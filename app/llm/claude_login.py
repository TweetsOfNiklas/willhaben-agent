"""Claude login from the dashboard: runs `claude setup-token` inside the container.

Flow: start() spawns the CLI in a pseudo-terminal and reads the sign-in URL it prints; the user opens
it, signs in with the Claude subscription and pastes the code shown afterwards; submit_code() types
that code into the CLI and picks the long-lived token (`sk-ant-oat01-...`, one year) from its output.
The token is stored in DATA_DIR/claude-token (mode 600) and takes precedence over
CLAUDE_CODE_OAUTH_TOKEN from .env. It is never logged.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import re
import secrets
import struct
import termios
import time
from pathlib import Path

from app.config import get_settings
from app.i18n import t

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https://(?:claude\.com|claude\.ai|console\.anthropic\.com|platform\.claude\.com)/\S*oauth/authorize\?\S+")
_TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]{20,}")
# Either the token or the CLI's error prompt ends a code submission.
_DONE_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]{20,}|OAuth error[^\n]*|Press Enter to retry")
_ERROR_RE = re.compile(r"(?:OAuth )?error[^\n]*", re.IGNORECASE)
_CURSOR_RIGHT_RE = re.compile(r"\x1b\[(\d*)C")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>()][0-9A-Za-z]?")
_SESSION_TIMEOUT_S = 600


def token_path() -> Path:
    return get_settings().data_dir / "claude-token"


def stored_token() -> str | None:
    try:
        token = token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def claude_token() -> str | None:
    """Token for `claude -p`: dashboard login first, then CLAUDE_CODE_OAUTH_TOKEN from .env."""
    return stored_token() or get_settings().claude_code_oauth_token or None


def token_source() -> str | None:
    if stored_token():
        return "login"
    if get_settings().claude_code_oauth_token:
        return "env"
    return None


def save_token(token: str) -> None:
    p = token_path()
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    os.replace(tmp, p)


def forget_token() -> None:
    token_path().unlink(missing_ok=True)


def _masked(text: str) -> str:
    """Output for the log: every long word (token, code) replaced, structure kept."""
    return re.sub(r"[A-Za-z0-9_\-#]{12,}", lambda m: f"•••({len(m.group(0))})", text)


def clean_terminal(raw: str) -> str:
    """Terminal output -> plain text (Ink draws spaces as cursor moves)."""
    text = _CURSOR_RIGHT_RE.sub(lambda m: " " * int(m.group(1) or 1), raw)
    return _ANSI_RE.sub("", text).replace("\r", "")


class ClaudeLogin:
    """One `claude setup-token` run. States: idle, need_code, verifying, done, error."""

    def __init__(self, cmd: list[str] | None = None) -> None:
        self.cmd = cmd or [get_settings().claude_bin, "setup-token"]
        self.state = "idle"
        self.url: str | None = None
        self.error: str | None = None
        self.started_at = 0.0
        self.attempt: str | None = None   # ties the code form to the run that printed the link
        self._proc: asyncio.subprocess.Process | None = None
        self._fd: int | None = None
        self._buf = ""
        self._raw = ""
        self._changed = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self.state in ("need_code", "verifying") and time.monotonic() - self.started_at < _SESSION_TIMEOUT_S

    async def start(self) -> str:
        """Start a fresh run and return the sign-in URL."""
        async with self._lock:
            await self._stop()
            self.state, self.url, self.error, self._buf, self._raw = "idle", None, None, "", ""
            self.attempt = secrets.token_hex(4)
            master, slave = pty.openpty()
            # Wide terminal so the URL and the token are printed on one line each.
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))
            env = dict(os.environ)
            for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DISPLAY"):
                env.pop(k, None)
            env.update({"TERM": "xterm-256color", "BROWSER": "true", "DISABLE_AUTOUPDATER": "1"})
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self.cmd, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
            except FileNotFoundError as e:
                os.close(master)
                os.close(slave)
                self.state, self.error = "error", t("claude CLI not found: {error}", error=e)
                raise RuntimeError(self.error) from e
            os.close(slave)
            self._fd = master
            os.set_blocking(master, False)
            asyncio.get_running_loop().add_reader(master, self._on_output)
            self.started_at = time.monotonic()
            m = await self._wait_for(_URL_RE, 30)
            if not m:
                self.state, self.error = "error", t("No sign-in address received: {output}", output=self._tail())
                await self._stop()
                raise RuntimeError(self.error)
            self.url, self.state = m.group(0), "need_code"
            log.info("claude login %s: waiting for code", self.attempt)
            return self.url

    async def submit_code(self, code: str, attempt: str | None = None) -> bool:
        """Type the code from the sign-in page into the CLI; True once the token is saved."""
        code = "".join(code.split())
        async with self._lock:
            if attempt and attempt != self.attempt:
                self.error = t("The code belongs to an older sign-in attempt (the link has been generated again since). Please open the current link and paste its code.")
                log.info("claude login: code for stale attempt %s (current %s)", attempt, self.attempt)
                return False
            if not self.active or self._fd is None:
                self.state, self.error = "error", t("The sign-in is no longer active (after 10 min or because the app restarted) – please click “Sign in with Claude” again")
                return False
            self.state = "verifying"
            mark = len(self._buf)
            # A long code written together with Enter counts as one paste and the Enter is swallowed
            # (verified with CLI 2.1.286); send Enter on its own once the paste has been taken.
            os.write(self._fd, code.encode())
            await asyncio.sleep(0.8)
            os.write(self._fd, b"\r")
            m = await self._wait_for(_DONE_RE, 120, since=mark)
            token = _TOKEN_RE.search(self._buf, mark) or _TOKEN_RE.search(re.sub(r"\s+", "", self._buf[mark:]))
            if token:
                save_token(token.group(0))
                self.state, self.error = "done", None
                log.info("claude login %s: token stored", self.attempt)
            else:
                errors = _ERROR_RE.findall(self._buf[mark:])
                why = errors[-1].strip() if errors else (t("no answer from Claude") if not m else self._tail(since=mark))
                self.state, self.error = "error", t("Claude rejected the code ({reason}). Please sign in again and use the fresh code.", reason=why)
                log.info("claude login %s: failed: %s", self.attempt, why)
                log.info("claude login %s: output after code (masked): %r", self.attempt, _masked(self._buf[mark:])[-1500:])
            await self._stop()
            return self.state == "done"

    async def cancel(self) -> None:
        async with self._lock:
            await self._stop()
            self.state = "idle"

    # ------------------------------------------------------------------ internals
    def _on_output(self) -> None:
        try:
            data = os.read(self._fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:      # child closed the terminal
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self._fd)
        self._raw = (self._raw + data.decode("utf-8", "replace"))[-400_000:]
        self._buf = clean_terminal(self._raw)
        self._changed.set()

    async def _wait_for(self, pattern: re.Pattern[str], timeout: float, since: int = 0) -> re.Match[str] | None:
        deadline = time.monotonic() + timeout
        while True:
            m = pattern.search(self._buf, since)
            if m:
                return m
            if self._proc and self._proc.returncode is not None:
                return pattern.search(self._buf, since)
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), min(left, 1.0))
            except asyncio.TimeoutError:
                pass

    def _tail(self, since: int = 0) -> str:
        text = _TOKEN_RE.sub("<token>", self._buf[since:])
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and len(ln.strip()) > 2]
        return " / ".join(lines[-4:])[-300:] or t("no output")

    async def _stop(self) -> None:
        if self._fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self._fd)
            except (ValueError, RuntimeError):
                pass
            os.close(self._fd)
            self._fd = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None
        self._buf = self._raw = ""


_login: ClaudeLogin | None = None


def get_login() -> ClaudeLogin:
    global _login
    if _login is None:
        _login = ClaudeLogin()
    return _login
