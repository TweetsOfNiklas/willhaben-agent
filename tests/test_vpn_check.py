"""Settings → VPN → Test the sites (app/services/vpn_check.py): exit by exit until the site answers; caps, live
traffic, stop and cancel, the panel. Fake wireproxy (FakeRunner) and fake transports only: no tunnel, no site request."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import platforms
from app.db import get_setting, set_setting
from app.platforms import PlatformError, vpn
from app.platforms.base import Platform
from app.platforms.httpclient import FetchResponse, HttpPlatformClient, TransportError
from app.platforms.ratelimit import RateLimiter
from app.platforms.types import SearchPage, SearchSpec
from app.services import jobs, vpn_check
from tests.test_vpn import FakeRunner, _collection, _conf

JSON, HTML = {"content-type": "application/json"}, {"content-type": "text/html"}
PAGE = (200, b'{"results": []}', JSON)
BOT = (200, b"<html>Just a moment...</html>", HTML)
N = lambda i: f"at-vie-wg-{i:03d}"                      # noqa: E731


@pytest.fixture(autouse=True)
def no_source():
    """Every test starts and ends without uploaded configs (DATA_DIR lives for the whole run)."""
    vpn.remove_source()
    yield
    vpn.remove_source()


@pytest.fixture
def fake(monkeypatch):
    """wireproxy without wireproxy (tests/test_vpn.py): no tunnel ever starts."""
    r = FakeRunner()
    monkeypatch.setattr(vpn, "runner", r)
    return r


class ExitTransport:
    """One exit's answers in order: (status, body, headers), an Exception, or an asyncio.Event to wait for first."""

    def __init__(self, answers):
        self.answers, self.calls, self.closed = list(answers), 0, False

    async def send(self, method, url, *, params=None, headers=None):
        self.calls += 1
        a = self.answers.pop(0)
        while isinstance(a, asyncio.Event):
            await a.wait()
            a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        status, body, hdrs = a
        return FetchResponse(status, hdrs, body, url)

    def clear_cookies(self):
        pass

    async def aclose(self):
        self.closed = True


def exit_factory(runner, answers, built):
    """transport_factory: the route's port -> its exit's transport; a direct transport (None) fails the test."""
    transports = {}

    def factory(proxy):
        assert proxy is not None, "a test request went out directly"
        name = runner.ports[int(proxy.rsplit(":", 1)[1])]
        built.append(name)
        return transports.setdefault(name, ExitTransport(answers.get(name, [])))

    factory.transports = transports
    return factory


class FakeSite(Platform):
    id, name, api_host, home_country = "fake", "Fake", "api.fake.example", "AT"
    capabilities = frozenset({"keyword_search"})

    def __init__(self, runner, answers, per_hour=0):
        self.built = []
        self.factory = exit_factory(runner, answers, self.built)
        self.client = HttpPlatformClient("fake", RateLimiter(0, per_hour, name="Fake"), name="Fake",
                                         transport_factory=self.factory)

    @property
    def limiter(self):
        return self.client.limiter

    def parse_search_url(self, url):
        raise NotImplementedError

    def describe(self, spec):
        return "fake"

    def keyword_search(self, keyword, price_from=None, price_to=None, *, category=None, sort="newest"):
        return SearchSpec(platform="fake", url=f"https://api.fake.example/s?q={keyword}", keyword=keyword)

    async def search(self, spec, *, rows=None, cache_s=0):
        r = await self.client._get(spec.url, kind="search")
        if r.status_code != 200:
            raise PlatformError(f"Fake search returned HTTP {r.status_code}")
        if b"results" not in r.content:
            raise PlatformError("Fake answered without a result list")
        return SearchPage(rows_found=0, ads=[])

    def ad_url(self, ad_id):
        return ad_id


@pytest.fixture
def site(fake):
    def make(answers, per_hour=0):
        s = FakeSite(fake, answers, per_hour)
        platforms.set_platform("fake", s)
        vpn.set_enabled("fake", True)
        return s

    yield make
    platforms.reset()


async def _test(only=None, **kw) -> vpn_check.SiteRun:
    assert vpn_check.start(only, **kw) is None
    await vpn_check._run.task
    return {s.pid: s for s in vpn_check._run.sites}[only or "fake"]


async def _until(cond, s=2.0):
    end = time.monotonic() + s
    while not cond():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.01)


def _reasons(pid="fake"):
    return {n: r for n, (_, r) in vpn.marks(pid).items()}


# ------------------------------------------------------------------ exit by exit
async def test_goes_from_exit_to_exit_until_the_site_answers_and_keeps_it(site, fake):
    _collection(N(1), N(2), N(3), N(4))
    fake.unreachable = {N(2)}
    st = site({N(1): [(403, b"cf", HTML)], N(3): [TransportError("reset")], N(4): [PAGE, PAGE]})
    s = await _test()
    assert s.verdict == "ok" and s.params == {"exit": N(4), "current": False}
    assert [(a.exit, a.verdict, a.requests) for a in s.attempts] == [
        (N(1), "blocked", 1), (N(2), "unreachable", 0), (N(3), "unreachable", 1), (N(4), "ok", 1)]
    assert s.attempts[0].detail == "HTTP 403" and s.attempts[2].detail == "reset" and s.requests == 3
    assert _reasons() == {N(1): "blocked", N(2): "unreachable", N(3): "unreachable"}
    assert st.limiter.cooling_down_s == 0                       # a refused exit never pauses the site
    tn = vpn.tunnel_for("fake")
    assert tn.config == N(4) and vpn._load()["fake"]["current"] == N(4) and tn._listeners == []

    probes = len(fake.probes)                                   # the found exit stays: no probe, the same route
    await st.search(st.keyword_search("x"))
    assert len(fake.probes) == probes and st.client.proxy == tn.route.proxy

    job = jobs.snapshot()["recent"][0]
    assert (job["label"], job["lane"], job["result"]) == ("VPN test", "vpn", "1 of 1 sites work through the VPN")
    vpn_check._run = None                                       # as after a restart: read back from the setting
    assert vpn_check.view()["rows"][0]["verdict"] == "ok"


async def test_a_working_current_exit_costs_one_request(site, fake):
    _collection(N(1), N(2))
    site({N(1): [PAGE]})
    tn = vpn.tunnel_for("fake")
    gen = (await tn.ensure()).gen
    s = await _test()
    assert s.verdict == "ok" and s.params["current"] is True and s.requests == 1
    assert len(fake.probes) == 1 and fake.starts == [N(1)] and tn.route.gen == gen
    assert vpn_check.view()["rows"][0]["message"] == f"The current exit {N(1)} works."


async def test_a_bot_page_is_marked_for_an_hour(site):
    _collection(N(1), N(2))
    site({N(1): [BOT], N(2): [PAGE]})
    s = await _test()
    assert s.verdict == "ok" and s.params["exit"] == N(2)
    until, reason = vpn.marks("fake")[N(1)]
    assert reason == "botcheck" and until - time.time() <= 3600
    assert s.attempts[0].verdict == "botcheck" and "without a result list" in s.attempts[0].detail
    assert s.attempts[0].requests == 1


async def test_bot_pages_everywhere_are_the_sites_problem(site):
    _collection(*(N(i) for i in range(1, 5)))
    st = site({N(i): [BOT] for i in range(1, 5)})
    s = await _test()
    assert s.verdict == "odd_page" and s.params["n"] == 3
    assert _reasons() == {N(1): "botcheck", N(2): "botcheck"}  # the third is not marked: probably not the exits
    assert vpn.tunnel_for("fake").config == N(3) and N(4) not in st.built
    row = vpn_check.view()["rows"][0]
    assert row["label"] == "Site error" and "page the app does not know" in row["message"]


async def test_a_challenge_moves_on_whatever_its_status_but_json_errors_do_not(site):
    _collection(N(1), N(2))
    site({N(1): [(503, b"<html>", {"content-type": "text/html", "cf-mitigated": "challenge"})], N(2): [PAGE]})
    s = await _test()
    assert s.verdict == "ok" and s.params["exit"] == N(2) and _reasons() == {N(1): "botcheck"}

    await vpn.source_changed()
    _collection(N(1), N(2))
    site({N(1): [(200, b'{"errors": []}', JSON)]})
    s = await _test()
    assert s.verdict == "site_error" and vpn.marks("fake") == {} and vpn.tunnel_for("fake").config == N(1)


async def test_a_site_error_or_the_sites_own_pause_marks_nothing(site):
    _collection(N(1), N(2))
    st = site({N(1): [(503, b"{}", JSON)]})
    s = await _test()
    assert s.verdict == "site_error" and "HTTP 503" in s.params["error"] and vpn.marks("fake") == {}
    tn = vpn.tunnel_for("fake")
    assert tn.config == N(1)
    assert "stays in use" in vpn_check.view()["rows"][0]["message"]

    st.limiter.penalize()                                       # the site paused itself after a block
    tn._exhausted_until, tn.failing = time.monotonic() + 600, True
    s = await _test()
    assert s.verdict == "paused" and st.factory.transports[N(1)].calls == 1
    assert tn.failing and tn._exhausted_until > time.monotonic()   # untouched: nothing was tested
    assert vpn.marks("fake") == {}


async def test_requests_stay_within_the_budget_cap(site):
    _collection(*(N(i) for i in range(1, 6)))
    st = site({N(i): [(403, b"", HTML)] for i in range(1, 6)}, per_hour=8)
    s = await _test()
    assert s.verdict == "budget" and s.requests == 2 and st.built == [N(1), N(2)]   # min(max(1, 8 // 4), 8 // 2)
    assert "2 requests" in vpn_check.view()["rows"][0]["message"]

    st = site({}, per_hour=8)
    st.limiter._recent.extend([time.monotonic()] * 7)           # the agents used most of the hour
    s = await _test()
    assert s.verdict == "no_budget" and st.built == [] and s.requests == 0 and s.params["used"] == 7


async def test_ten_exits_then_test_more_goes_on(site):
    _collection(*(N(i) for i in range(1, 16)))
    st = site({**{N(i): [(403, b"", HTML)] for i in range(1, 13)}, N(13): [PAGE]})
    s = await _test()
    assert s.verdict == "more" and s.tried == 10 and s.params["left"] == 5
    row = vpn_check.view()["rows"][0]
    assert (row["action"], row["action_label"]) == ("more", "Test 5 more")
    assert row["hint"] and "dedicated IP" in row["hint"]        # all refused with 403
    tn = vpn.tunnel_for("fake")
    assert tn.route is None and not tn.failing
    before = len(st.built)
    s = await _test("fake")
    assert s.verdict == "ok" and s.params["exit"] == N(13) and st.built[before:] == [N(11), N(12), N(13)]


async def test_no_exit_left_fails_closed_and_forget_tests_again(site, fake):
    _collection(*(N(i) for i in range(1, 7)))
    fake.unreachable = {N(i) for i in range(2, 7)}
    st = site({N(1): [(403, b"", HTML)]})
    s = await _test()
    assert s.verdict == "none" and s.params == {"n": 6, "k": 0}
    tn = vpn.tunnel_for("fake")
    assert tn.failing and vpn.health()[0] == "problem" and len(fake.probes) == 6
    assert vpn_check.view()["rows"][0]["action"] == "forget"

    fake.unreachable.clear()
    st.factory.transports[N(1)].answers.append(PAGE)
    s = await _test("fake", forget=True)
    assert s.verdict == "ok" and s.params["exit"] == N(1)


async def test_a_profile_is_asked_again_despite_its_mark(site):
    vpn.import_upload("home.conf", _conf().encode())
    vpn.mark("fake", "home", "blocked")
    vpn.mark("other", "x", "blocked")
    site({"home": [(403, b"", HTML), PAGE]})
    s = await _test()
    assert s.verdict == "profile" and s.params["reason"] == "blocked" and s.requests == 1
    assert "x" in vpn.marks("other")                            # other sites keep theirs
    assert "does not answer through the profile (blocked by the site)" in vpn_check.view()["rows"][0]["message"]
    s = await _test()
    assert s.verdict == "ok" and s.params["exit"] == "home" and vpn.marks("fake") == {}


# ------------------------------------------------------------------ one at a time, stop, live traffic, cancel
async def test_one_test_at_a_time_and_the_live_panel(site):
    from app.main import templates

    _collection(N(1))
    g = asyncio.Event()
    st = site({N(1): [g, PAGE]})
    assert vpn_check.start() is None
    await _until(lambda: st.factory.transports.get(N(1)) and st.factory.transports[N(1)].calls == 1)
    assert vpn_check.start() == vpn_check.start("fake") == "A VPN test is running"
    v = vpn_check.view()
    assert v["live"] and all(r["action"] is None for r in v["rows"])
    assert v["rows"][0]["message"] == f"Asking Fake through {N(1)} …"
    html = templates.get_template("settings/_vpn_test.html").render(vpn_note=None)
    assert 'hx-trigger="every 2s"' in html and "Stop" in html and "Test now" not in html and "<details" not in html
    g.set()
    await vpn_check._run.task
    assert vpn_check._run.sites[0].verdict == "ok" and not vpn_check.view()["live"]


async def test_stop_waits_for_the_exit_in_progress(site):
    _collection(N(1), N(2))
    g = asyncio.Event()
    st = site({N(1): [g, (403, b"", HTML)], N(2): [PAGE]})
    assert vpn_check.start() is None
    await _until(lambda: st.factory.transports.get(N(1)) and st.factory.transports[N(1)].calls == 1)
    vpn_check.stop()
    assert vpn_check.view()["stopping"]
    g.set()
    await vpn_check._run.task
    s = vpn_check._run.sites[0]
    assert s.verdict == "stopped" and s.params["reason"] == "you" and vpn_check._run.status == "stopped"
    assert _reasons() == {N(1): "blocked"} and N(2) not in st.built    # its try completed; no further exit


async def test_live_requests_on_the_same_tunnel(site):
    _collection(N(1), N(2))
    g = asyncio.Event()
    st = site({N(1): [g, BOT], N(2): [PAGE]})
    assert vpn_check.start() is None
    await _until(lambda: st.factory.transports.get(N(1)) and st.factory.transports[N(1)].calls == 1)
    tn = vpn.tunnel_for("fake")
    await tn.failed(tn.route, "blocked", detail="HTTP 403")     # a search agent's request was refused meanwhile
    await tn.ensure()                                            # and moved the tunnel on
    g.set()
    await vpn_check._run.task
    s = vpn_check._run.sites[0]
    assert s.verdict == "ok" and s.params["exit"] == N(2)
    assert _reasons() == {N(1): "blocked"}                      # not botcheck: the test's late answer marks nothing
    a = s.attempts[0]
    assert (a.exit, a.verdict, a.detail, a.requests) == (N(1), "blocked", "HTTP 403", None)


async def test_never_goes_direct_when_switched_off_mid_test(site):
    _collection(N(1), N(2))
    g = asyncio.Event()
    st = site({N(1): [g, (403, b"", HTML)]})
    assert vpn_check.start() is None
    await _until(lambda: st.factory.transports.get(N(1)) and st.factory.transports[N(1)].calls == 1)
    vpn.set_enabled("fake", False)
    await vpn.stop_site("fake")
    g.set()
    await vpn_check._run.task
    s = vpn_check._run.sites[0]
    assert s.verdict == "off" and vpn.marks("fake") == {}
    assert not any("fake" in p.users for p in vpn._procs.values())


async def test_new_configs_cancel_the_test_and_nothing_reaches_them(site):
    _collection(N(1))
    g = asyncio.Event()
    st = site({N(1): [g, PAGE]})
    assert vpn_check.start() is None
    await _until(lambda: st.factory.transports.get(N(1)) and st.factory.transports[N(1)].calls == 1)
    await vpn_check.cancel("uploaded")
    s = vpn_check._run.sites[0]
    assert vpn_check._run.status == "stopped" and s.verdict == "stopped" and s.params["reason"] == "uploaded"
    assert vpn.marks("fake") == {} and st.client._transport is None
    await asyncio.sleep(0)
    assert st.factory.transports[N(1)].closed                  # the half-used transport is closed, not reused
    assert json.loads(get_setting("vpn_test"))["status"] == "stopped"

    await vpn.source_changed()
    _collection("de-fra-wg-001")
    vpn_check.clear()
    assert not vpn_check.view()["run"] and "de-fra-wg-001" not in st.built


async def test_clear_and_forget_routes_stop_a_running_test(site):
    from app.main import settings_vpn_clear, settings_vpn_forget

    _collection(N(1), N(2))
    g = asyncio.Event()
    st = site({N(1): [g, (403, b"", HTML)], N(2): [PAGE]})
    assert vpn_check.start() is None
    await _until(lambda: st.factory.transports.get(N(1)) and st.factory.transports[N(1)].calls == 1)
    await settings_vpn_forget()
    assert vpn_check._run.reason == "forgotten"
    g.set()
    await vpn_check._run.task
    assert vpn_check._run.sites[0].verdict == "stopped" and N(2) not in st.built
    assert vpn_check.view()["summary"] == "Stopped: the blocks were forgotten"

    g2 = asyncio.Event()
    st.factory.transports[N(1)].answers[:] = [g2, PAGE]
    vpn.forget()
    assert vpn_check.start() is None
    await _until(lambda: st.factory.transports[N(1)].calls == 2)
    await settings_vpn_clear()
    assert not vpn_check.running() and vpn.source() == (None, []) and not vpn_check.view()["run"]


async def test_shutdown_mid_test_shows_interrupted(site):
    _collection(N(1))
    g = asyncio.Event()
    st = site({N(1): [g, PAGE]})
    assert vpn_check.start() is None
    await _until(lambda: st.factory.transports.get(N(1)) and st.factory.transports[N(1)].calls == 1)
    await vpn_check.shutdown()
    assert not vpn_check.running() and json.loads(get_setting("vpn_test"))["status"] == "interrupted"
    vpn_check._run = None
    row = vpn_check.view()["rows"][0]
    assert row["verdict"] == "interrupted" and row["message"] == "Interrupted by a restart of the app"

    stored = json.loads(get_setting("vpn_test"))                # a hard kill: stored while it ran
    stored["status"], stored["sites"][0]["status"] = "running", "testing"
    set_setting("vpn_test", json.dumps(stored))
    v = vpn_check.view()
    assert v["summary"] == "Interrupted by a restart of the app" and v["rows"][0]["verdict"] == "interrupted"
    assert v["rows"][0]["action"] == "test" and not v["live"]


async def test_cancelled_before_it_began(site):
    _collection(N(1))
    site({N(1): [PAGE]})
    assert vpn_check.start() is None
    await vpn_check.cancel("removed")                           # the task had not run yet
    assert vpn_check._run.status == "stopped" and vpn_check._run.sites[0].params == {"reason": "removed"}
    assert json.loads(get_setting("vpn_test"))["status"] == "stopped"


async def test_preconditions(site, fake, monkeypatch):
    from app.config import get_settings

    assert vpn_check.blocker() == "Upload WireGuard configs first"
    _collection(N(1))
    assert vpn_check.blocker() == "Switch the VPN on for a site first"
    fake.installed = False
    assert vpn_check.blocker() == "wireproxy is not installed"
    fake.installed = True
    monkeypatch.setattr(get_settings(), "wh_replay", True)
    assert "offline replay" in vpn_check.blocker()
    monkeypatch.setattr(get_settings(), "wh_replay", False)
    site({N(1): [PAGE]})                                        # fake on, willhaben's VPN off
    await _test()
    assert [s.pid for s in vpn_check._run.sites] == ["fake"]
    vpn.set_enabled("fake", False)
    assert vpn_check.start("fake") == "Switch the VPN on for a site first"
    assert vpn_check.view()["blocked"] == "Switch the VPN on for a site first"


# ------------------------------------------------------------------ the real adapters' checks
async def test_kleinanzeigen_credentials_are_not_the_exits_fault(fake, request):
    from app.platforms import kleinanzeigen

    _collection(N(1), N(2))
    platforms.set_platform("kleinanzeigen", kleinanzeigen.KleinanzeigenPlatform())
    request.addfinalizer(platforms.reset)
    c = kleinanzeigen.KleinanzeigenClient(transport=ExitTransport([]))
    built = []
    c._factory = exit_factory(fake, {N(1): [(403, b'{"error":"unauthorized"}', JSON)]}, built)
    c._tunnel, c._transport = (lambda: vpn.tunnel_for("kleinanzeigen")), None
    c.limiter = RateLimiter(0, name="Kleinanzeigen")
    kleinanzeigen.set_client(c)
    request.addfinalizer(lambda: kleinanzeigen.set_client(None))
    vpn.set_enabled("kleinanzeigen", True)
    s = await _test("kleinanzeigen")
    assert s.verdict == "site_error" and "app credentials" in s.params["error"]
    assert vpn.marks("kleinanzeigen") == {} and built == [N(1)]


def _ebay(fake, request, monkeypatch, answers):
    from app.platforms import ebay

    platforms.set_platform("ebay", ebay.EbayPlatform())
    request.addfinalizer(platforms.reset)
    c = ebay.EbayClient(transport=ExitTransport([]))
    built = []
    c._factory = exit_factory(fake, answers, built)
    c._tunnel, c._transport = (lambda: vpn.tunnel_for("ebay")), None
    ebay.set_client(c)
    request.addfinalizer(lambda: ebay.set_client(None))
    monkeypatch.setattr(ebay.ebay_limiter(), "min_gap_s", 0)
    vpn.set_enabled("ebay", True)
    return c, built


async def test_ebay_bot_page_then_warm_up_through_the_test(fake, request, monkeypatch):
    from tests.conftest import FIXTURES

    _collection(N(1), N(2))
    page = (FIXTURES / "ebay_search.html").read_bytes()
    c, _ = _ebay(fake, request, monkeypatch, {N(1): [(200, b"<html>Pardon Our Interruption</html>", HTML)],
                                               N(2): [(403, b"", HTML), (200, page, HTML)]})
    s = await _test("ebay")
    assert s.verdict == "ok" and s.params["exit"] == N(2) and c._warm
    assert _reasons("ebay") == {N(1): "botcheck"}
    assert [(a.exit, a.verdict, a.requests) for a in s.attempts] == [(N(1), "botcheck", 1), (N(2), "ok", 2)]


async def test_ebay_cannot_run_away_through_blocked_exits(fake, request, monkeypatch):
    from app.platforms import ebay

    _collection(*(N(i) for i in range(1, 7)))
    _, built = _ebay(fake, request, monkeypatch, {N(i): [(403, b"", HTML)] * 3 for i in range(1, 7)})
    s = await _test("ebay")
    assert s.verdict == "budget" and s.requests == 9 <= vpn_check._cap(ebay.ebay_limiter(), 3) == 10
    assert _reasons("ebay") == {N(1): "blocked", N(2): "blocked", N(3): "blocked"} and built == [N(1), N(2), N(3)]
    assert ebay.ebay_limiter().last_hour == 9 and ebay.ebay_limiter().cooling_down_s == 0


async def test_ebay_stop_waits_for_the_warm_up_on_one_exit(fake, request, monkeypatch):
    _collection(N(1), N(2))
    g = asyncio.Event()
    c, built = _ebay(fake, request, monkeypatch, {N(1): [(403, b"", HTML), (403, b"", HTML), g, (403, b"", HTML)]})
    assert vpn_check.start("ebay") is None
    await _until(lambda: c._factory.transports.get(N(1)) and c._factory.transports[N(1)].calls == 3)
    vpn_check.stop()
    g.set()
    await vpn_check._run.task
    s = vpn_check._run.sites[0]
    assert s.verdict == "stopped" and s.requests == 3 and N(2) not in built and _reasons("ebay") == {N(1): "blocked"}


# ------------------------------------------------------------------ the settings page
def _wait_test(timeout=5.0):
    """TestClient: the test runs in the app's loop thread; wait until it ended (also before its job showed up)."""
    from tests.conftest import wait_jobs

    end = time.monotonic() + timeout
    while vpn_check.running() and time.monotonic() < end:
        time.sleep(0.02)
    assert not vpn_check.running(), "the VPN test still runs"
    wait_jobs()


def test_settings_page_button_and_panel(fake):
    from app.main import app

    _collection(N(1))
    platforms.set_platform("fake", FakeSite(fake, {N(1): [PAGE, PAGE]}))
    vpn.set_enabled("fake", True)
    try:
        with TestClient(app) as c:
            page = c.get("/settings/vpn").text
            assert 'id="vpn-test"' in page and "Test the sites" in page and "every 2s" not in page
            assert 'id="vpn-sites" data-refresh-on-lane="vpn"' in page and "Not tested yet." in page

            r = c.post("/settings/vpn/test", headers={"HX-Request": "true"})
            assert r.status_code == 200 and "<html" not in r.text and 'id="vpn-test"' in r.text
            _wait_test()
            panel = c.get("/settings/vpn/test").text
            assert f"Works through {N(1)}" in panel and "Exits tried (1)" in panel and "every 2s" not in panel
            assert "Test again" in panel and f"Exit now: {N(1)}" in c.get("/settings/vpn").text
            set_setting("ui_lang", "de")
            assert f"Funktioniert über {N(1)}" in c.get("/settings/vpn/test").text
            set_setting("ui_lang", "en")
            assert any(j["lane"] == "vpn" and j["label"] == "VPN test" for j in c.get("/jobs").json()["recent"])

            assert c.post("/settings/vpn/test", data={"site": "nope"}).status_code == 404
            r = c.post("/settings/vpn/test", data={"site": "fake"}, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"].startswith("/settings/vpn?msg=")
            _wait_test()
            r = c.post("/settings/vpn/test/stop", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"].startswith("/settings/vpn?msg=")
            assert 'id="vpn-test"' not in c.post("/settings/vpn/clear").text
    finally:
        platforms.reset()
