"""Logged-in willhaben access through a persistent Playwright browser profile.

Why a real browser: login is Keycloak SSO with FingerprintJS, and cookie replays from
other HTTP clients were invalidated after ~40 minutes. All account API calls therefore
run as `fetch()` inside a willhaben.at page of the same browser profile, so cookies,
TLS fingerprint and headers are exactly those of the browser the user logged in with.

Two pages are kept open:
  ui_page  - visible in the noVNC window; used for login and for the request recorder. Parked on about:blank
             after UI_IDLE_S without use (park_idle_ui): a willhaben page keeps rendering ads and animations on
             the virtual display, which was nearly all of the app's idle CPU.
  api_page - parked on https://www.willhaben.at/robots.txt (same origin, tiny) for fetch()
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import utcnow
from app.i18n import t
from app.willhaben import access_log, recorder, replay
from app.willhaben.public import WEB_CLIENT, Blocked, willhaben_limiter

log = logging.getLogger(__name__)

ORIGIN = "https://www.willhaben.at"
PARK_URL = f"{ORIGIN}/robots.txt"
LOGIN_URL = f"{ORIGIN}/iad/myprofile/login?r=/iad/myprofile/myadverts"
UI_IDLE_S = 15 * 60       # the visible page goes to about:blank after this long without use
VNC_PORT = 5900           # x11vnc; websockify (noVNC) holds a connection to it while a viewer is open
ME_PATH = "/webapi/userprofile-service/userprofile/me"
# Special keys the remote view may press (printable characters go through keyboard.type).
_REMOTE_KEYS = {"Enter", "Tab", "Shift+Tab", "Backspace", "Delete", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp",
                "ArrowDown", "Home", "End", "PageUp", "PageDown", "Control+a", "Control+z", "Space"}
_CAPTURE_SKIP = ("/_next/static", "/cdn-cgi/", "/logevent", "advertisingmetrics", "/webapi/dac/", "/bbx-search/_next")


def _other_site(url: str) -> bool:
    """A request to a site whose account lives in this browser too (app/services/site_accounts.py)."""
    from urllib.parse import urlsplit

    from app.services.site_accounts import SITES

    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for s in SITES.values() for h in s.hosts)


class NotLoggedIn(RuntimeError):
    pass


class SessionUnavailable(RuntimeError):
    pass


@dataclass
class ApiResponse:
    status: int
    text: str
    url: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.text) if self.text else None


class BrowserSession:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.limiter = willhaben_limiter()   # shared with the anonymous client: same IP
        self._pw = None
        self.context = None
        self.ui_page = None
        self.api_page = None
        self.logged_in: bool | None = None
        self.user: dict[str, Any] | None = None
        self.last_check: datetime | None = None
        self.error: str | None = None
        self._capture: dict[str, Any] | None = None
        self._capture_tasks: set[asyncio.Task] = set()
        self._input_lock = asyncio.Lock()
        self._ui_used_at = time.monotonic()   # last use of the visible page (see park_idle_ui)
        self._ui_holds = 0
        # live chat: own page holding the chat WebSocket (see start_chat_listener)
        self.chat_page = None
        self._creating_internal = False
        self._chat_cb = None
        self.chat_live: dict[str, Any] = {"state": "off", "since": None, "last_frame": None, "events": 0}

    # ---------------------------------------------------------------- lifecycle
    @property
    def started(self) -> bool:
        return self.context is not None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._pw = await async_playwright().start()
            self.context = await self._pw.chromium.launch_persistent_context(
                str(self.settings.profile_dir),
                headless=self.settings.browser_headless,
                locale="de-AT",
                timezone_id="Europe/Vienna",
                viewport={"width": 1280, "height": 860},
                args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            )
            self.ui_page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.api_page = await self.context.new_page()
            self.context.on("page", self._on_new_page)
            await self._goto(self.api_page, PARK_URL, wait_until="domcontentloaded")
            if self.ui_page.url in ("about:blank", ""):
                await self._goto(self.ui_page, ORIGIN + "/", wait_until="domcontentloaded")
            self.error = None
        except Exception as e:  # noqa: BLE001 - surface any launch problem in the dashboard
            self.error = t("Browser start failed: {error}", error=e)
            log.exception("browser start failed")
            await self.stop()

    async def stop(self) -> None:
        try:
            if self.context:
                await self.context.close()
        finally:
            self.context = self.ui_page = self.api_page = None
            if self._pw:
                await self._pw.stop()
                self._pw = None

    def _require(self) -> None:
        if not self.context or not self.api_page:
            raise SessionUnavailable(self.error or t("Browser session is not running"))

    # ---------------------------------------------------------------- API calls
    @staticmethod
    async def _goto(page, url: str, kind: str = "navigation", **kw: Any) -> Any:
        """page.goto() of a willhaben page, counted in the access log (channel account) and recorded (no body)."""
        started = time.monotonic()
        with access_log.track("account", kind) as tr:
            try:
                res = await page.goto(url, **kw)
            except Exception as e:
                recorder.record_http("navigation", kind, "GET", url, outcome="error", error=str(e),
                                     duration_ms=int((time.monotonic() - started) * 1000))
                raise
            tr.status = getattr(res, "status", None)
        recorder.record_http("navigation", kind, "GET", url, status=tr.status, final_url=getattr(res, "url", None),
                             duration_ms=int((time.monotonic() - started) * 1000), store_response=False)
        return res

    async def _csrf(self) -> str | None:
        for c in await self.context.cookies(ORIGIN):
            if c["name"] == "x-bbx-csrf-token":
                return c["value"]
        return None

    async def _ensure_parked(self) -> None:
        if not self.api_page.url.startswith(ORIGIN):
            await self._goto(self.api_page, PARK_URL, wait_until="domcontentloaded")

    async def api(self, method: str, path: str, body: Any = None, accept: str = "application/json") -> ApiResponse:
        """Run fetch() for an `/webapi/...` path (or a page, with accept=text/html) inside the logged-in browser."""
        url = path if path.startswith("http") else ORIGIN + path
        payload = json.dumps(body) if body is not None else None
        if replay.enabled() and (rec := replay.find(method, url, payload)):    # WH_REPLAY=1
            replay.note(rec, access_log.classify(url))
            return ApiResponse(*replay.as_api_response(rec))
        self._require()
        headers = {"accept": accept, "x-wh-client": WEB_CLIENT}
        csrf = await self._csrf()
        if csrf:
            headers["x-bbx-csrf-token"] = csrf
        if body is not None:
            headers["content-type"] = "application/json"
        kind = access_log.classify(url)
        async with self.limiter:
            await self._ensure_parked()
            with access_log.track("account", kind) as tr:
                started = time.monotonic()
                try:
                    res = await self.api_page.evaluate(
                        """async ({method, url, headers, payload}) => {
                            const r = await fetch(url, {method, headers, body: payload, credentials: 'include'});
                            return {status: r.status, text: await r.text(), url: r.url,
                                    headers: Object.fromEntries(r.headers.entries())};
                        }""",
                        {"method": method, "url": url, "headers": headers, "payload": payload},
                    )
                except Exception as e:
                    recorder.record_http("account", kind, method, url, req_headers=headers, req_body=payload, outcome="error",
                                         error=str(e), duration_ms=int((time.monotonic() - started) * 1000))
                    raise
                tr.status = res["status"]
        recorder.record_http("account", kind, method, url, req_headers=headers, req_body=payload, status=res["status"],
                             resp_headers=res.get("headers"), resp_body=res.get("text"), final_url=res.get("url"),
                             duration_ms=int((time.monotonic() - started) * 1000))
        if res["status"] == 429:
            raise Blocked(t("willhaben responded {status}; pausing {s}s", status=429, s=f"{self.limiter.penalize():.0f}"))
        return ApiResponse(status=res["status"], text=res["text"], url=res["url"])

    async def upload(self, path: str, field: str, filename: str, data: bytes, mime: str) -> ApiResponse:
        """multipart/form-data POST of one file from the logged-in page (e.g. ad photos)."""
        import base64

        url = path if path.startswith("http") else ORIGIN + path
        if replay.enabled() and (rec := replay.find("UPLOAD", url, upload=(field, mime))):
            replay.note(rec, "upload")
            return ApiResponse(*replay.as_api_response(rec))
        self._require()
        headers = {"accept": "application/json", "x-wh-client": WEB_CLIENT}
        csrf = await self._csrf()
        if csrf:
            headers["x-bbx-csrf-token"] = csrf
        async with self.limiter:
            await self._ensure_parked()
            with access_log.track("account", "upload") as tr:
                started = time.monotonic()
                try:
                    res = await self.api_page.evaluate(
                        """async ({url, headers, field, filename, b64, mime}) => {
                            const bin = atob(b64), bytes = new Uint8Array(bin.length);
                            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                            const fd = new FormData();
                            fd.append(field, new Blob([bytes], {type: mime}), filename);
                            const r = await fetch(url, {method: 'POST', headers, body: fd, credentials: 'include'});
                            return {status: r.status, text: await r.text(), url: r.url,
                                    headers: Object.fromEntries(r.headers.entries())};
                        }""",
                        {"url": url, "headers": headers, "field": field, "filename": filename,
                         "b64": base64.b64encode(data).decode(), "mime": mime},
                    )
                except Exception as e:
                    recorder.record_http("account", "upload", "UPLOAD", url, req_headers=headers, outcome="error",
                                         error=str(e), upload=(field, mime),
                                         duration_ms=int((time.monotonic() - started) * 1000))
                    raise
                tr.status = res["status"]
        # the file itself is not stored (photos: size + hash only), it is matched by field and type
        recorder.record_http("account", "upload", "UPLOAD", url, req_headers=headers,
                             req_body={"field": field, "filename": filename, "mime": mime, "bytes": len(data)},
                             status=res["status"], resp_headers=res.get("headers"), resp_body=res.get("text"),
                             final_url=res.get("url"), upload=(field, mime),
                             duration_ms=int((time.monotonic() - started) * 1000))
        return ApiResponse(status=res["status"], text=res["text"], url=res["url"])

    async def ws_send(self, url: str, frames: list[dict[str, Any]], settle_ms: int = 1500) -> dict[str, Any]:
        """Open a WebSocket from the logged-in page, send frames that get no answer (e.g. read receipts), close.

        Frames go out with small random gaps, roughly like messages scrolling into view one after another.
        """
        if replay.enabled() and (rec := replay.find("WS", url, frames)):
            replay.note(rec, "chat")
            return replay.as_ws_result(rec)
        self._require()
        async with self.limiter:
            await self._ensure_parked()
            with access_log.track("chat", "chat") as tr:
                started = time.monotonic()
                res = await self.api_page.evaluate(
                    """({url, frames, settle}) => new Promise((resolve) => {
                        let sent = 0, done = false;
                        const ws = new WebSocket(url);
                        const finish = (status) => { if (done) return; done = true; clearTimeout(t);
                                                     try { ws.close(); } catch (e) {} resolve({status, sent}); };
                        const t = setTimeout(() => finish('timeout'), 15000 + frames.length * 1000);
                        ws.onopen = async () => {
                            for (const f of frames) {
                                ws.send(JSON.stringify(f)); sent++;
                                await new Promise(r => setTimeout(r, 250 + Math.random() * 650));
                            }
                            setTimeout(() => finish('ok'), settle);
                        };
                        ws.onerror = () => finish('ws_error');
                        ws.onclose = () => finish(sent === frames.length ? 'ok' : 'closed');
                    })""",
                    {"url": url, "frames": frames, "settle": settle_ms},
                )
                tr.outcome = access_log.ws_outcome(res.get("status") if isinstance(res, dict) else None)
        recorder.record_ws("chat", url, frames, res, outcome=tr.outcome, duration_ms=int((time.monotonic() - started) * 1000))
        return res

    async def ws_request(self, url: str, frame: dict[str, Any], request_id: str | None, *, req_type: str,
                         timeout_ms: int = 20000) -> dict[str, Any]:
        """Open a WebSocket from the logged-in page, send one frame, return the server's answer to it.

        The browser attaches the willhaben cookies itself (same site), exactly like the chat web app.
        Resolves with the first frame carrying our request_id and a final status (success/error),
        or {"status": "timeout"|"ws_error"|"closed"} otherwise.
        """
        if replay.enabled() and (rec := replay.find("WS", url, frame)):
            replay.note(rec, "send" if req_type == "send_message" else "chat")
            return replay.as_ws_result(rec)
        self._require()
        async with self.limiter:
            await self._ensure_parked()
            with access_log.track("chat", "send" if req_type == "send_message" else "chat") as tr:
                started = time.monotonic()
                res = await self.api_page.evaluate(
                    """({url, frame, rid, reqType, timeout}) => new Promise((resolve) => {
                        let done = false, acked = false;
                        const ws = new WebSocket(url);
                        const finish = (r) => { if (done) return; done = true; clearTimeout(t);
                                                try { ws.close(); } catch (e) {} resolve({...r, acked}); };
                        const t = setTimeout(() => finish({status: 'timeout'}), timeout);
                        ws.onopen = () => ws.send(JSON.stringify(frame));
                        ws.onmessage = (e) => {
                            let m; try { m = JSON.parse(e.data); } catch (_) { return; }
                            if ((rid !== null && m.request_id !== rid) || m.req_type !== reqType) return;
                            if (m.status === 'ack') { acked = true; return; }
                            finish(m);
                        };
                        ws.onerror = () => finish({status: 'ws_error'});
                        ws.onclose = (e) => finish({status: 'closed', code: e.code});
                    })""",
                    {"url": url, "frame": frame, "rid": request_id, "reqType": req_type, "timeout": timeout_ms},
                )
                status = res.get("status") if isinstance(res, dict) else None
                # acked but no final frame: the caller treats it as a likely success (checks the history)
                tr.outcome = "ok" if isinstance(res, dict) and res.get("acked") and status in ("timeout", "closed") \
                    else access_log.ws_outcome(status)
        recorder.record_ws("send" if req_type == "send_message" else "chat", url, frame, res, outcome=tr.outcome,
                           duration_ms=int((time.monotonic() - started) * 1000))
        return res

    async def api_json(self, method: str, path: str, body: Any = None) -> Any:
        r = await self.api(method, path, body)
        if r.status in (401, 403):
            if await self.refresh_sso():
                r = await self.api(method, path, body)
        if r.status in (401, 403):
            self.logged_in = False
            raise NotLoggedIn(f"{path}: HTTP {r.status}")
        if not r.ok:
            raise RuntimeError(f"{method} {path}: HTTP {r.status} {r.text[:200]}")
        try:
            return r.json()
        except ValueError:      # e.g. a redirect to one of willhaben's pages: name it instead of a JSON parse error
            ended = f", {t('ended at')} {r.url}" if r.url and not r.url.endswith(path) else ""
            raise RuntimeError(t("{method} {path}: willhaben answered with a web page instead of data (HTTP {status}{ended})",
                                 method=method, path=path, status=r.status, ended=ended)) from None

    # ---------------------------------------------------------------- login state
    async def _me(self) -> dict[str, Any] | None:
        """Profile of the logged-in user, or None. Anonymous requests get HTTP 204 with no body."""
        r = await self.api("GET", ME_PATH)
        if r.status != 200 or not r.text:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        return parse_me(data)

    async def check_login(self) -> bool:
        if not self.started:
            self.logged_in = None
            return False
        data = await self._me()
        # Web session gone but maybe the (longer-lived) SSO session is not: try a silent re-login,
        # except when we already know we are logged out.
        if data is None and self.logged_in is not False and await self.refresh_sso():
            data = await self._me()
        self.last_check = utcnow()
        if data:
            self.user = data
            self.logged_in = True
        else:
            self.user = None
            self.logged_in = False
        return bool(self.logged_in)

    async def refresh_sso(self) -> bool:
        """Re-establish the web session from a still-valid Keycloak SSO session (no password).

        The web app itself does this via an iframe on /webapi/sso/login?background-login.
        """
        self._require()
        try:
            await self._goto(self.api_page, f"{ORIGIN}/webapi/sso/login?background-login", "login", wait_until="load", timeout=20000)
            await self.api_page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001 - best effort
            pass
        finally:
            await self._goto(self.api_page, PARK_URL, wait_until="domcontentloaded")
        return await self._me() is not None

    # ---------------------------------------------------------------- idle: park the visible page
    def touch_ui(self) -> None:
        self._ui_used_at = time.monotonic()

    @contextmanager
    def ui_in_use(self):
        """A flow drives the visible page (e.g. PayLivery waiting for the bank): never park it meanwhile."""
        self._ui_holds += 1
        try:
            yield
        finally:
            self._ui_holds -= 1
            self.touch_ui()

    async def park_idle_ui(self, idle_s: float = UI_IDLE_S) -> bool:
        """Visible page unused for idle_s: go to about:blank. True if it was parked."""
        page = self.ui_page
        if (page is None or page.is_closed() or self._ui_holds or self.capturing or page.url in ("about:blank", "")
                or time.monotonic() - self._ui_used_at < idle_s):
            return False
        if vnc_viewer_connected():
            self.touch_ui()      # someone watches in noVNC
            return False
        await page.goto("about:blank")
        log.info("visible browser page idle, parked on about:blank")
        return True

    async def open_login(self) -> None:
        self._require()
        self.touch_ui()
        await self.ui_page.bring_to_front()
        await self._goto(self.ui_page, LOGIN_URL, "login", wait_until="domcontentloaded")

    async def load_hidden(self, url: str, timeout_ms: int = 30000) -> str:
        """Open `url` in a short-lived tab of the logged-in browser (not the visible one) and return where it ended
        up after redirects and scripts (the other sites' login checks, app/services/site_accounts.py)."""
        self._require()
        ui = self.ui_page
        self._creating_internal = True
        try:
            page = await self.context.new_page()
        finally:
            self._creating_internal = False
            self.ui_page = ui
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)   # a script's redirect to the login
            except Exception:  # noqa: BLE001 - busy pages never get idle: what loaded counts
                pass
            return page.url
        finally:
            await page.close()

    async def open_url(self, url: str) -> None:
        self._require()
        self.touch_ui()
        await self.ui_page.bring_to_front()
        if url.startswith(ORIGIN):
            await self._goto(self.ui_page, url, wait_until="domcontentloaded")
        else:
            await self.ui_page.goto(url, wait_until="domcontentloaded")

    async def screenshot(self, fmt: str = "png") -> bytes:
        self._require()
        self.touch_ui()     # the dashboard's remote view polls this while it is open
        if fmt == "jpeg":
            return await self.ui_page.screenshot(type="jpeg", quality=70)
        return await self.ui_page.screenshot(type="png")

    # ---------------------------------------------------------------- remote control (dashboard)
    async def remote_input(self, ev: dict[str, Any]) -> None:
        """Replay one user input from the dashboard's remote view on the visible page.

        Coordinates are CSS pixels of the page viewport (= screenshot pixels). Inputs are
        serialized so fast typing arrives in order.
        """
        self._require()
        self.touch_ui()
        page = self.ui_page
        async with self._input_lock:
            kind = ev.get("type")
            if kind == "click":
                await page.mouse.click(float(ev["x"]), float(ev["y"]))
            elif kind == "text":
                await page.keyboard.type(str(ev["text"])[:2000])
            elif kind == "key":
                key = str(ev["key"])
                if key not in _REMOTE_KEYS:
                    raise ValueError(t("Key not allowed: {key}", key=key))
                await page.keyboard.press(key)
            elif kind == "scroll":
                await page.mouse.move(float(ev.get("x", 640)), float(ev.get("y", 430)))
                await page.mouse.wheel(0, max(-3000.0, min(3000.0, float(ev["dy"]))))
            elif kind in ("back", "reload"):
                started = time.monotonic()
                with access_log.track("account", "navigation") as tr:
                    res = await (page.go_back if kind == "back" else page.reload)(wait_until="domcontentloaded")
                    tr.status = getattr(res, "status", None)
                if res is not None:
                    recorder.record_http("navigation", "navigation", "GET", getattr(res, "url", "") or page.url,
                                         status=tr.status, store_response=False,
                                         duration_ms=int((time.monotonic() - started) * 1000))
            else:
                raise ValueError(t("unknown input: {kind}", kind=kind))

    def _on_new_page(self, page) -> None:
        # Popups (e.g. a login provider window) become the visible page so the remote view follows them.
        if page is self.api_page or self._creating_internal:
            return
        self.ui_page = page
        self.touch_ui()
        page.on("close", self._on_page_closed)

    # ---------------------------------------------------------------- live chat (no polling)
    _CHAT_JS = """(url) => {
        if (window.__whLive) return 'already';
        window.__whLive = true;
        let delay = 2000, ws = null, ping = null;
        const connect = () => {
            ws = new WebSocket(url);
            ws.onopen = () => { delay = 2000; window.__whChat(JSON.stringify({_state: 'open'}));
                                ping = setInterval(() => { try { ws.send(JSON.stringify({req_type: 'ping'})); } catch (e) {} }, 10000); };
            ws.onmessage = (e) => window.__whChat(typeof e.data === 'string' ? e.data : '{}');
            ws.onclose = (e) => { clearInterval(ping); window.__whChat(JSON.stringify({_state: 'closed', code: e.code}));
                                  setTimeout(connect, delay); delay = Math.min(delay * 2, 60000); };
            ws.onerror = () => {};
        };
        connect();
        return 'started';
    }"""

    async def start_chat_listener(self, on_event) -> None:
        """Keep the chat WebSocket open in a page of the logged-in browser (like the web app does).

        on_event(conversation_id, frame) is awaited for every pushed frame that names a conversation.
        Pings every 10 s keep the connection alive; the browser reconnects with backoff on its own.
        """
        self._require()
        self._chat_cb = on_event
        await self._open_chat_page()

    async def _open_chat_page(self) -> None:
        from app.willhaben.account import CHAT_WS

        if self.chat_page is not None and not self.chat_page.is_closed():
            await self.chat_page.close()
        ui = self.ui_page
        self._creating_internal = True
        try:
            self.chat_page = await self.context.new_page()
        finally:
            self._creating_internal = False
            self.ui_page = ui
        await self.chat_page.expose_function("__whChat", self._on_chat_frame)
        await self._goto(self.chat_page, PARK_URL, wait_until="domcontentloaded")
        await self.chat_page.evaluate(self._CHAT_JS, CHAT_WS)
        self.chat_live.update(state="connecting", since=utcnow())

    async def _on_chat_frame(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        now = utcnow()
        self.chat_live["last_frame"] = now
        if "_state" in msg:
            state = "connected" if msg["_state"] == "open" else f"getrennt ({msg.get('code')})"
            # access log: one count per (re)connect of the live socket; a close without an open = failed attempt
            if state == "connected":
                access_log.record("chat", "chat", "ok")
            elif self.chat_live["state"] != "connected":
                access_log.record("chat", "chat", "error")
            if state != self.chat_live["state"]:
                log.info("live chat: %s", state)
                self.chat_live.update(state=state, since=now)
            return
        if msg.get("req_type") == "ping":
            return
        from app.willhaben.account import CHAT_WS
        recorder.record_push(CHAT_WS, msg)     # kept for the timeline and replay; pings and socket states are not
        payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
        cid = msg.get("conversation_id") or payload.get("conversation_id")
        if cid and self._chat_cb:
            self.chat_live["events"] += 1
            try:
                await self._chat_cb(str(cid), msg)
            except Exception:  # noqa: BLE001 - never break the page's callback
                log.exception("live chat callback failed")

    def chat_live_ok(self) -> bool:
        last = self.chat_live.get("last_frame")
        return self.chat_live["state"] == "connected" and last is not None and (utcnow() - last).total_seconds() < 60

    async def chat_watchdog(self) -> None:
        """Re-open the chat page when it went silent (page crash, browser restart, long network outage)."""
        while True:
            await asyncio.sleep(60)
            if not self.started or self._chat_cb is None:
                continue
            last = self.chat_live.get("last_frame") or self.chat_live.get("since")
            if last is None or (utcnow() - last).total_seconds() > 90:
                log.info("live chat silent, reopening")
                try:
                    await self._open_chat_page()
                except Exception as e:  # noqa: BLE001
                    self.chat_live.update(state=t("Error: {error}", error=e), since=utcnow())

    def _on_page_closed(self, page) -> None:
        if page is self.ui_page and self.context:
            rest = [p for p in self.context.pages
                    if p is not self.api_page and p is not self.chat_page and not p.is_closed()]
            if rest:
                self.ui_page = rest[-1]

    # ---------------------------------------------------------------- request recorder
    @property
    def capturing(self) -> bool:
        return self._capture is not None

    def start_capture(self, label: str) -> None:
        self._require()
        if self._capture:
            return
        self._capture = {"label": label, "started_at": utcnow().isoformat(), "entries": []}
        self.context.on("requestfinished", self._on_request_finished)

    def _on_request_finished(self, request) -> None:
        task = asyncio.ensure_future(self._record(request))
        self._capture_tasks.add(task)
        task.add_done_callback(self._capture_tasks.discard)

    async def _record(self, request) -> None:
        cap = self._capture
        url = request.url
        if cap is None or any(s in url for s in _CAPTURE_SKIP):
            return
        other = _other_site(url)          # Kleinanzeigen, Vinted: kept in the capture file only (their messages)
        if "willhaben" not in url and not other:
            return
        if request.resource_type not in ("fetch", "xhr", "document", "websocket", "eventsource"):
            return
        try:
            resp = await request.response()
            body = raw = None
            if resp is not None:
                try:
                    raw = await resp.body()
                    # Pages carry their data in __NEXT_DATA__ at the very end, so keep them whole.
                    limit = 3_000_000 if request.resource_type == "document" else 200_000
                    body = raw[:limit].decode("utf-8", "replace")
                except Exception:  # noqa: BLE001 - redirects have no body
                    body = raw = None
            # what willhaben's own web app sent (PayLivery steps): also a replayable recording
            if not other:
                recorder.record_http("browser", access_log.classify(url), request.method, url,
                                     req_headers=await request.all_headers(), req_body=request.post_data,
                                     status=resp.status if resp else None,
                                     resp_headers=await resp.all_headers() if resp else None, resp_body=raw,
                                     outcome=None if resp else "error")
            cap["entries"].append({
                "ts": utcnow().isoformat(),
                "method": request.method,
                "url": url,
                "type": request.resource_type,
                "request_headers": _redact(await request.all_headers()),
                "post_data": (request.post_data or "")[:50_000] or None,
                "status": resp.status if resp else None,
                "response_headers": _redact(await resp.all_headers()) if resp else None,
                "response_body": body,
            })
        except Exception as e:  # noqa: BLE001 - recorder must never break the page
            log.debug("capture failed for %s: %s", url, e)

    async def stop_capture(self) -> Path | None:
        if not self._capture:
            return None
        self.context.remove_listener("requestfinished", self._on_request_finished)
        if self._capture_tasks:
            await asyncio.gather(*self._capture_tasks, return_exceptions=True)
        cap, self._capture = self._capture, None
        cap["stopped_at"] = utcnow().isoformat()
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in cap["label"])[:40] or "capture"
        path = self.settings.captures_dir / f"{utcnow():%Y%m%d-%H%M%S}-{safe}.json"
        path.write_text(json.dumps(cap, ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    def status(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "logged_in": self.logged_in,
            "user": self.user,
            "last_check": self.last_check,
            "error": self.error,
            "capturing": self.capturing,
            "capture_count": len(self._capture["entries"]) if self._capture else 0,
            "chat_live": dict(self.chat_live),
        }


def vnc_viewer_connected() -> bool:
    """An established TCP connection to the VNC port (Linux /proc): a noVNC viewer is open."""
    port = f":{VNC_PORT:04X}"
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) > 3 and f[3] == "01" and f[1].endswith(port):   # state 01 = ESTABLISHED, local port
                return True
    return False


def parse_me(data: Any) -> dict[str, Any] | None:
    """Normalise the `/userprofile/me` response to {id, uuid, nickname, email}, or None if anonymous.

    The real response nests the profile under `userData` (`loginId`, `uuid`, `firstName`, `emailAddress`);
    the flat keys are kept as a fallback in case the shape changes.
    """
    if not isinstance(data, dict):
        return None
    u = data.get("userData") if isinstance(data.get("userData"), dict) else data
    user = {
        "id": u.get("loginId") or u.get("id") or u.get("userId"),
        "uuid": u.get("uuid"),
        "nickname": u.get("nickname") or u.get("displayName") or u.get("firstName"),
        "email": u.get("emailAddress") or u.get("email"),
    }
    if not any(user[k] for k in ("id", "uuid", "email")):
        return None
    if user["id"] is not None:
        user["id"] = str(user["id"])
    return user


_redact = recorder.redact_headers      # cookies, CSRF token and authorization never leave the browser


class ReplaySession(BrowserSession):
    """WH_REPLAY=1 with BROWSER_ENABLED=0: the account without a browser, answered from the recordings only
    (app/willhaben/replay.py). A request that was never recorded fails with SessionUnavailable."""

    @property
    def started(self) -> bool:
        return True

    async def start(self) -> None:
        self.error = None

    async def stop(self) -> None:
        pass

    def _require(self) -> None:
        raise SessionUnavailable(t("Offline replay: this willhaben request was never recorded (no browser)"))

    async def refresh_sso(self) -> bool:
        return False

    async def start_chat_listener(self, on_event) -> None:
        self.chat_live.update(state="replay", since=utcnow())

    async def chat_watchdog(self) -> None:
        return

    async def park_idle_ui(self, idle_s: float = UI_IDLE_S) -> bool:
        return False

    def status(self) -> dict[str, Any]:
        return {**super().status(), "replay": True}


_session: BrowserSession | None = None


def get_session() -> BrowserSession:
    global _session
    if _session is None:
        s = get_settings()
        _session = ReplaySession() if s.wh_replay and not s.browser_enabled else BrowserSession()
    return _session
