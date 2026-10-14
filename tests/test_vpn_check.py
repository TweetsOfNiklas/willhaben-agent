"""Shuffles of the VPN connection and the test after each (app/services/vpn_check.py): every site through the new
server, the next one when a site refuses, the best one with the failing sites paused, triggers and their guards,
Geizhals, fail closed, profiles, budgets, the panel. Fake wireproxy (FakeRunner) and fake transports only: no tunnel
ever starts and no site is asked."""
import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import platforms
from app.db import get_setting, set_setting
from app.platforms import PlatformError, vpn
from app.platforms.base import Platform
from app.platforms.httpclient import FetchResponse, HttpPlatformClient, TransportError
from app.platforms.ratelimit import RateLimiter
from app.platforms.types import SearchPage, SearchSpec
from app.services import geizhals, jobs, vpn_check
from tests.test_vpn import FakeRunner, _collection, _conf

JSON, HTML = {"content-type": "application/json"}, {"content-type": "text/html"}
PAGE = (200, b'{"results": []}', JSON)
BOT = (200, b"<html>Just a moment...</html>", HTML)
NO = (403, b"<html>cf</html>", HTML)
N = lambda i: f"at-vie-wg-{i:03d}"                      # noqa: E731


@pytest.fixture(autouse=True)
def no_source():
    """Every test starts and ends without uploaded configs (DATA_DIR lives for the whole run)."""
    vpn.remove_source()
    yield
    vpn.remove_source()


@pytest.fixture
def fake(monkeypatch):
    """wireproxy without wireproxy (tests/test_vpn.py); the "random" pick takes the first by name."""
    r = FakeRunner()
    monkeypatch.setattr(vpn, "runner", r)
    monkeypatch.setattr(vpn, "_choice", lambda names: sorted(names)[0])
    return r


class ExitTransport:
    """One server's answers in order: (status, body, headers), an Exception, or an asyncio.Event to wait for first."""

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


def _server(runner, proxy):
    return runner.ports[int(proxy.rsplit(":", 1)[1])]


def exit_factory(runner, answers, built):
    """transport_factory: the route's port -> its server's transport; a direct transport (None) fails the test."""
    transports = {}

    def factory(proxy):
        assert proxy is not None, "a request went out directly"
        name = _server(runner, proxy)
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
        self.client = HttpPlatformClient(self.id, RateLimiter(0, per_hour, name=self.name), name=self.name,
                                         transport_factory=self.factory)

    @property
    def limiter(self):
        return self.client.limiter

    def parse_search_url(self, url):
        raise NotImplementedError

    def describe(self, spec):
        return "fake"

    def keyword_search(self, keyword, price_from=None, price_to=None, *, category=None, sort="newest"):
        return SearchSpec(platform=self.id, url=f"https://{self.api_host}/s?q={keyword}", keyword=keyword)

    async def search(self, spec, *, rows=None, cache_s=0):
        r = await self.client._get(spec.url, kind="search")
        if r.status_code != 200:
            raise PlatformError(f"{self.name} search returned HTTP {r.status_code}")
        if b"results" not in r.content:
            raise PlatformError(f"{self.name} answered without a result list")
        return SearchPage(rows_found=0, ads=[])

    def ad_url(self, ad_id):
        return ad_id


class FakeSite2(FakeSite):
    id, name, api_host = "fake2", "Fake 2", "api.fake2.example"


@pytest.fixture
def sites(fake):
    """sites(answers of "fake", answers of "fake2", ...): fake sites with the VPN on, each {server: answers}."""
    def make(*answers, per_hour=()):
        out = []
        for i, (cls, ans) in enumerate(zip((FakeSite, FakeSite2), answers, strict=False)):
            s = cls(fake, ans, per_hour[i] if i < len(per_hour) else 0)
            platforms.set_platform(s.id, s)
            vpn.set_enabled(s.id, True)
            out.append(s)
        return out

    yield make
    platforms.reset()


def gz(monkeypatch, runner, answers):
    """Geizhals with its VPN switch on, answering per server: {server: [(status, text) or an Exception]}; requests
    without a proxy are noted as "direct". Returns what was asked, in order."""
    seen = []

    def client(proxy):
        name = _server(runner, proxy) if proxy else "direct"

        def handler(req):
            seen.append(name)
            a = answers[name].pop(0)
            if isinstance(a, Exception):
                raise a
            return httpx.Response(a[0], text=a[1], headers={"content-type": "text/html"})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(geizhals, "_client", client)
    monkeypatch.setattr(geizhals, "_limiter", geizhals.RateLimiter(0, name="Geizhals"))
    vpn.set_enabled(vpn.GEIZHALS, True)
    return seen


async def _shuffle(trigger="manual") -> vpn_check.Shuffle:
    assert vpn_check.start(trigger) is None
    sh = vpn_check._run
    await sh.task
    return sh


async def _until(cond, s=2.0):
    end = time.monotonic() + s
    while not cond():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.01)


def _reasons(pid="fake"):
    return {n: r for n, (_, r) in vpn.marks(pid).items()}


def _gap_passed():
    """As if the last shuffle ended long enough ago for an automatic one."""
    vpn.update_conn(shuffled=time.time() - vpn_check.AUTO_GAP_S - 1)


# ------------------------------------------------------------------ the shuffle and its test
async def test_a_shuffle_keeps_the_first_server_every_site_answers_and_all_share_it(sites, fake):
    _collection(N(1), N(2), N(3))
    a, b = sites({N(1): [NO], N(2): [PAGE, PAGE]}, {N(1): [PAGE], N(2): [PAGE, PAGE]})
    sh = await _shuffle()
    assert (sh.status, sh.kept) == ("done", N(2)) and [s.name for s in sh.servers] == [N(1), N(2)]
    first = sh.servers[0].answers
    assert (first["fake"].verdict, first["fake"].detail, first["fake2"].verdict) == ("blocked", "HTTP 403", "ok")
    assert fake.starts == [N(1), N(2)] and fake.most == 1                 # one process at a time
    assert fake.probes == [(N(1), "api.fake.example"), (N(2), "api.fake.example")]
    assert _reasons("fake") == {N(1): "blocked"} and _reasons("fake2") == {}
    st = vpn.conn_state()
    assert st["server"] == N(2) and st["paused"] == {} and st["shuffled"] >= sh.started and not vpn.conn.busy
    assert a.limiter.cooling_down_s == 0                                   # a refused server never pauses the site

    await a.search(a.keyword_search("x"))                                  # live requests: the same one process
    await b.search(b.keyword_search("x"))
    assert a.client.proxy == b.client.proxy == vpn.conn.current().proxy and fake.starts == [N(1), N(2)]

    job = jobs.snapshot()["recent"][0]
    assert (job["label"], job["lane"], job["link"], job["result"]) == (
        "VPN server change", "vpn", "/settings/vpn", f"{N(2)}: all 2 sites answer")
    vpn_check._run = None                                                  # as after a restart: from the setting
    v = vpn_check.view()
    assert [(r["name"], r["label"]) for r in v["rows"]] == [("Fake", "Works"), ("Fake 2", "Works")]
    assert [(s["name"], s["kept"]) for s in v["servers"]] == [(N(1), False), (N(2), True)]
    assert v["servers"][0]["parts"] == ["Fake: blocked by the site (HTTP\u00a0403)"] and v["trigger"] == "Started by you"


async def test_no_server_suits_all_the_best_stays_and_the_failing_site_pauses(sites, fake, monkeypatch):
    monkeypatch.setattr(vpn_check, "MAX_SERVERS", 3)
    _collection(N(1), N(2), N(3), N(4))
    a, b = sites({N(1): [NO], N(2): [NO], N(3): [(429, b"", HTML)]},
                 {N(1): [PAGE, PAGE], N(2): [TransportError("reset")], N(3): [PAGE]})
    sh = await _shuffle()
    assert sh.kept == N(1) and [s.name for s in sh.servers] == [N(1), N(2), N(3)]
    assert fake.starts == [N(1), N(2), N(3), N(1)] and fake.most == 1      # up to 3, then the best starts again
    assert _reasons("fake") == {N(1): "blocked", N(2): "blocked", N(3): "rate"}
    assert _reasons("fake2") == {N(2): "unreachable"}
    assert vpn.conn_state()["paused"] == {"fake": ["blocked", "HTTP 403"]}

    built = len(a.built)
    with pytest.raises(PlatformError, match=f"Fake refused the VPN server {N(1)}"):
        await a.search(a.keyword_search("x"))                              # paused: fails closed ...
    assert len(a.built) == built and not vpn_check.running()               # ... and asks for no shuffle
    assert vpn.conn_state().get("pending") is None
    await b.search(b.keyword_search("x"))                                  # the other site goes on

    v = vpn_check.view()
    assert v["summary"] == f"{N(1)} stays, the best of 3 servers: 1 of 2 sites answer" and v["tone"] == "warn"
    rows = {r["pid"]: r for r in v["rows"]}
    assert (rows["fake"]["label"], rows["fake"]["tone"]) == ("Refused", "bad")
    assert rows["fake"]["message"] == "Blocked by the site (HTTP\u00a0403). Its requests pause until the next shuffle."
    assert rows["fake2"]["label"] == "Works"
    assert [s["label"] for s in v["servers"]] == ["1 of 2 answer", "0 of 2 answer", "1 of 2 answer"]
    assert jobs.snapshot()["recent"][0]["problems"] == 1
    assert vpn.health() == ("problem", f"Fake paused: refused {N(1)}")


async def test_a_block_between_shuffles_triggers_one_shuffle_not_a_storm(sites, fake):
    _collection(N(1), N(2), N(3))
    a, b = sites({N(1): [PAGE, NO], N(2): [PAGE, PAGE], N(3): [PAGE]}, {N(1): [PAGE], N(2): [PAGE, NO], N(3): [PAGE]})
    sh1 = await _shuffle()
    assert sh1.kept == N(1)
    _gap_passed()
    await a.search(a.keyword_search("x"))              # 403 through N1: a shuffle, the request waits and asks again
    sh2 = vpn_check._run
    assert sh2 is not sh1 and (sh2.trigger, sh2.by, sh2.why, sh2.detail) == ("auto", "fake", "blocked", "HTTP 403")
    assert sh2.kept == N(2) and a.client.proxy == vpn.conn.current().proxy and vpn.paused("fake") is None
    assert _reasons("fake") == {N(1): "blocked"} and vpn_check.view()["trigger"] == (
        f"Fake: {N(1)} blocked by the site (HTTP\u00a0403)")

    with pytest.raises(PlatformError, match="refused the VPN server"):
        await b.search(b.keyword_search("x"))          # the other site refused it right away: the guard lets it wait
    assert vpn_check._run is sh2 and not vpn_check.running() and vpn.paused("fake2") == ("blocked", "HTTP 403")
    pending = vpn.conn_state()["pending"]
    assert pending[1:] == ["fake2", "blocked", "HTTP 403"]
    assert pending[0] == pytest.approx(vpn.conn_state()["shuffled"] + vpn_check.AUTO_GAP_S)
    assert vpn_check.view()["pending"] == "Brought forward: Fake 2 refused the server (blocked by the site)."
    await vpn_check.tick()
    assert vpn_check._run is sh2                       # not its turn yet
    _gap_passed()
    vpn.update_conn(pending=[time.time() - 1, *pending[1:]])
    await vpn_check.tick()
    sh3 = vpn_check._run
    await sh3.task
    assert (sh3.trigger, sh3.by, sh3.kept) == ("auto", "fake2", N(3)) and vpn.paused("fake2") is None
    assert vpn.conn_state()["pending"] is None and len(vpn.conn_state()["auto"]) == 2


async def test_a_site_starts_one_automatic_shuffle_between_two_regular_ones(sites, fake):
    _collection(N(1), N(2), N(3))
    a, b = sites({N(1): [PAGE, NO], N(2): [PAGE, PAGE, NO], N(3): [PAGE]}, {N(1): [PAGE], N(2): [PAGE], N(3): [PAGE]})
    await _shuffle()
    _gap_passed()
    await a.search(a.keyword_search("x"))              # refused N1: one automatic shuffle, N2 answers
    sh2 = vpn_check._run
    assert (sh2.trigger, sh2.by, sh2.kept) == ("auto", "fake", N(2))
    _gap_passed()
    with pytest.raises(PlatformError, match="refused the VPN server"):
        await a.search(a.keyword_search("x"))          # answers the test, refuses live again: no second one
    assert vpn_check._run is sh2 and vpn.conn_state().get("pending") is None and vpn.paused("fake")
    assert _reasons("fake") == {N(1): "blocked", N(2): "blocked"}
    sh3 = await _shuffle()                             # the next regular one tests it again
    assert sh3.kept == N(3) and vpn.paused("fake") is None and vpn.conn_state()["triggered"] == {}


async def test_a_site_whose_pages_changed_does_not_keep_the_shuffles_going(fake, monkeypatch):
    monkeypatch.setattr(vpn_check, "MAX_SERVERS", 4)

    class Picky(FakeSite):
        """Judges its pages like eBay (`valid`): a changed layout looks like a bot check through every server."""

        async def search(self, spec, *, rows=None, cache_s=0):
            r = await self.client._get(spec.url, kind="search", valid=lambda r: b"results" in r.content)
            if b"results" not in r.content:
                raise PlatformError("Fake answered without a result list")
            return SearchPage(rows_found=0, ads=[])

    _collection(*(N(i) for i in range(1, 5)))
    s = Picky(fake, {N(i): [BOT] * 3 for i in range(1, 5)})
    platforms.set_platform(s.id, s)
    vpn.set_enabled(s.id, True)
    try:
        sh = await _shuffle()
        assert sh.kept == N(3) and vpn.conn_state()["odd"] == ["fake"] and vpn.paused("fake") is None
        _gap_passed()
        with pytest.raises(PlatformError, match="without a result list"):
            await s.search(s.keyword_search("x"))      # the same page live: the site's error, no shuffle
        assert vpn_check._run is sh and vpn.paused("fake") is None and set(vpn.marks("fake")) == {N(1), N(2)}
    finally:
        platforms.reset()


async def test_a_restart_during_an_automatic_shuffle_runs_it_again(sites, fake):
    _collection(N(1), N(2), N(3))
    g = asyncio.Event()
    a, b = sites({N(1): [PAGE, NO], N(2): [g, PAGE]}, {N(1): [PAGE], N(2): [PAGE, PAGE]})
    await _shuffle()
    _gap_passed()
    shuffled = vpn.conn_state()["shuffled"]
    task = asyncio.create_task(a.search(a.keyword_search("x")))    # refused: paused, an automatic shuffle
    await _until(lambda: a.factory.transports.get(N(2)) and a.factory.transports[N(2)].calls == 1)
    await vpn_check.shutdown()                                      # the app stops while it tests N2
    await vpn.shutdown()
    with pytest.raises(PlatformError):
        await task
    st = vpn.conn_state()
    assert st["shuffled"] == shuffled and vpn.paused("fake")       # not done: the schedule does not move on
    assert st["pending"][1:] == ["fake", "blocked", "HTTP 403"] and st["pending"][0] <= time.time()
    vpn.reset()                                                     # the restart
    vpn_check.reset()
    await vpn_check.tick()                                          # the first tick runs it again
    sh = vpn_check._run
    await sh.task
    assert (sh.trigger, sh.by, sh.kept) == ("auto", "fake", N(2)) and vpn.paused("fake") is None


async def test_the_pick_ignores_the_marks_of_a_site_the_best_server_failed(sites, fake, monkeypatch):
    monkeypatch.setattr(vpn_check, "MAX_SERVERS", 2)
    _collection(N(1), N(2), N(3), "de-fra-wg-001")
    a, b = sites({N(1): [NO], N(2): [NO, NO], N(3): [NO]}, {N(1): [PAGE], N(2): [PAGE, PAGE], N(3): [PAGE]})
    sh = await _shuffle()
    assert sh.kept == N(1) and vpn.conn_state()["failing"] == ["fake"] and set(vpn.marks("fake")) == {N(1), N(2)}
    sh = await _shuffle()                              # Fake refuses everything: its marks do not push the others off
    assert sh.servers[0].name == N(2)                  # Austria, despite Fake's mark on it


def test_automatic_shuffles_keep_a_gap_and_a_cap_per_hour():
    now = 100_000.0
    assert vpn_check._auto_at({}, now) == vpn_check.AUTO_GAP_S
    st = {"shuffled": now - 60, "auto": [now - 3000]}
    assert vpn_check._auto_at(st, now) == now - 60 + vpn_check.AUTO_GAP_S
    st = {"shuffled": now - 1000, "auto": [now - 3000, now - 2000, now - 1500]}
    assert vpn_check._auto_at(st, now) == now - 3000 + 3600               # three in the hour: the oldest leaves it
    st["auto"][0] = now - 4000
    assert vpn_check._auto_at(st, now) == now - 1000 + vpn_check.AUTO_GAP_S


async def test_the_schedule_survives_a_restart(sites, fake):
    _collection(N(1), N(2))
    (a,) = sites({N(1): [PAGE, PAGE], N(2): [PAGE]})
    await vpn_check.tick()                             # never shuffled: due at once
    sh = vpn_check._run
    await sh.task
    assert (sh.trigger, sh.kept) == ("schedule", N(1))
    assert vpn_check.next_at() == pytest.approx(vpn.conn_state()["shuffled"] + 6 * 3600)
    await vpn_check.tick()
    assert vpn_check._run is sh                        # not due
    vpn_check.set_interval(2)
    assert vpn_check.interval_h() == 2

    await vpn.shutdown()                               # a restart: no process, no shuffle in memory
    vpn.reset()
    vpn_check.reset()
    vpn.update_conn(shuffled=time.time() - 3600)
    await vpn_check.tick()
    assert vpn_check._run is None                      # 1 h of 2: the stored time counts
    await a.search(a.keyword_search("x"))              # the stored server comes back, untested and unshuffled
    assert fake.starts == [N(1), N(1)] and vpn_check._run is None
    assert vpn_check.view()["server"] == N(1) and vpn_check.view()["run"]   # the last shuffle from the setting
    vpn.update_conn(shuffled=time.time() - 3 * 3600)
    await vpn_check.tick()
    sh2 = vpn_check._run
    await sh2.task
    assert (sh2.trigger, sh2.before, sh2.kept) == ("schedule", N(1), N(2))   # another server, not the same again


async def test_fail_closed_during_and_after_a_failed_shuffle(sites, fake, monkeypatch):
    monkeypatch.setattr(vpn, "SHUFFLE_WAIT_S", 0.05)
    _collection(N(1), N(2))
    g = asyncio.Event()
    a, b = sites({N(1): [g, NO], N(2): [NO]}, {N(1): [NO], N(2): [NO]})
    assert vpn_check.start() is None
    await _until(lambda: a.factory.transports.get(N(1)) and a.factory.transports[N(1)].calls == 1)
    with pytest.raises(PlatformError, match="changing its server"):
        await b.search(b.keyword_search("x"))          # a live request meanwhile: waits a little, then fails closed
    await _until(lambda: b.factory.transports.get(N(1)) and b.factory.transports[N(1)].calls == 1)
    g.set()
    sh = vpn_check._run
    await sh.task
    assert sh.status == "failed" and sh.kept is None and vpn.conn.current() is None and fake.alive() == 0
    assert b.factory.transports[N(1)].calls == 1       # only the test's request went out, through the VPN
    st = vpn.conn_state()
    assert st["server"] is None and st["error"] == "No server works: none of the 2 tried answers any site"

    with pytest.raises(PlatformError, match="No working VPN server for Fake right now"):
        await a.search(a.keyword_search("x"))          # no server: asks for one, which has to wait its turn
    assert vpn.conn_state()["pending"][1:3] == ["fake", "none"] and not vpn_check.running()
    v = vpn_check.view()
    assert (v["summary"], v["tone"]) == (st["error"], "bad") and not v["connected"]
    assert [r["message"] for r in v["rows"]] == ["Refused every server tried (2)."] * 2
    assert jobs.snapshot()["recent"][0]["problems"] == 1 and vpn.health() == ("problem", st["error"])


async def test_a_collection_with_servers_outside_the_eu_never_uses_them(sites, fake):
    _collection("ch-zrh-wg-001", "gb-lon-wg-001", "al-tia-wg-001", N(1))
    sites({N(1): [NO]}, {N(1): [NO]})
    sh = await _shuffle()
    assert [s.name for s in sh.servers] == [N(1)] and fake.starts == [N(1)]   # no fallback to ch, gb or al
    assert sh.status == "failed" and sh.reason == "none"
    vpn.remove_source()
    _collection("ch-zrh-wg-001", "gb-lon-wg-001")
    sh = await _shuffle()
    assert (sh.status, sh.reason, sh.servers) == ("failed", "no_eu", [])
    assert vpn_check.view()["summary"] == "No server works: the collection has no EU server left to try"
    assert not {"ch-zrh-wg-001", "gb-lon-wg-001", "al-tia-wg-001"} & set(fake.starts)


async def test_a_profile_is_tested_and_never_swapped(sites, fake):
    vpn.import_upload("home.conf", _conf().encode())
    a, b = sites({"home": [NO, PAGE]}, {"home": [PAGE, PAGE]})
    sh = await _shuffle()
    assert sh.profile and [s.name for s in sh.servers] == ["home"] and sh.kept == "home"
    assert vpn.paused("fake") == ("blocked", "HTTP 403") and vpn.paused("fake2") is None
    assert vpn_check.view()["summary"] == "home: 1 of 2 sites answer"
    sh = await _shuffle()                              # the next test asks the profile again despite its mark
    assert sh.kept == "home" and vpn.paused("fake") is None and fake.starts == ["home"]
    assert jobs.snapshot()["recent"][0]["label"] == "VPN test"


async def test_a_server_nobody_could_be_asked_through_stays_untested(sites, fake):
    _collection(N(1), N(2))
    a, b = sites({N(1): [NO]}, {N(1): [NO]}, per_hour=(10, 10))
    a.limiter._recent.extend([time.monotonic()] * 9)
    b.limiter._recent.extend([time.monotonic()] * 9)
    sh = await _shuffle()
    assert sh.kept == N(1) and len(sh.servers) == 1 and a.built == b.built == []
    v = vpn_check.view()
    assert v["summary"] == f"{N(1)} stays untested: no site could be asked this time"
    assert v["servers"][0]["label"] == "not tested" and vpn.conn_state()["paused"] == {}


async def test_a_sites_hourly_budget_caps_its_tests(sites, fake):
    _collection(*(N(i) for i in range(1, 6)))
    a, b = sites({N(i): [NO] for i in range(1, 6)}, {N(i): [PAGE] for i in range(1, 6)}, per_hour=(8,))
    sh = await _shuffle()
    assert sh.kept == N(3) and a.limiter.last_hour == 2 and a.built == [N(1), N(2)]   # a quarter of 8
    ans = sh.servers[2].answers["fake"]
    assert ans.verdict == "budget" and ans.params["why"] == "quarter" and vpn.paused("fake") is None
    row = vpn_check.view()["rows"][0]
    assert (row["label"], row["tone"]) == ("Not tested", "")
    assert row["message"] == ("Not tested: one shuffle sends Fake at most a quarter of its 8 requests per hour and "
                              "half of what is left (2 sent).")

    a.limiter._recent.extend([time.monotonic()] * 6)  # the agents used the rest of the hour
    sh = await _shuffle()
    assert sh.servers[0].answers["fake"].verdict == "budget" and a.limiter.last_hour == 8
    assert vpn_check.view()["rows"][0]["message"] == ("Not tested: Fake has used 8 of its 8 requests this hour; the "
                                                     "rest stays for the search agents.")


async def test_a_paused_limiter_is_not_tested_and_bot_pages_everywhere_are_the_sites(sites, fake, monkeypatch):
    monkeypatch.setattr(vpn_check, "MAX_SERVERS", 4)
    _collection(*(N(i) for i in range(1, 5)))
    a, b = sites({N(i): [BOT] for i in range(1, 5)}, {N(i): [PAGE] for i in range(1, 5)})
    b.limiter.penalize()
    b.limiter._cooldown_until = time.monotonic() + 600
    sh = await _shuffle()
    assert [sh.servers[i].answers["fake"].verdict for i in range(3)] == ["botcheck", "botcheck", "odd_page"]
    assert sh.kept == N(3) and _reasons("fake") == {N(1): "botcheck", N(2): "botcheck"}
    assert sh.servers[0].answers["fake2"].verdict == "paused" and b.built == []
    rows = {r["pid"]: r for r in vpn_check.view()["rows"]}
    assert rows["fake"]["label"] == "Site error" and "page the app does not know" in rows["fake"]["message"]
    assert rows["fake2"]["message"].startswith("Not tested: Fake 2 pause after a block")


async def test_a_site_error_is_an_answer(sites, fake):
    _collection(N(1), N(2))
    sites({N(1): [(503, b"{}", JSON)]}, {N(1): [PAGE]})
    sh = await _shuffle()
    assert sh.kept == N(1) and len(sh.servers) == 1 and vpn.marks("fake") == {}
    assert "That is not the server's fault" in vpn_check.view()["rows"][0]["message"]


# ------------------------------------------------------------------ Geizhals
async def test_geizhals_with_its_switch_on_is_tested_and_goes_through_the_server(sites, fake, monkeypatch):
    _collection(N(1), N(2))
    sites({N(1): [PAGE], N(2): [PAGE]})
    seen = gz(monkeypatch, fake, {N(1): [(403, "")], N(2): [(200, "<html></html>"), (200, "<html></html>")],
                                  "direct": [(200, "<html></html>")]})
    sh = await _shuffle()
    assert sh.kept == N(2) and [s.pid for s in sh.sites] == ["fake", "geizhals"]
    assert sh.servers[0].answers["geizhals"].verdict == "blocked" and _reasons("geizhals") == {N(1): "blocked"}
    assert geizhals._limiter.cooling_down_s == 0      # a refused server is no block of the home IP
    assert await geizhals.search("x") == [] and seen == [N(1), N(2), N(2)]
    rows = {r["pid"]: r for r in vpn_check.view()["rows"]}
    assert (rows["geizhals"]["name"], rows["geizhals"]["label"]) == ("Geizhals", "Works")

    vpn.set_enabled(vpn.GEIZHALS, False)              # off: directly, as before, and no longer tested
    assert await geizhals.search("x") == [] and seen[-1] == "direct"
    assert [t.pid for t in vpn_check.targets()] == ["fake"]


async def test_geizhals_refusing_the_server_shuffles_without_pausing_geizhals(sites, fake, monkeypatch):
    _collection(N(1), N(2))
    sites({N(1): [PAGE], N(2): [PAGE]})
    seen = gz(monkeypatch, fake, {N(1): [(200, ""), (403, "")], N(2): [(200, "")]})
    sh = await _shuffle()
    _gap_passed()
    with pytest.raises(geizhals.GeizhalsError, match=r"Geizhals refused the VPN server \(HTTP 403\)"):
        await geizhals.search("x")
    sh2 = vpn_check._run
    assert sh2 is not sh and (sh2.by, sh2.why) == ("geizhals", "blocked")
    await sh2.task
    assert sh2.kept == N(2) and vpn.paused("geizhals") is None and geizhals._limiter.cooling_down_s == 0
    assert seen == [N(1), N(1), N(2)]


async def test_geizhals_queued_when_its_switch_goes_on_goes_through_the_vpn(fake, monkeypatch):
    _collection(N(1))
    vpn.update_conn(server=N(1))
    seen = gz(monkeypatch, fake, {N(1): [(200, "<html></html>")], "direct": []})
    vpn.set_enabled(vpn.GEIZHALS, False)
    async with geizhals._limiter:                  # another lookup holds the limiter ...
        task = asyncio.create_task(geizhals.search("x"))
        for _ in range(10):
            await asyncio.sleep(0)
        vpn.set_enabled(vpn.GEIZHALS, True)        # ... while the switch goes on
    assert await task == [] and seen == [N(1)]     # never directly


async def test_geizhals_fails_closed_without_a_server(fake, monkeypatch):
    seen = gz(monkeypatch, fake, {})
    monkeypatch.setattr(vpn, "_shuffle", lambda *a: False)
    with pytest.raises(geizhals.GeizhalsError, match="no WireGuard config"):
        await geizhals.search("x")
    _collection(N(1))
    with pytest.raises(geizhals.GeizhalsError, match="No working VPN server for Geizhals"):
        await geizhals.search("x")
    assert seen == [] and fake.starts == []


# ------------------------------------------------------------------ the real adapters' rules
async def test_kleinanzeigen_credentials_are_not_the_servers_fault(fake, request):
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
    sh = await _shuffle()
    ans = sh.servers[0].answers["kleinanzeigen"]
    assert sh.kept == N(1) and ans.verdict == "site_error" and "app credentials" in ans.detail
    assert vpn.marks("kleinanzeigen") == {} and built == [N(1)]
    row = vpn_check.view()["rows"][0]
    assert row["action"] == {"label": "Open Kleinanzeigen settings", "href": "/settings/kleinanzeigen"}


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
    monkeypatch.setattr(ebay.ebay_limiter(), "per_hour", 40)             # pinned, not the default budget
    vpn.set_enabled("ebay", True)
    return c, built


async def test_a_timeout_of_ebays_description_keeps_the_server(fake, request, monkeypatch):
    from tests.conftest import FIXTURES

    _collection(N(1), N(2))
    page, item = (FIXTURES / "ebay_search.html").read_bytes(), (FIXTURES / "ebay_item.html").read_bytes()
    c, built = _ebay(fake, request, monkeypatch, {N(1): [(200, page, HTML), (200, item, HTML),
                                                          TransportError("Operation timed out")]})
    sh = await _shuffle()
    _gap_passed()
    d = await c.item("920036165580")                 # the description (itm.ebaydesc.com) does not answer
    assert d is not None and c._factory.transports[N(1)].calls == 3        # the ad shows without it
    assert vpn_check._run is sh and vpn.paused("ebay") is None and vpn.marks("ebay") == {}


async def test_ebay_warms_up_on_a_server_and_sends_at_most_three_requests_there(fake, request, monkeypatch):
    from app.platforms import ebay
    from tests.conftest import FIXTURES

    _collection(N(1), N(2))
    page = (FIXTURES / "ebay_search.html").read_bytes()
    c, built = _ebay(fake, request, monkeypatch, {N(1): [(403, b"", HTML)] * 4,
                                                  N(2): [(403, b"", HTML), (200, page, HTML)]})
    sh = await _shuffle()
    one, two = (sh.servers[i].answers["ebay"] for i in (0, 1))
    assert (one.verdict, one.requests, two.verdict, two.requests) == ("blocked", 3, "ok", 2)
    assert sh.kept == N(2) and c._warm and c._factory.transports[N(1)].calls == 3
    assert _reasons("ebay") == {N(1): "blocked"} and ebay.ebay_limiter().cooling_down_s == 0


# ------------------------------------------------------------------ cancel, shutdown
async def test_new_configs_or_shutdown_cancel_the_shuffle(sites, fake):
    _collection(N(1))
    g = asyncio.Event()
    (a,) = sites({N(1): [g, PAGE]})
    assert vpn_check.start() is None
    assert vpn_check.start() == "The VPN is changing its server"          # one at a time
    await _until(lambda: a.factory.transports.get(N(1)) and a.factory.transports[N(1)].calls == 1)
    await vpn_check.cancel("uploaded")
    sh = vpn_check._run
    assert (sh.status, sh.reason) == ("stopped", "uploaded") and not vpn.conn.busy and vpn.conn.current() is None
    assert fake.alive() == 0 and a.client._transport is None               # the half-used transport is dropped
    assert json.loads(get_setting("vpn_test"))["status"] == "stopped"
    assert vpn_check.view()["summary"] == "Stopped: new configs were uploaded"

    a.factory.transports[N(1)].answers[:] = [g, PAGE]
    g.clear()
    assert vpn_check.start() is None
    await _until(lambda: a.factory.transports[N(1)].calls == 2)
    await vpn_check.shutdown()
    await vpn.shutdown()
    assert vpn_check._run.status == "interrupted" and fake.alive() == 0 and not vpn.conn.busy

    stored = json.loads(get_setting("vpn_test"))                           # a hard kill: stored while it ran
    stored["status"] = "running"
    set_setting("vpn_test", json.dumps(stored))
    vpn_check._run = None
    assert vpn_check.view()["summary"] == "Interrupted by a restart of the app"


async def test_removing_configs_stops_the_process_even_when_the_shuffle_fails_to_stop(monkeypatch):
    from app.main import settings_vpn_clear

    stopped = []

    async def broken(reason):
        raise RuntimeError("database is locked")

    async def process_stop():
        stopped.append(True)

    monkeypatch.setattr(vpn_check, "cancel", broken)
    monkeypatch.setattr(vpn, "source_changed", process_stop)
    with pytest.raises(RuntimeError):
        await settings_vpn_clear()
    assert stopped == [True]


async def test_preconditions(sites, fake, monkeypatch):
    from app.config import get_settings

    assert vpn_check.blocker() == "Upload WireGuard configs first"
    _collection(N(1))
    assert vpn_check.blocker() == "Switch the VPN on for a site first"
    assert vpn_check.trigger("fake", "blocked") is False and vpn_check._run is None
    fake.installed = False
    assert vpn_check.blocker() == "wireproxy is not installed"
    fake.installed = True
    monkeypatch.setattr(get_settings(), "wh_replay", True)
    assert "offline replay" in vpn_check.blocker()
    monkeypatch.setattr(get_settings(), "wh_replay", False)
    sites({N(1): [PAGE]})
    assert vpn_check.blocker() is None


# ------------------------------------------------------------------ the settings page
async def test_the_live_panel_polls_its_body_and_keeps_its_header(sites, fake):
    from app.main import templates

    _collection(N(1), N(2))
    g = asyncio.Event()
    a, b = sites({N(1): [g, PAGE]}, {N(1): [PAGE]})
    assert vpn_check.start() is None
    await _until(lambda: a.factory.transports.get(N(1)) and a.factory.transports[N(1)].calls == 1
                 and b.factory.transports.get(N(1)) and b.factory.transports[N(1)].calls == 1)
    v = vpn_check.view()
    assert v["live"] and v["progress"] == f"Server 1 of up to 10: {N(1)}"
    assert [(r["spinner"], r["label"]) for r in v["rows"]] == [(True, ""), (False, "Works")]
    assert v["rows"][0]["message"] == "" and v["rows"][1]["message"] == ""          # chips: nothing to say yet
    html = templates.get_template("settings/_vpn_test.html").render(vpn_note=None)
    body = html.index('id="vpn-test-body"')
    assert html.index('aria-live="polite"') < body and html.count('hx-trigger="every 2s"') == 1
    assert 'hx-select-oob="#vpn-test-progress:innerHTML,#vpn-test-clock:innerHTML"' in html
    assert "<details" not in html and " disabled>" in html                # no "Shuffle now" while one runs
    assert ">asking</span>" in html and "Reason: Started by you" in html  # a spinner says what it does; the cause
    g.set()
    await vpn_check._run.task
    html = templates.get_template("settings/_vpn_test.html").render(vpn_note=None)
    assert "hx-trigger" not in html and "Servers tried (1)" in html and "Shuffle now" in html


async def test_the_panel_in_german(sites, fake):
    from app.i18n import set_request_lang
    from app.main import templates

    _collection(N(1), N(2))
    sites({N(1): [NO], N(2): [PAGE]}, {N(1): [PAGE], N(2): [PAGE]})
    await _shuffle()
    set_request_lang("de")
    try:
        v = vpn_check.view()
        assert v["summary"] == f"{N(2)}: alle 2 Seiten antworten" and v["trigger"] == "Von dir gestartet"
        html = templates.get_template("settings/_vpn_test.html").render(vpn_note=None)
        assert "Anlass: Von dir gestartet" in html                               # the cause, not a result
        assert v["servers"][0]["parts"] == ["Fake: von der Seite gesperrt (HTTP\u00a0403)"]
        assert v["servers"][0]["label"] == "1 von 2 antworten"
    finally:
        set_request_lang("en")


def _wait_shuffle(timeout=5.0):
    """TestClient: the shuffle runs in the app's loop thread; wait until it ended (also before its job showed up)."""
    from tests.conftest import wait_jobs

    end = time.monotonic() + timeout
    while vpn_check.running() and time.monotonic() < end:
        time.sleep(0.02)
    assert not vpn_check.running(), "the shuffle still runs"
    wait_jobs()


def test_settings_page_shuffle_now_interval_and_panel(fake):
    from app.main import app

    _collection(N(1), N(2))
    platforms.set_platform("fake", FakeSite(fake, {N(1): [PAGE, PAGE], N(2): [PAGE]}))
    vpn.set_enabled("fake", True)
    try:
        with TestClient(app) as c:
            page = c.get("/settings/vpn").text
            assert 'id="vpn-test"' in page and "Shuffle now" in page and "every 2s" not in page
            assert "none yet: the first shuffle picks one" in page and 'value="6"' in page
            assert 'id="vpn-test-body"' not in page                         # no shuffle yet

            r = c.post("/settings/vpn/shuffle", headers={"HX-Request": "true"})
            assert r.status_code == 200 and "<html" not in r.text and 'id="vpn-test"' in r.text
            _wait_shuffle()
            panel = c.get("/settings/vpn/test").text
            assert f"{N(1)}: Fake answers" in panel and "Servers tried (1)" in panel and "every 2s" not in panel
            poll = c.get("/settings/vpn/test", headers={"HX-Request": "true", "HX-Target": "vpn-test-body"})
            assert (poll.headers["HX-Retarget"], poll.headers["HX-Reselect"]) == ("#vpn-test", "#vpn-test")
            set_setting("ui_lang", "de")
            assert "Jetzt wechseln" in c.get("/settings/vpn/test").text
            set_setting("ui_lang", "en")
            assert any(j["lane"] == "vpn" and j["label"] == "VPN server change" for j in c.get("/jobs").json()["recent"])

            r = c.post("/settings/vpn/interval", data={"hours": "3"}, follow_redirects=False)
            assert r.status_code == 303 and vpn_check.interval_h() == 3
            for bad in ("x", "²", "0", "999", "2.5"):
                r = c.post("/settings/vpn/interval", data={"hours": bad})
                assert r.status_code == 200 and "Enter whole hours, 1 to 168" in r.text
            assert vpn_check.interval_h() == 3                              # nothing clamped silently
            r = c.post("/settings/vpn/shuffle", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"].startswith("/settings/vpn?msg=Shuffle%20started")
            _wait_shuffle()
            assert vpn.conn_state()["server"] == N(2)
            assert "Blocks forgotten" in c.post("/settings/vpn/forget").text
            assert 'id="vpn-test"' not in c.post("/settings/vpn/clear").text
    finally:
        platforms.reset()
