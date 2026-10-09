"""Search agents, screening, price checks, scam check and the ad viewer stay anonymous: never the account session."""
import ast
from pathlib import Path

import httpx

from app.willhaben import public as public_mod
from app.willhaben.parse import _price_from_display

APP = Path(__file__).resolve().parent.parent / "app"
ANONYMOUS_MODULES = ["services/search_agents.py", "services/screening.py", "services/deals.py", "services/scam.py",
                     "services/geizhals.py", "willhaben/public.py"]
ACCOUNT = {"app.willhaben.session", "app.willhaben.account", "app.willhaben.insert", "app.willhaben.sales"}


def test_anonymous_modules_never_import_the_account_session():
    for rel in ANONYMOUS_MODULES:
        p = APP / rel
        if not p.exists():
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            assert not (set(names) & ACCOUNT), f"{rel} imports the logged-in session ({names})"
            if isinstance(node, ast.Name):
                assert node.id not in ("get_session", "AccountClient"), f"{rel} uses {node.id}"


async def test_public_client_has_own_jar_and_starts_fresh(monkeypatch):
    seen, status = [], {"next": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        code, status["next"] = status["next"], 200
        return httpx.Response(code, json={}, headers={"set-cookie": f"bbxid=visitor{len(seen)}; Path=/"})

    c = public_mod.PublicClient(transport=httpx.MockTransport(handler))
    c.limiter = public_mod.RateLimiter(0)                   # no waiting / cool-down in the test
    monkeypatch.setattr(c.limiter, "penalize", lambda: 0.0)
    await c._get("https://www.willhaben.at/webapi/a", json_api=True)
    await c._get("https://www.willhaben.at/webapi/b", json_api=True)
    assert seen == [None, "bbxid=visitor1"]                 # an anonymous visitor, never account cookies

    c._jar_since -= public_mod.ANON_SESSION_S + 1           # after 6 h: a new visitor
    await c._get("https://www.willhaben.at/webapi/c", json_api=True)
    assert seen[-1] is None

    status["next"] = 429                                   # blocked -> forget that visitor
    try:
        await c._get("https://www.willhaben.at/webapi/d", json_api=True)
    except public_mod.WillhabenError:
        pass
    assert not c._client.cookies
    await c.aclose()


def test_price_from_display():
    assert _price_from_display("€ 2.400") == 2400
    assert _price_from_display("€ 1.234,50") == 1234.5
    assert _price_from_display("VB") is None and _price_from_display(None) is None
