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


class FakeSite2(FakeSite):
    id, name, api_host = "fake2", "Fake 2", "api.fake2.example"


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
    assert (job["label"], job["lane"], job["result"]) == ("VPN test", "vpn", "Fake: Works")   # one site: no "1 of 1"
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
    assert s.params["why"] == "quarter" and s.params["left"] == 3
    assert vpn_check.view()["rows"][0]["message"] == ("Stopped after 2 requests: one test uses at most a quarter of "
                                                      "the 8 requests Fake allows per hour. 3 exits not tried yet.")

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
    assert (row["action"]["kind"], row["action"]["label"]) == ("more", "Test 5 more")
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
    assert vpn_check.view()["rows"][0]["action"] == {"kind": "forget", "label": "Forget its blocks and test again"}
    assert vpn_check.view()["rows"][0]["message"] == "No exit works: all 6 tried."

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


async def test_a_try_never_sends_through_an_exit_a_live_request_just_left(site):
    _collection(N(1), N(2))
    g = asyncio.Event()
    st = site({N(1): [g, (403, b"", HTML)], N(2): [PAGE, PAGE]})
    live = asyncio.create_task(st.search(st.keyword_search("x")))     # a search agent's request holds the limiter
    await _until(lambda: st.factory.transports.get(N(1)) and st.factory.transports[N(1)].calls == 1)
    assert vpn_check.start() is None
    await _until(lambda: st.limiter._lock._waiters)                     # the test's try waits for the limiter on N1
    g.set()                                                             # the live request is refused and leaves N1
    await live
    await vpn_check._run.task
    s = vpn_check._run.sites[0]
    assert st.factory.transports[N(1)].calls == 1                       # the test's request never went out through N1
    assert s.verdict == "ok" and s.params["exit"] == N(2) and _reasons() == {N(1): "blocked"}
    assert st.limiter.last_hour == 3                                    # the try that sent nothing costs no budget


async def test_an_exit_whose_wireproxy_keeps_stopping_ends_the_site(site, fake, monkeypatch):
    probe = fake.probe

    async def dies_after_the_probe(port, host):
        ok = await probe(port, host)
        for proc in vpn._procs.values():
            if proc.port == port:
                proc.handle.terminate()
        return ok

    monkeypatch.setattr(fake, "probe", dies_after_the_probe)
    _collection(N(1), N(2))
    st = site({})
    s = await _test()
    assert s.verdict == "error" and "3 times in a row" in s.params["error"]
    assert fake.starts == [N(1)] * 3 and s.requests == 0 and st.built == [] and vpn.marks("fake") == {}


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
    v = vpn_check.view()
    row = v["rows"][0]
    assert row["verdict"] == "interrupted" and row["label"] == "Stopped" and row["message"] == ""   # the header says it
    assert v["summary"] == "Interrupted by a restart of the app"

    stored = json.loads(get_setting("vpn_test"))                # a hard kill: stored while it ran
    stored["status"], stored["sites"][0]["status"] = "running", "testing"
    set_setting("vpn_test", json.dumps(stored))
    v = vpn_check.view()
    assert v["summary"] == "Interrupted by a restart of the app" and v["rows"][0]["verdict"] == "interrupted"
    assert v["rows"][0]["action"]["kind"] == "test" and not v["live"]


async def test_cancelled_before_it_began(site):
    _collection(N(1))
    site({N(1): [PAGE]})
    assert vpn_check.start() is None
    await vpn_check.cancel("removed")                           # the task had not run yet
    assert vpn_check._run.status == "stopped" and vpn_check._run.sites[0].params == {"reason": "removed"}
    assert json.loads(get_setting("vpn_test"))["status"] == "stopped"


async def test_one_site_again_keeps_the_other_sites_results(site, fake):
    _collection(N(1), N(2))
    g = asyncio.Event()
    site({N(1): [g, PAGE, (403, b"", HTML)], N(2): [PAGE]})
    platforms.set_platform("fake2", FakeSite2(fake, {N(1): [(503, b"{}", JSON)]}))
    vpn.set_enabled("fake2", True)
    assert vpn_check.start() is None
    await _until(lambda: vpn_check._run.sites[1].status == "done")
    assert vpn_check.view()["progress"] == "1 of 2 sites done"
    g.set()
    await vpn_check._run.task
    v = vpn_check.view()
    assert [r["pid"] for r in v["rows"]] == ["fake", "fake2"] and v["summary"] == "1 of 2 sites work through the VPN"
    kept = vpn_check._run.sites[1]

    assert vpn_check.start("fake") is None                              # "Test now" on one row
    v = vpn_check.view()
    assert v["live"] and v["progress"] == "Testing Fake" and [r["status"] for r in v["rows"]] == ["waiting", "done"]
    await vpn_check._run.task
    assert vpn_check._run.sites[1] is kept and vpn_check._run.only == "fake"
    job = jobs.snapshot()["recent"][0]
    assert job["result"] == "Fake: Works" and job["total"] == 1
    vpn_check._run = None                                               # after a restart: from the setting
    v = vpn_check.view()
    assert [(r["pid"], r["verdict"]) for r in v["rows"]] == [("fake", "ok"), ("fake2", "site_error")]
    assert v["summary"] == "1 of 2 sites work through the VPN"
    assert v["rows"][0]["ago"] is None and v["rows"][1]["ago"] is not None    # the kept row says when it was tested


async def test_a_result_cleared_while_its_test_still_runs_stays_gone(site):
    _collection(N(1))
    g = asyncio.Event()
    st = site({N(1): [g, PAGE]})
    assert vpn_check.start() is None
    await _until(lambda: st.factory.transports.get(N(1)) and st.factory.transports[N(1)].calls == 1)
    task = vpn_check._run.task
    vpn_check.clear()                                                   # as when cancel() gave up waiting for it
    assert vpn_check.start() == "A VPN test is running"
    g.set()
    await task
    assert vpn_check._run is None and get_setting("vpn_test") == "" and not vpn_check.view()["run"]


async def test_removing_configs_stops_the_exits_even_when_the_test_fails_to_stop(monkeypatch):
    from app.main import settings_vpn_clear

    stopped = []

    async def broken(reason):
        raise RuntimeError("database is locked")

    async def exits_stop():
        stopped.append(True)

    monkeypatch.setattr(vpn_check, "cancel", broken)
    monkeypatch.setattr(vpn, "source_changed", exits_stop)
    with pytest.raises(RuntimeError):
        await settings_vpn_clear()
    assert stopped == [True]


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
    assert s.verdict == "site_error" and "app credentials" in s.params["error"] and s.params["fix"] == "settings"
    assert vpn.marks("kleinanzeigen") == {} and built == [N(1)]
    assert s.attempts[0].detail == "HTTP 403"                  # the row's message has the long error, once
    row = vpn_check.view()["rows"][0]
    assert row["action"] == {"kind": "settings", "label": "Open Kleinanzeigen settings",
                             "href": "/settings/kleinanzeigen"}


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
    assert s.verdict == "budget" and s.requests == 9 <= vpn_check._cap(ebay.ebay_limiter(), 3)[0] == 10
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


async def test_ebay_sends_at_most_three_requests_per_exit_whatever_its_visitor_does(fake, request, monkeypatch):
    from tests.conftest import FIXTURES

    _collection(N(1), N(2))
    g = asyncio.Event()
    page = (FIXTURES / "ebay_search.html").read_bytes()
    c, built = _ebay(fake, request, monkeypatch, {N(1): [(403, b"", HTML), g] + [(403, b"", HTML)] * 3,
                                                  N(2): [(200, page, HTML)]})
    assert vpn_check.start("ebay") is None
    await _until(lambda: c._factory.transports.get(N(1)) and c._factory.transports[N(1)].calls == 2)
    c.new_anonymous_session()                   # a live request's refused answer started a new visitor meanwhile
    g.set()
    await vpn_check._run.task
    s = vpn_check._run.sites[0]
    assert c._factory.transports[N(1)].calls == 3 and _reasons("ebay") == {N(1): "blocked"}
    assert [(a.exit, a.verdict, a.requests) for a in s.attempts] == [(N(1), "blocked", 3), (N(2), "ok", 1)]


# ------------------------------------------------------------------ the settings page
def _row(verdict, tried=0, **params):
    """The panel's row for a finished test of the fake site with this verdict."""
    now = time.time()
    s = vpn_check.SiteRun("fake", "Fake", status="done", verdict=verdict, params=params, started=now - 5,
                          finished=now, attempts=[vpn_check.Attempt(N(i), "blocked", "HTTP 403", 1, 2.5)
                                                  for i in range(1, tried + 1)], requests=tried)
    vpn_check._run = vpn_check.Run([s], status="done", finished=now)
    return vpn_check.view()["rows"][0]


async def test_next_steps_only_where_they_help(site):
    _collection(N(1), N(2))
    st = site({}, per_hour=40)
    r = _row("ok", 1, exit=N(1))
    assert r["action"] is None and r["counters"] == "1 tried · 1 request · 5 s"
    r = _row("no_budget", used=36, per_hour=40)
    assert (r["label"], r["tone"], r["counters"]) == ("Not tested", "", "")      # not "Paused", no "0 tried"
    now = time.monotonic()
    st.limiter._recent.extend([now - 3000] + [now] * 35)               # 36 of 40: one has to leave the hour first
    r = _row("no_budget", used=36, per_hour=40)
    assert r["action"] is None and r["note"] == "A test is possible again in about 10 min."
    st.limiter._recent.clear()
    assert _row("no_budget", used=36, per_hour=40)["action"]["kind"] == "test"

    st.vpn_test_requests = 3                                            # eBay: 3 per exit, 10 of 40 per test
    r = _row("budget", 3, n=9, used=9, per_hour=40, left=15, why="quarter")
    assert r["action"]["label"] == "Test 3 more" and r["label"] == "No exit found yet"
    assert r["message"] == ("Stopped after 9 requests: one test uses at most a quarter of the 40 requests Fake allows "
                            "per hour. 15 exits not tried yet.")
    assert _row("budget", n=4, used=30, per_hour=40, left=1, why="left")["message"] == (
        "Stopped after 4 requests: one test uses at most half of what is left of the hour's 40 requests (30 used). "
        "1 exit not tried yet.")
    assert _row("budget", n=1, used=36, per_hour=40, left=0, why="full")["message"] == (
        "Stopped after 1 request: Fake has used 36 of its 40 requests this hour; the rest stays for the search agents.")
    st.vpn_test_requests = 1

    r = _row("paused", error="Fake pause after a block, 300 s left")
    assert (r["label"], r["message"]) == ("Not tested", "Not tested: Fake pause after a block, 300 s left")
    st.limiter.penalize()
    st.limiter._cooldown_until = time.monotonic() + 600
    assert _row("paused", error="x")["note"] == "A test is possible again in about 10 min."
    st.limiter.reset()
    r = _row("stopped", 2, reason="you")
    assert (r["label"], r["message"], r["action"]["kind"]) == ("Stopped", "", "test")   # the header says who stopped
    assert _row("site_error", 1, exit=N(1), error="HTTP 503", fix="")["action"]["kind"] == "test"
    r = _row("none", 6, n=6, k=18)
    assert r["message"] == "No exit works: 6 tried, 18 skipped because they failed recently."
    assert "never" not in r["message"] and r["action"]["label"] == "Forget its blocks and test again"


async def test_no_row_buttons_while_no_test_can_start(site, fake):
    from app.main import templates

    _collection(N(1))
    site({})
    _row("stopped", 1, reason="you")
    assert 'name="site"' in templates.get_template("settings/_vpn_test.html").render(vpn_note=None)
    fake.installed = False
    html = templates.get_template("settings/_vpn_test.html").render(vpn_note="wireproxy is not installed")
    assert html.count("wireproxy is not installed") == 1 and 'name="site"' not in html and " disabled>" in html


async def test_the_panel_in_german(site):
    from app.i18n import set_request_lang

    _collection(N(1))
    site({})
    set_request_lang("de")
    try:
        r = _row("ok", 1, exit=N(1))
        assert r["counters"] == "1 probiert · 1 Anfrage · 5 s" and r["attempts"][0]["cost"] == ["1 Anfrage", "2,5 s"]
        assert vpn_check.view()["summary"] == "Fake: Funktioniert"
        assert _row("more", 10, n=10, left=4)["message"] == ("Kein funktionierender Ausgang unter den 10 probierten; "
                                                            "ein Test probiert höchstens 10. 4 Ausgänge noch offen.")
    finally:
        set_request_lang("en")


async def test_the_live_panel_keeps_its_header_while_polling(site):
    from app.main import templates

    _collection(N(1))
    g = asyncio.Event()
    st = site({N(1): [g, PAGE]})
    assert vpn_check.start() is None
    await _until(lambda: st.factory.transports.get(N(1)) and st.factory.transports[N(1)].calls == 1)
    html = templates.get_template("settings/_vpn_test.html").render(vpn_note=None)
    rows = html.index('id="vpn-test-rows"')
    assert html.index("Stop") < rows and html.index('aria-live="polite"') < rows      # outside what the poll swaps
    assert 'id="vpn-test-progress" aria-live="polite">Testing Fake<' in html
    assert 'hx-select-oob="#vpn-test-progress:innerHTML,#vpn-test-clock:innerHTML"' in html
    assert html.count("hx-trigger") == 1 and "exit 1" in html and "0 tried" not in html
    g.set()
    await vpn_check._run.task
    html = templates.get_template("settings/_vpn_test.html").render(vpn_note=None)
    assert "hx-trigger" not in html and "vpn-test-progress" not in html


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
            assert 'id="vpn-sites" data-refresh-on-lane="vpn"' in page and "every try is a real search" in page
            assert 'id="vpn-test-rows"' not in page                      # no rows before the first test

            r = c.post("/settings/vpn/test", headers={"HX-Request": "true"})
            assert r.status_code == 200 and "<html" not in r.text and 'id="vpn-test"' in r.text
            _wait_test()
            panel = c.get("/settings/vpn/test").text
            assert f"Works through {N(1)}" in panel and "Exits tried (1)" in panel and "every 2s" not in panel
            assert "Test again" in panel and f"Exit now: {N(1)}" in c.get("/settings/vpn").text
            poll = c.get("/settings/vpn/test", headers={"HX-Request": "true", "HX-Target": "vpn-test-rows"})
            assert (poll.headers["HX-Retarget"], poll.headers["HX-Reselect"]) == ("#vpn-test", "#vpn-test")
            assert "HX-Retarget" not in c.get("/settings/vpn/test", headers={"HX-Request": "true"}).headers
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
