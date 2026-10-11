"""The shared platform HTTP client: pacing, cache, blocks, network errors, recording per platform, transports."""
import httpx
import pytest
from sqlmodel import select

from app.db import WhRecording, session
from app.platforms import Blocked, PlatformError
from app.platforms.httpclient import CurlTransport, FetchResponse, HttpPlatformClient, HttpxTransport, TransportError
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
        status, body = a
        return FetchResponse(status, {"content-type": "application/json"}, body, url)

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
