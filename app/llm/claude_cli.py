"""Option B: Claude via the `claude -p` CLI, authenticated with the Max subscription.

Auth: `claude setup-token` -> CLAUDE_CODE_OAUTH_TOKEN (one year), either from .env or from the
dashboard login (app/llm/claude_login.py), which runs setup-token inside the container. The CLI must NOT run in
`--bare` mode, which ignores that token; the CLI version is pinned in the Dockerfile because
the docs announce that `--bare` will become the default for `-p` in a future release.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.i18n import t
from app.llm.base import LLMError, LLMResult, model_for
from app.llm.claude_login import claude_token

log = logging.getLogger(__name__)


class ClaudeCLI:
    def __init__(self, cmd: list[str] | None = None, model: str | None = None,
                 timeout_s: int | None = None, purpose: str | None = None) -> None:
        s = get_settings()
        self.purpose = purpose      # for the usage statistics (app/llm/usage.py)
        self.cmd = cmd or [s.claude_bin]
        self.model = model or model_for(None)
        self.timeout_s = timeout_s or s.llm_timeout_s
        self._token = claude_token()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # An API key would take precedence over the subscription token and bill the Console org.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if self._token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self._token
        env.setdefault("DISABLE_AUTOUPDATER", "1")
        return env

    def build_args(self, *, system: str, schema: dict[str, Any], with_images: bool) -> list[str]:
        return [
            *self.cmd,
            "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(schema, ensure_ascii=False),
            "--model", self.model,
            "--system-prompt", system,
            "--tools", "Read" if with_images else "",
            "--permission-prompts", "none",
            "--no-session-persistence",
            "--setting-sources", "project",
            "--strict-mcp-config",
        ]

    async def complete(self, *, system: str, prompt: str, schema: dict[str, Any],
                       images: list[Path] | None = None) -> LLMResult:
        from app.llm import usage

        started = time.monotonic()
        try:
            res = await self._complete(system=system, prompt=prompt, schema=schema, images=images)
        except LLMError as e:
            usage.record(self.purpose, self.model, None, ok=False, error=str(e),
                         duration_ms=int((time.monotonic() - started) * 1000))
            raise
        usage.record(self.purpose, res.model, res.raw, ok=True, duration_ms=res.duration_ms)
        return res

    async def _complete(self, *, system: str, prompt: str, schema: dict[str, Any],
                        images: list[Path] | None = None) -> LLMResult:
        images = images or []
        with tempfile.TemporaryDirectory(prefix="wh-llm-") as tmp:
            names = []
            for i, src in enumerate(images, 1):
                dst = Path(tmp) / f"foto{i}{src.suffix.lower() or '.jpg'}"
                shutil.copyfile(src, dst)
                names.append(dst.name)
            if names:
                prompt += (
                    "\n\nFotos des Artikels liegen im Arbeitsverzeichnis. Sieh dir jedes mit dem "
                    "Read-Tool an, bevor du antwortest: " + ", ".join(f"./{n}" for n in names)
                )
            args = self.build_args(system=system, schema=schema, with_images=bool(names))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, cwd=tmp, env=self._env(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as e:
                raise LLMError(t("claude CLI not found ({cmd}). Check CLAUDE_BIN.", cmd=self.cmd[0])) from e
            try:
                out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), self.timeout_s)
            except asyncio.TimeoutError as e:
                proc.kill()
                raise LLMError(t("claude CLI timed out after {n} s", n=self.timeout_s)) from e
        return self.parse_output(out, err, proc.returncode)

    def parse_output(self, out: bytes, err: bytes, returncode: int | None) -> LLMResult:
        try:
            data = json.loads(out.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            tail = (err or out).decode("utf-8", "replace")[-400:]
            raise LLMError(t("claude CLI returned no JSON (exit {code}): {output}", code=returncode, output=tail)) from e
        if data.get("is_error"):
            raise LLMError(t("claude CLI error: {error}", error=data.get("result") or data.get("subtype")))
        structured = data.get("structured_output")
        if structured is None and isinstance(data.get("result"), str):
            try:
                structured = json.loads(data["result"])
            except json.JSONDecodeError:
                structured = None
        if not isinstance(structured, dict):
            raise LLMError(t("claude CLI returned no structured result"))
        models = list((data.get("modelUsage") or {}).keys())
        return LLMResult(
            data=structured,
            model=models[0] if models else self.model,
            duration_ms=data.get("duration_ms"),
            cost_usd=data.get("total_cost_usd"),
            raw={k: data.get(k) for k in ("session_id", "num_turns", "duration_ms", "usage", "modelUsage", "total_cost_usd")},
        )
