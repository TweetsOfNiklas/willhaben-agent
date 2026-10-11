"""A fake willhaben for tests, served from exported recordings (`python -m app.willhaben.export --out <dir>`).

    cassette = Cassette.load(FIXTURES / "rec")
    public_mod.set_public(PublicClient(transport=transport_from_cassette(cassette)))   # anonymous requests
    monkeypatch.setattr(chats, "_account", lambda: AccountClient(FakeSession(cassette)))   # the logged-in account

Requests are matched exactly like offline replay does (app/willhaben/recorder.canonical_key: method, URL without
volatile parameters, body without request ids). Unmatched requests answer 599 / raise and are listed in
`cassette.misses`, so a test can assert it used nothing unexpected.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.willhaben.recorder import canonical_key
from app.willhaben.session import ORIGIN, ApiResponse, NotLoggedIn, SessionUnavailable, parse_me

ME_PATH = "/webapi/userprofile-service/userprofile/me"


@dataclass
class Cassette:
    entries: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    misses: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, folder: Path) -> Cassette:
        c = cls()
        for f in sorted(Path(folder).rglob("*.json")):
            e = json.loads(f.read_text(encoding="utf-8"))
            body = e["request"]["body"]
            upload = (str(body.get("field")), str(body.get("mime"))) if e["method"] == "UPLOAD" and isinstance(body, dict) else None
            key = canonical_key(e["method"], e["url"], body, upload=upload)
            old = c.entries.get((e["method"], key))
            if old is None or e.get("recorded_at", "") >= old.get("recorded_at", ""):
                c.entries[(e["method"], key)] = e
        return c

    def find(self, method: str, url: str, body: Any = None, *, upload: tuple[str, str] | None = None) -> dict[str, Any] | None:
        e = self.entries.get((method.upper(), canonical_key(method, url, body, upload=upload)))
        if e is None:
            self.misses.append(f"{method.upper()} {url}")
        return e


def _text(body: Any) -> str:
    if body is None:
        return ""
    return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)


def transport_from_cassette(cassette: Cassette | Path) -> httpx.MockTransport:
    c = cassette if isinstance(cassette, Cassette) else Cassette.load(cassette)

    def handler(request: httpx.Request) -> httpx.Response:
        e = c.find(request.method, str(request.url), request.content or None)
        if e is None:
            return httpx.Response(599, text=f"no fixture for {request.method} {request.url}")
        headers = {k: v for k, v in e["response"]["headers"].items() if k.lower() != "content-length"}
        return httpx.Response(e["status"] or 200, headers=headers, text=_text(e["response"]["body"]))

    return httpx.MockTransport(handler)


class FakeSession:
    """Stands in for BrowserSession: api / api_json / upload / ws_request / ws_send from the cassette."""

    def __init__(self, cassette: Cassette | Path) -> None:
        self.cassette = cassette if isinstance(cassette, Cassette) else Cassette.load(cassette)
        self.calls: list[tuple[str, str, Any]] = []
        self.started = True
        self.logged_in = True
        self.user: dict[str, Any] | None = None

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else ORIGIN + path

    async def api(self, method: str, path: str, body: Any = None, accept: str = "application/json") -> ApiResponse:
        self.calls.append((method, path, body))
        url = self._url(path)
        e = self.cassette.find(method, url, json.dumps(body) if body is not None else None)
        if e is None:
            raise SessionUnavailable(f"no fixture for {method} {path}")
        return ApiResponse(status=e["status"] or 200, text=_text(e["response"]["body"]), url=e.get("final_url") or url)

    async def api_json(self, method: str, path: str, body: Any = None) -> Any:
        r = await self.api(method, path, body)
        if r.status in (401, 403):
            self.logged_in = False
            raise NotLoggedIn(f"{path}: HTTP {r.status}")
        if not r.ok:
            raise RuntimeError(f"{method} {path}: HTTP {r.status} {r.text[:200]}")
        return r.json()

    async def upload(self, path: str, field: str, filename: str, data: bytes, mime: str) -> ApiResponse:
        self.calls.append(("UPLOAD", path, filename))
        url = self._url(path)
        e = self.cassette.find("UPLOAD", url, upload=(field, mime))
        if e is None:
            raise SessionUnavailable(f"no fixture for UPLOAD {path}")
        return ApiResponse(status=e["status"] or 200, text=_text(e["response"]["body"]), url=url)

    async def ws_request(self, url: str, frame: dict[str, Any], request_id: str | None, *, req_type: str,
                         timeout_ms: int = 20000) -> dict[str, Any]:
        self.calls.append(("WS", url, frame))
        e = self.cassette.find("WS", url, frame)
        return e["response"]["body"] if e else {"status": "ws_error"}

    async def ws_send(self, url: str, frames: list[dict[str, Any]], settle_ms: int = 1500) -> dict[str, Any]:
        self.calls.append(("WS", url, frames))
        e = self.cassette.find("WS", url, frames)
        return e["response"]["body"] if e else {"status": "ws_error"}

    async def refresh_sso(self) -> bool:
        return False

    async def check_login(self) -> bool:
        try:
            self.user = parse_me(await self.api_json("GET", ME_PATH))
        except (SessionUnavailable, NotLoggedIn):
            self.user = None
        self.logged_in = self.user is not None
        return self.logged_in
