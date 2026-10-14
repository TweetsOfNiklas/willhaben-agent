"""The shared platform HTTP client: pacing, cache, blocks, network errors, recording per platform, transports."""
import asyncio

import httpx
import pytest
from sqlmodel import select

from app.db import WhRecording, session
from app.platforms import Blocked, PlatformError
from app.platforms.base import NotTheExit
from app.platforms.httpclient import (CurlTransport, ExitMoved, FetchResponse, HttpPlatformClient, HttpxTransport,
                                      Trial, TransportError, trial)
from app.platforms.ratelimit import RateLimiter
from app.services import access_stats, timeline
from app.willhaben import access_log, replay
from app.config import get_settings


class FakeTransport:
    def __init__(self, answers):
        self.answers, self.calls, self.cleared = list(answers), [], 0

    async def send(self, method, url, *, params=None, headers=None):
        self.calls.append((method, url, params, headers))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        status, body, *headers = a          # (status, body) answers JSON; a third item gives the headers
        return FetchResponse(status, headers[0] if headers else {"content-type": "application/json"}, body, url)

    def clear_cookies(self):
        self.cleared += 1

    async def aclose(self):
        pass


def _client(answers, **kw) -> tuple[HttpPlatformClient, FakeTransport]:
    tr = FakeTransport(answers)
    c = HttpPlatformClient("fake", RateLimiter(0, name="Fake"), name="Fake", transport=tr, **kw)
    return c, tr


async def test_get_counts_records_and_caches():
    timeline.set_recording(timeline=True, willhaben=True)
    c, tr = _client([(200, b'{"a": 1}'), (404, b"")])
    r = await c._get("https://fake.example/api?q=rad", params={"page": 1}, headers={"accept": "application/json"}, cache_s=60)
    assert r.status_code == 200 and r.status == 200 and r.json() == {"a": 1} and r.text == '{"a": 1}'
    again = await c._get("https://fake.example/api?q=rad", params={"page": 1}, headers={"accept": "application/json"}, cache_s=60)
    assert again is r and len(tr.calls) == 1                           # the second one came from the cache
    gone = await c._get("https://fake.example/ad/1", cache_s=60)
    assert gone.status_code == 404
    assert access_stats.last_hour(platform="fake") == 2 and access_stats.last_hour() == 0
    with session() as db:
        recs = list(db.exec(select(WhRecording).order_by(WhRecording.id)))
    assert [(x.platform, x.cached, x.status) for x in recs] == [("fake", False, 200), ("fake", True, 200), ("fake", False, 404)]
    assert recs[0].url == "https://fake.example/api?q=rad&page=1" == tr.calls[0][1]
    assert tr.calls[0][3] == {"accept": "application/json"}


async def test_blocks_errors_and_status_hook():
    c, tr = _client([(429, b""), TransportError("tls handshake failed"), (401, b"")])
    with pytest.raises(Blocked) as info:
        await c._get("https://fake.example/x")
    assert info.value.sent and tr.cleared == 1 and c.limiter.cooling_down_s > 0 and "Fake responded 429" in str(info.value)
    c.limiter = RateLimiter(0, name="Fake")
    with pytest.raises(PlatformError, match="tls handshake"):
        await c._get("https://fake.example/x")

    class Picky(HttpPlatformClient):
        def _check_status(self, r):
            if r.status_code == 401:
                raise PlatformError("credentials rotated")
            super()._check_status(r)

    picky = Picky("fake", RateLimiter(0, name="Fake"), transport=tr)
    with pytest.raises(PlatformError, match="credentials rotated"):
        await picky._get("https://fake.example/x")
    assert tr.cleared == 1                                                # a 401 is not a block: no new visitor


async def test_jar_rotation_and_classify():
    c, tr = _client([(200, b"{}"), (200, b"{}")])
    c.session_s = 0
    await c._get("https://fake.example/x")
    await c._get("https://fake.example/x")
    assert tr.cleared >= 1 and c.classify("https://www.willhaben.at/iad/object?adId=1") == "detail"


async def test_replay_answers_before_the_transport(monkeypatch):
    timeline.set_recording(timeline=True, willhaben=True)
    c, tr = _client([(200, b'{"live": 1}')])
    await c._get("https://fake.example/api?q=rad")
    monkeypatch.setattr(get_settings(), "wh_replay", True)
    r = await c._get("https://fake.example/api?q=rad&_=123")
    assert r.json() == {"live": 1} and r.extensions["wh_replayed"] and len(tr.calls) == 1
    assert replay.find("GET", "https://fake.example/api?q=rad").platform == "fake"


async def test_httpx_transport_and_curl_transport_build():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-test"))
        return httpx.Response(200, text="ok", headers={"set-cookie": "a=1"})

    tr = HttpxTransport({"x-test": "1"}, httpx.MockTransport(handler))
    r = await tr.send("GET", "https://fake.example/x", params={"a": "b"})
    assert r.status_code == 200 and r.text == "ok" and seen == ["1"] and tr.client.cookies
    tr.clear_cookies()
    assert not tr.client.cookies
    await tr.aclose()
    curl = CurlTransport({"x-test": "1"})
    curl.clear_cookies()
    await curl.aclose()


async def test_public_client_is_a_platform_client():
    from app.willhaben.public import PublicClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rowsFound": 0, "advertSummaryList": {}})

    c = PublicClient(transport=httpx.MockTransport(handler))
    c.limiter = RateLimiter(0)
    page = await c.search("https://www.willhaben.at/webapi/ad-search/search/atz/seo/x?keyword=rad")
    assert page.rows_found == 0 and c.platform == "willhaben" and c._client is not None
    assert access_log.pending() and access_stats.last_hour() == 1
    await c.aclose()


# ------------------------------------------------------------------ through the VPN (app/platforms/vpn.py)
class FakeTunnel:
    """Hands out routes http://exit1, http://exit2, ...; failed() moves on. `dead`: no exit works (fail closed)."""

    def __init__(self, dead=False):
        from app.platforms.vpn import Route

        self.Route, self.dead, self.n, self.route, self.failures, self.details = Route, dead, 0, None, [], []

    async def ensure(self):
        if self.dead:
            raise PlatformError("No working VPN exit for Fake")
        if self.route is None:
            self.n += 1
            self.route = self.Route(f"http://exit{self.n}", self.n)
        return self.route

    async def failed(self, route, reason, detail=""):
        self.failures.append((route.proxy, reason))
        self.details.append(detail)
        if self.route is not None and route.gen == self.route.gen:
            self.route = None
            return True
        return False

    def current(self):
        return self.route


def _vpn_client(answers_by_exit: dict, tunnel: FakeTunnel, cls=HttpPlatformClient, **kw):
    transports = {proxy: FakeTransport(answers) for proxy, answers in answers_by_exit.items()}
    built = []

    def factory(proxy):
        built.append(proxy)
        return transports[proxy]

    c = cls("fake", RateLimiter(0, name="Fake"), name="Fake", transport_factory=factory, tunnel=lambda: tunnel, **kw)
    return c, transports, built


async def test_a_refused_exit_moves_on_without_pausing_the_site():
    tunnel = FakeTunnel()
    c, transports, built = _vpn_client({"http://exit1": [(403, b"cloudflare")], "http://exit2": [(200, b"{}")]}, tunnel)
    before = access_stats.last_hour(platform="fake")
    r = await c._get("https://fake.example/api")
    assert r.status_code == 200 and c.proxy == "http://exit2"
    assert built == ["http://exit1", "http://exit2"] and tunnel.failures == [("http://exit1", "blocked")]
    assert tunnel.details == ["HTTP 403"]
    assert c.limiter.cooling_down_s == 0          # the shared limiter (logged-in session too) is not paused
    assert access_stats.last_hour(platform="fake") - before == 2        # both requests count, the 403 as blocked


async def test_three_refusals_give_up_for_this_request():
    tunnel = FakeTunnel()
    c, _, built = _vpn_client({f"http://exit{i}": [(429 if i == 2 else 403, b"")] for i in (1, 2, 3)}, tunnel)
    with pytest.raises(Blocked) as info:
        await c._get("https://fake.example/api")
    assert info.value.sent and "refused the VPN exit" in str(info.value) and len(built) == 3
    assert [f[1] for f in tunnel.failures] == ["blocked", "rate", "blocked"] and c.limiter.cooling_down_s == 0


async def test_no_exit_means_no_request_at_all():
    c, _, built = _vpn_client({}, FakeTunnel(dead=True))
    with pytest.raises(PlatformError, match="No working VPN exit"):
        await c._get("https://fake.example/api")
    assert built == []                            # never a direct transport (factory(None))


async def test_an_unreachable_exit_moves_on():
    tunnel = FakeTunnel()
    c, _, _ = _vpn_client({"http://exit1": [TransportError("proxy connect refused")], "http://exit2": [(200, b"{}")]}, tunnel)
    assert (await c._get("https://fake.example/api")).status_code == 200
    assert tunnel.failures == [("http://exit1", "unreachable")]


async def test_cached_answers_do_not_touch_the_tunnel():
    tunnel = FakeTunnel()
    c, transports, _ = _vpn_client({"http://exit1": [(200, b"{}")]}, tunnel)
    await c._get("https://fake.example/api", cache_s=60)
    tunnel.dead = True
    assert (await c._get("https://fake.example/api", cache_s=60)).status_code == 200


async def test_the_vpn_follows_the_switch_without_a_new_client(monkeypatch):
    from app.platforms import vpn

    tunnel = FakeTunnel()
    monkeypatch.setattr(vpn, "tunnel_for", lambda pid: tunnel if vpn.enabled(pid) else None)
    transports = {None: FakeTransport([(200, b"{}")]), "http://exit1": FakeTransport([(200, b"{}")])}
    c = HttpPlatformClient("fake", RateLimiter(0, name="Fake"), transport_factory=lambda p: transports[p])
    await c._get("https://fake.example/a")
    assert c.proxy is None and len(transports[None].calls) == 1
    vpn.set_enabled("fake", True)
    await c._get("https://fake.example/b")
    assert c.proxy == "http://exit1" and len(transports["http://exit1"].calls) == 1


async def test_kleinanzeigen_tells_an_exit_block_from_rotated_credentials():
    from app.platforms.kleinanzeigen import KleinanzeigenClient

    ka = KleinanzeigenClient(transport=FakeTransport([]))
    html = FetchResponse(403, {"Content-Type": "text/html; charset=utf-8", "Server": "AkamaiGHost"}, b"<html>", "u")
    creds = FetchResponse(403, {"content-type": "application/json"}, b"{}", "u")
    assert ka._exit_blocked(html) and not ka._exit_blocked(creds)
    assert ka._exit_blocked(FetchResponse(429, {}, b"", "u")) and not ka._exit_blocked(FetchResponse(401, {}, b"", "u"))

    tunnel = FakeTunnel()
    ka._factory = lambda proxy: FakeTransport([(403, b'{"error": "unauthorized"}')])
    ka._tunnel, ka._transport = (lambda: tunnel), None
    with pytest.raises(PlatformError, match="app credentials") as info:   # through the VPN too: no rotation
        await ka._get("https://api.kleinanzeigen.de/api/ads.json")
    assert tunnel.failures == [] and isinstance(info.value, NotTheExit)    # the VPN test does not move on either


async def test_ebay_warms_up_on_an_exit_instead_of_leaving_it():
    from app.platforms import ebay
    from tests.conftest import FIXTURES

    page = (FIXTURES / "ebay_search.html").read_bytes()
    exits = {"http://exit1": FakeTransport([(403, b""), (200, page), TransportError("down")]),
             "http://exit2": FakeTransport([(403, b""), (200, page), (403, b"")]),
             "http://exit3": FakeTransport([(403, b""), (200, page)])}
    tunnel = FakeTunnel()
    c = ebay.EbayClient(transport=FakeTransport([]))
    c.limiter = RateLimiter(0, name="eBay")
    c._factory, c._tunnel, c._transport = exits.__getitem__, (lambda: tunnel), None
    assert len((await c.search_page({"_nkw": "iphone"})).ads) == 5          # the warm-up 403 keeps the exit
    assert tunnel.failures == [] and len(exits["http://exit1"].calls) == 2
    assert len((await c.search_page({"_nkw": "ipad"})).ads) == 5            # exit1 gone: exit2 warms up anew
    assert tunnel.failures == [("http://exit1", "unreachable")] and len(exits["http://exit2"].calls) == 2
    assert len((await c.search_page({"_nkw": "ipod"})).ads) == 5            # warm: a 403 refuses the exit
    assert tunnel.failures[1:] == [("http://exit2", "blocked")] and len(exits["http://exit3"].calls) == 2


async def test_ebay_keeps_the_exit_when_the_description_is_refused():
    from app.platforms import ebay
    from tests.conftest import FIXTURES

    exits = {"http://exit1": FakeTransport([(200, (FIXTURES / "ebay_item.html").read_bytes()), (403, b"")])}
    tunnel = FakeTunnel()
    c = ebay.EbayClient(transport=FakeTransport([]))
    c.limiter = RateLimiter(0, name="eBay")
    c._factory, c._tunnel, c._transport = exits.__getitem__, (lambda: tunnel), None
    d = await c.item("920036165580")                                       # itm.ebaydesc.com answers 403
    assert d.place == "Thalgau, Österreich" and tunnel.failures == [] and c.proxy == "http://exit1"
    assert exits["http://exit1"].cleared == 0 and c._warm and c.limiter.cooling_down_s == 0


async def test_a_page_in_place_of_the_one_asked_for_is_not_kept():
    c, tr = _client([(200, b"<html>bot check</html>"), (200, b'{"a": 1}')])
    asked = []

    def valid(r):
        asked.append(r.text)
        return r.text.startswith("{")

    assert (await c._get("https://fake.example/api", cache_s=60, valid=valid)).text == "<html>bot check</html>"
    r = await c._get("https://fake.example/api", cache_s=60, valid=valid)        # the bot check was not kept
    again = await c._get("https://fake.example/api", cache_s=60, valid=valid)    # the answer was
    assert r.json() == {"a": 1} and again is r and len(tr.calls) == 2 and len(asked) == 2   # cached: not judged again


def test_a_proxy_and_a_mock_transport_do_not_mix():
    with pytest.raises(ValueError):
        HttpxTransport({}, httpx.MockTransport(lambda r: httpx.Response(200)), proxy="http://127.0.0.1:1")
    tr = HttpxTransport({}, proxy="http://127.0.0.1:1")
    assert tr.proxy == "http://127.0.0.1:1"
    curl = CurlTransport({}, proxy="http://127.0.0.1:1")
    assert curl.proxy == "http://127.0.0.1:1"


# ------------------------------------------------------------------ the VPN test's Trial (app/services/vpn_check.py)
async def test_a_trial_tries_one_exit_only():
    tunnel = FakeTunnel()
    c, _, built = _vpn_client({"http://exit1": [(403, b"cloudflare")], "http://exit2": [(200, b"{}")]}, tunnel)
    tr = Trial(await tunnel.ensure())
    with trial(tr), pytest.raises(Blocked) as info:
        await c._get("https://fake.example/api")
    assert info.value.sent and (tr.refused, tr.detail, tr.sent, tr.status) == ("blocked", "HTTP 403", 1, 403)
    assert built == ["http://exit1"] and tunnel.failures == [("http://exit1", "blocked")]   # not on to exit2
    assert c.limiter.cooling_down_s == 0

    tunnel = FakeTunnel()
    c, _, built = _vpn_client({"http://exit1": [TransportError("proxy connect refused")]}, tunnel)
    tr = Trial(await tunnel.ensure())
    with trial(tr), pytest.raises(PlatformError, match="Network error through the VPN"):
        await c._get("https://fake.example/api")
    assert (tr.refused, tr.sent) == ("unreachable", 1) and tunnel.failures == [("http://exit1", "unreachable")]
    assert tunnel.details == ["proxy connect refused"] and built == ["http://exit1"]


async def test_a_trial_sends_nothing_when_the_exit_moved():
    tunnel = FakeTunnel()
    c, _, built = _vpn_client({"http://exit2": [(200, b"{}")]}, tunnel)
    tr = Trial(await tunnel.ensure())
    await tunnel.failed(tr.route, "blocked")      # another request found exit1 refused ...
    await tunnel.ensure()                         # ... and moved on to exit2
    with trial(tr), pytest.raises(ExitMoved):
        await c._get("https://fake.example/api")
    assert built == [] and tr.sent == 0


async def test_a_trial_never_goes_direct():
    from app.platforms.vpn import Route

    c, _, built = _vpn_client({}, None)           # the switch went off: no tunnel
    with trial(Trial(Route("http://exit1", 1, "x"))), pytest.raises(PlatformError, match="VPN is off"):
        await c._get("https://fake.example/api")
    assert built == []                            # never factory(None)


async def test_a_trial_notes_a_challenge_and_the_content_type():
    tunnel = FakeTunnel()
    c, _, _ = _vpn_client({"http://exit1": [(503, b"<html>", {"cf-mitigated": "challenge",
                                                                "content-type": "text/html"})]}, tunnel)
    tr = Trial(await tunnel.ensure())
    with trial(tr):
        r = await c._get("https://fake.example/api")
    assert r.status_code == 503 and tr.challenge and tr.status == 503 and "html" in tr.ctype and not tr.refused
    assert tunnel.failures == []                  # the test decides about bot checks, not the client


async def test_a_cancelled_trial_request_drops_its_transport():
    gate = asyncio.Event()

    class Hanging(FakeTransport):
        closed = False

        async def send(self, method, url, *, params=None, headers=None):
            self.calls.append(url)
            await gate.wait()
            return FetchResponse(200, {}, b"{}", url)

        async def aclose(self):
            self.closed = True

    hanging, fresh, seen = Hanging([]), FakeTransport([(200, b"{}")]), []

    def factory(proxy):
        seen.append(proxy)
        return hanging if len(seen) == 1 else fresh

    tunnel = FakeTunnel()
    c = HttpPlatformClient("fake", RateLimiter(0, name="Fake"), name="Fake", transport_factory=factory,
                           tunnel=lambda: tunnel)
    tr = Trial(await tunnel.ensure())

    async def run():
        with trial(tr):
            await c._get("https://fake.example/api")

    task = asyncio.create_task(run())
    while not hanging.calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c._transport is None and c.proxy is None
    await asyncio.sleep(0)
    assert hanging.closed                         # closed in the background
    assert (await c._get("https://fake.example/api")).status_code == 200      # a live request: a new transport
    assert seen == ["http://exit1", "http://exit1"] and len(fresh.calls) == 1
