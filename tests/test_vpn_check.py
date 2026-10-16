"""The VPN search (app/services/vpn_check.py): one server for every site or two that share them, found in the
background while the live connection keeps serving; fail fast, pacing within the sites' budgets, parallel tunnels,
cancelled slow asks, triggers and their guards, Geizhals, fail closed, profiles, the panel. Fake wireproxy
(FakeRunner) and fake transports only: no tunnel ever starts and no site is asked."""
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
    """wireproxy without wireproxy (tests/test_vpn.py); the "random" order within a tier is by name."""
    r = FakeRunner()
    monkeypatch.setattr(vpn, "runner", r)
    monkeypatch.setattr(vpn, "_shuffled", sorted)
    return r


@pytest.fixture
def serial(monkeypatch):
    """One tunnel and one server asked at a time: the steps in a fixed order."""
    monkeypatch.setattr(vpn, "SEARCH_TUNNELS", 1)
    monkeypatch.setattr(vpn_check, "ASK_PARALLEL", 1)


class ExitTransport:
    """One server's answers in order: (status, body, headers), an Exception, or an asyncio.Event to wait for first;
    then `default` (None: a TransportError, the request was not scripted)."""

    def __init__(self, answers, default=None):
        self.answers, self.default, self.calls, self.closed = list(answers), default, 0, False

    def _next(self):
        return self.answers.pop(0) if self.answers else self.default

    async def send(self, method, url, *, params=None, headers=None):
        self.calls += 1
        a = self._next()
        while isinstance(a, asyncio.Event):
            await a.wait()
            a = self._next()
        if a is None:
            raise TransportError("unscripted request")
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


def exit_factory(runner, answers, built, default=None):
    """transport_factory: the route's port -> its server's transport; a direct transport (None) fails the test."""
    transports = {}

    def factory(proxy):
        assert proxy is not None, "a request went out directly"
        name = _server(runner, proxy)
        built.append(name)
        return transports.setdefault(name, ExitTransport(answers.get(name, []), default))

    factory.transports = transports
    return factory


class FakeSite(Platform):
    id, name, api_host, home_country = "fake", "Fake", "api.fake.example", "AT"
    capabilities = frozenset({"keyword_search"})

    def __init__(self, runner, answers, per_hour=0, default=None, cost=1):
        self.built = []
        self.vpn_test_requests = cost
        self.factory = exit_factory(runner, answers, self.built, default)
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

    def calls(self, server):
        t = self.factory.transports.get(server)
        return t.calls if t else 0


class FakeSite2(FakeSite):
    id, name, api_host = "fake2", "Fake 2", "api.fake2.example"


class FakeSite3(FakeSite):
    id, name, api_host = "fake3", "Fake 3", "api.fake3.example"


class FakeSite4(FakeSite):
    id, name, api_host = "fake4", "Fake 4", "api.fake4.example"


@pytest.fixture
def sites(fake):
    """sites({server: answers} of "fake", of "fake2", ...): fake sites with the VPN on."""
    def make(*answers, per_hour=(), default=None, cost=()):
        out = []
        for i, (cls, ans) in enumerate(zip((FakeSite, FakeSite2, FakeSite3, FakeSite4), answers, strict=False)):
            s = cls(fake, ans, per_hour[i] if i < len(per_hour) else 0, default, cost[i] if i < len(cost) else 1)
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


def _live(*servers, **assign):
    """Live servers as an earlier search left them; `assign`: site -> server."""
    vpn.update_conn(servers=list(servers), assign=assign, since={s: time.time() - 600 for s in servers})


async def _search(trigger="manual") -> vpn_check.Search:
    assert vpn_check.start(trigger) is None
    sh = vpn_check._run
    await asyncio.wait_for(sh.task, 10)
    return sh


async def _until(cond, s=3.0):
    end = time.monotonic() + s
    while not cond():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.01)


def _reasons(pid="fake"):
    return {n: r for n, (_, r) in vpn.marks(pid).items()}


def _asked(sh, server):
    srv = next((s for s in sh.servers if s.name == server and not s.live), None)
    return {p: a.verdict for p, a in srv.answers.items()} if srv else {}


def _gap_passed():
    """As if the last search ended long enough ago for an automatic one."""
    vpn.update_conn(shuffled=time.time() - vpn_check.AUTO_GAP_S - 1)


# ------------------------------------------------------------------ in the background
async def test_a_server_for_all_is_found_in_the_background_while_the_old_one_keeps_serving(sites, fake):
    _collection(N(1), N(2), N(3))
    gate = asyncio.Event()
    a, b = sites({N(2): [gate, PAGE, PAGE]}, {N(1): [PAGE], N(2): [PAGE, PAGE]})
    _live(N(1), fake=N(1), fake2=N(1))
    assert vpn_check.start("manual") is None                       # a regular search: another server
    sh = vpn_check._run
    await _until(lambda: a.calls(N(2)) == 1)                       # Fake is asked through N2 (it hangs) ...
    await b.search(b.keyword_search("x"))                          # ... while Fake 2 searches through N1, at once
    assert b.client.proxy == vpn.conn.route(N(1)).proxy and vpn.live_servers() == [N(1)] and vpn_check.running()
    assert set(vpn.conn.testing()) <= {N(2), N(3)}                 # the search's own tunnels, never N1
    gate.set()
    await asyncio.wait_for(sh.task, 5)
    assert (sh.status, sh.plan["servers"], sh.switched) == ("done", [N(2)], 1)
    assert fake.running() == [N(2)] and fake.starts.count(N(2)) == 1   # taken over as it ran, N1 and N3 stopped
    await a.search(a.keyword_search("x"))
    await b.search(b.keyword_search("x"))
    assert a.client.proxy == b.client.proxy == vpn.conn.route(N(2)).proxy
    assert vpn.conn_state()["assign"] == {"fake": N(2), "fake2": N(2)} and vpn.since(N(2)) >= sh.started
    job = jobs.snapshot()["recent"][0]
    assert (job["label"], job["lane"], job["link"]) == ("VPN server search", "vpn", "/settings/vpn")
    assert job["result"] == f"{N(2)} for all 2 sites"
    vpn_check._run = None                                          # as after a restart: from the setting
    v = vpn_check.view()
    assert [(r["name"], r["label"]) for r in v["rows"]] == [("Fake", "Works"), ("Fake 2", "Works")]
    assert v["connections"][0]["name"] == N(2) and v["trigger"] == "Started by you"


async def test_a_refused_site_waits_without_an_error_while_the_others_go_on(sites, fake):
    _collection(N(1), N(2), N(3))
    gate = asyncio.Event()
    a, b = sites({N(1): [NO] * vpn.CONFIRM, N(2): [gate, PAGE, PAGE]}, {N(1): [PAGE, PAGE, PAGE], N(2): [PAGE, PAGE]})
    _live(N(1), fake=N(1), fake2=N(1))
    _gap_passed()
    with pytest.raises(vpn.VpnWait, match="its requests wait until the search finds another one"):
        await a.search(a.keyword_search("x"))                      # refused 3 times in a row: it waits, a search starts
    sh = vpn_check._run
    assert (sh.trigger, sh.by, sh.why, sh.detail) == ("auto", "fake", "blocked", "HTTP 403")
    await _until(lambda: a.calls(N(2)) == 1)
    with pytest.raises(vpn.VpnWait):
        await a.search(a.keyword_search("x"))                      # no waiting for the search: at once
    await b.search(b.keyword_search("x"))                          # the others keep their server meanwhile
    assert b.client.proxy == vpn.conn.route(N(1)).proxy
    gate.set()
    await asyncio.wait_for(sh.task, 5)
    assert sh.plan["servers"] == [N(2)] and vpn.paused("fake") is None   # N2 answers both: one server again
    await a.search(a.keyword_search("x"))
    assert a.client.proxy == vpn.conn.route(N(2)).proxy and fake.running() == [N(2)]


async def test_the_pickiest_site_is_asked_first_and_a_server_it_refuses_costs_one_request(sites, fake, serial):
    _collection(N(1), N(2), N(3))
    a, b, c = sites({N(3): [PAGE]}, {N(3): [PAGE]}, {N(2): [NO], N(3): [PAGE]})
    for old in ("de-old-wg-001", "de-old-wg-002"):                 # Fake 3 refused servers lately: the pickiest
        vpn.mark("fake3", old, "blocked")
    _live(N(1), fake=N(1), fake2=N(1), fake3=N(1))
    sh = await _search()
    assert _asked(sh, N(2)) == {"fake3": "blocked"}                # the others were not asked there
    assert a.calls(N(2)) == b.calls(N(2)) == 0 and N(2) not in a.built + b.built
    assert list(_asked(sh, N(3))) [0] == "fake3" and set(_asked(sh, N(3))) == {"fake", "fake2", "fake3"}
    assert sh.plan["servers"] == [N(3)] and _reasons("fake3")[N(2)] == "blocked"


async def test_every_eu_server_is_searched_no_cap(sites, fake):
    _collection(*(N(i) for i in range(1, 16)), "ch-zrh-wg-001", "gb-lon-wg-001")
    a, b = sites({N(i): [NO] for i in range(1, 14)} | {N(14): [PAGE, PAGE]}, {}, default=PAGE)
    sh = await _search()
    assert sh.status == "done" and sh.plan["servers"] == [N(14)]   # the 14th, far past the old cap of 10
    asked = {s.name for s in sh.servers if s.answers}
    assert {N(i) for i in range(1, 15)} <= asked
    assert not {"ch-zrh-wg-001", "gb-lon-wg-001"} & set(fake.starts)   # EU only
    assert fake.most <= vpn.MAX_LIVE + vpn.SEARCH_TUNNELS and fake.running() == [N(14)]
    assert len(fake.starts) == len(set(fake.starts))               # every server once


async def test_two_servers_share_the_sites_when_none_serves_all(sites, fake):
    _collection(N(1), N(2), N(3), N(4))
    a, b = sites({N(1): [PAGE, PAGE]}, {N(2): [PAGE, PAGE]}, default=NO)
    sh = await _search()
    assert sh.status == "done" and set(sh.plan["servers"]) == {N(1), N(2)}
    assert sh.plan["assign"] == {"fake": N(1), "fake2": N(2)} and sh.plan["uncovered"] == []
    await a.search(a.keyword_search("x"))
    await b.search(b.keyword_search("x"))
    assert a.client.proxy == vpn.conn.route(N(1)).proxy and b.client.proxy == vpn.conn.route(N(2)).proxy
    assert fake.running() == [N(1), N(2)] and len(vpn.conn.live) == 2      # two processes, no more
    assert fake.most <= vpn.MAX_LIVE + vpn.SEARCH_TUNNELS
    v = vpn_check.view()
    assert v["two"] and {c["name"]: c["sites"] for c in v["connections"]} == {N(1): "Fake", N(2): "Fake 2"}
    assert v["summary"].startswith("Two servers:") and vpn.health()[0] == "ok"


async def test_when_two_cannot_cover_all_the_best_pair_stays_and_the_rest_pauses_with_a_note(sites, fake):
    _collection(N(1), N(2), N(3))
    a, b, c = sites({N(1): [PAGE]}, {N(2): [PAGE]}, {N(3): [PAGE]}, default=NO)
    sh = await _search()
    assert sh.status == "done" and len(sh.plan["servers"]) == 2 and len(sh.plan["uncovered"]) == 1
    left = sh.plan["uncovered"][0]
    assert vpn.paused(left) == ("none", "3/3")
    site = {"fake": a, "fake2": b, "fake3": c}[left]
    with pytest.raises(vpn.VpnWait, match=f"No VPN server answers {site.name} right now"):
        await site.search(site.keyword_search("x"))                # fail closed, no agent error
    v = vpn_check.view()
    assert v["tone"] == "warn" and v["summary"].startswith(f"No server for {site.name} after 3 of 3 EU servers")
    row = next(r for r in v["rows"] if r["pid"] == left)
    assert (row["label"], row["tone"]) == ("No server", "bad") and "wait for the next search" in row["message"]
    note = next(s for s in vpn.status()["sites"] if s["id"] == left)["paused"]["note"]
    assert note.startswith("Paused: no VPN server answers. The last search checked 3 of 3 EU servers")
    assert vpn.health() == ("problem", f"{site.name} paused: no VPN server answers them")
    assert jobs.snapshot()["recent"][0]["problems"] == 1


async def test_a_server_one_site_refuses_still_records_the_others_and_becomes_a_partner(sites, fake, serial):
    """at-vie answered 6 of 7 sites while willhaben refused it in 0.3 s: those answers count."""
    _collection(N(1), N(2))
    slow = asyncio.Event()
    a, b, c = sites({N(1): [NO], N(2): [PAGE]}, {N(1): [slow, PAGE]}, {N(1): [slow, PAGE]}, default=NO)
    assert vpn_check.start("manual") is None
    sh = vpn_check._run
    await _until(lambda: a.calls(N(1)) == 1 and b.calls(N(1)) == 1 and c.calls(N(1)) == 1)
    await _until(lambda: _asked(sh, N(1)).get("fake") == "blocked")
    await asyncio.sleep(0.05)
    assert _asked(sh, N(1)) == {"fake": "blocked", "fake2": "asking", "fake3": "asking"}   # kept running
    slow.set()
    await asyncio.wait_for(sh.task, 5)
    assert _asked(sh, N(1)) == {"fake": "blocked", "fake2": "ok", "fake3": "ok"}
    assert sh.plan["assign"] == {"fake": N(2), "fake2": N(1), "fake3": N(1)}     # N1 serves the others
    assert set(vpn.oks("fake2")) == {N(1)} and _reasons("fake")[N(1)] == "blocked"


# ------------------------------------------------------------------ parallel and fast
async def test_tunnels_start_in_parallel_and_dead_servers_are_marked_only_once_confirmed(sites, fake):
    _collection(*(N(i) for i in range(1, 21)))
    fake.dead = {N(i) for i in range(1, 13)}                       # stale configs: no handshake
    held = asyncio.Event()
    for i in range(1, 21):
        fake.gates[N(i)] = held
    a, b = sites({N(13): [PAGE, PAGE]}, {N(13): [PAGE, PAGE]}, default=NO)
    assert vpn_check.start("manual") is None
    sh = vpn_check._run
    await _until(lambda: len(fake.procs) >= vpn.SEARCH_TUNNELS // 2)
    await asyncio.sleep(0.05)
    assert 2 <= len(vpn.conn.testing()) <= vpn.SEARCH_TUNNELS and fake.alive() == len(vpn.conn.testing())
    assert vpn.marks("fake") == {}                                 # nothing stored while they fail beside others
    held.set()
    await asyncio.wait_for(sh.task, 5)
    assert sh.plan["servers"] == [N(13)] and fake.running() == [N(13)]
    assert fake.most <= vpn.MAX_LIVE + vpn.SEARCH_TUNNELS
    dead = [s for s in sh.servers if s.problem]
    assert {s.name for s in dead} == fake.dead and all(s.detail == "no handshake in 5 s" for s in dead)
    marked = {n: r for n, (until, r) in vpn.marks("fake").items() if until > time.time() + 23 * 3600}
    assert len(marked) == vpn_check.MAX_DEAD_MARKS and set(marked.values()) == {"dead"}   # confirmed, capped
    assert sum(s.confirmed for s in dead) == vpn_check.MAX_DEAD_MARKS
    assert a.calls(N(1)) == 0                                      # a dead server costs no site request


# ------------------------------------------------------------------ the review's guards
async def test_a_connect_failure_counts_only_for_the_site_whose_host_it_was(sites, fake, serial):
    _collection(N(1), N(2))
    fake.unreachable = {N(1)}                                      # the handshake works, the first site's host not
    a, b = sites({N(2): [PAGE, PAGE]}, {N(1): [PAGE], N(2): [PAGE, PAGE]})
    sh = await _search()
    assert _asked(sh, N(1)) == {"fake": "unreachable", "fake2": "ok"}     # the other site was asked through it
    assert a.calls(N(1)) == 0 and N(1) not in vpn.marks("fake") and N(1) in vpn.oks("fake2")
    assert sh.plan["servers"] == [N(2)]


async def test_a_live_request_without_an_answer_marks_its_server_for_an_hour(sites, fake):
    _collection(N(1), N(2))
    (a,) = sites({N(1): [PAGE, TransportError("timed out")] + [TransportError("timed out")] * 2})
    _live(N(1), fake=N(1))
    vpn.update_conn(shuffled=time.time())                          # no search now (the gap)
    await a.search(a.keyword_search("x"))
    with pytest.raises(PlatformError):
        await a.search(a.keyword_search("x"))
    until, reason = vpn.marks("fake")[N(1)]
    assert reason == "unreachable" and time.time() + 3000 < until < time.time() + 3700


async def test_a_network_outage_writes_nothing_and_the_search_runs_again(sites, fake):
    names = [N(i) for i in range(1, 17)]
    _collection(*names)
    down = TransportError("All connection attempts failed")
    a, b = sites({n: [PAGE] * 4 for n in names}, {n: [PAGE] * 4 for n in names})
    _live(N(1), fake=N(1), fake2=N(1))
    await a.search(a.keyword_search("x"))
    await b.search(b.keyword_search("x"))
    fake.dead, fake.down = set(names), {N(1)}                      # the router reboots: no handshake, nothing through
    for s in (a, b):
        s.factory.transports[N(1)].answers[:] = [down] * 6
    _gap_passed()
    for s in (a, b):
        with pytest.raises(PlatformError):
            await s.search(s.keyword_search("x"))
    sh = vpn_check._run
    await asyncio.wait_for(sh.task, 10)
    assert (sh.status, sh.reason) == ("failed", "offline")
    assert vpn.marks("fake") == vpn.marks("fake2") == {}           # no server marked, not even the live one
    assert vpn.paused("fake") is None and vpn.paused("fake2") is None and vpn.live_servers() == [N(1)]
    assert vpn_check.pending()["at"] == pytest.approx(time.time() + vpn_check.OFFLINE_RETRY_S, abs=5)
    assert "internet connection seems down" in vpn_check.view()["summary"]
    fake.dead, fake.down = set(), set()                            # back two minutes later
    for s in (a, b):
        s.factory.transports[N(1)].answers[:] = [PAGE]
        await s.search(s.keyword_search("x"))                      # through the same server as before
        assert s.client.proxy == vpn.conn.route(N(1)).proxy


async def test_servers_dead_beside_a_working_live_one_are_asked_again_one_at_a_time(sites, fake, monkeypatch):
    names = [N(i) for i in range(1, 26)]
    _collection(*names)
    fake.dead = {N(i) for i in range(2, 22)}                       # 20 stale configs, N22 is fine
    a, b = sites({N(1): [PAGE, PAGE], N(22): [PAGE, PAGE]}, {N(1): [PAGE, PAGE], N(22): [PAGE, PAGE]},
                 default=NO)
    _live(N(1), fake=N(1), fake2=N(1))
    await a.search(a.keyword_search("x"))
    sh = await _search()
    assert sh.plan["servers"] == [N(22)] and sh.mode == "serial"   # the breaker: one at a time from then on
    dead = {n for n, (_u, r) in vpn.marks("fake").items() if r == "dead"}
    assert len(dead) == vpn_check.MAX_DEAD_MARKS and dead <= fake.dead   # confirmed on their own, at most ten
    assert sh.status == "done"


async def test_a_dead_live_server_is_no_outage_its_sites_move(sites, fake, serial):
    """The server in use went down (maintenance), the network is fine, and a few stale configs come early."""
    names = [N(i) for i in range(1, 20)]
    _collection(*names)
    a, b = sites({n: [PAGE] * 5 for n in names} | {N(2): [NO]}, {n: [PAGE] * 5 for n in names} | {N(2): [NO]})
    _live(N(1), fake=N(1), fake2=N(1))
    await a.search(a.keyword_search("x"))
    await b.search(b.keyword_search("x"))
    fake.dead, fake.down = {N(1), N(3), N(4), N(5)}, {N(1)}
    for s in (a, b):
        s.factory.transports[N(1)].answers[:] = [TransportError("timed out")] * 10
    _gap_passed()
    for s in (a, b):
        with pytest.raises(PlatformError):
            await s.search(s.keyword_search("x"))                  # no answer through N1: they wait, a search runs
    sh = vpn_check._run
    await asyncio.wait_for(sh.task, 10)
    assert (sh.status, sh.reason) == ("done", "") and sh.plan["servers"] == [N(6)]   # no "offline"
    assert vpn.paused("fake") is None and vpn.paused("fake2") is None
    for s in (a, b):
        await s.search(s.keyword_search("x"))
        assert s.client.proxy == vpn.conn.route(N(6)).proxy
    assert vpn.marks("fake")[N(1)][1] == "unreachable"            # the dead live server: an hour


async def test_the_network_dropping_while_dead_servers_are_confirmed_marks_none(sites, fake, monkeypatch):
    names = [N(i) for i in range(1, 41)]
    _collection(*names)
    dead = {N(i) for i in (9, 19, 29)}                             # stale, failing beside others (no breaker)
    fake.dead = set(dead)
    ans = {n: [NO] * 3 for n in names} | {N(1): [PAGE] * 5, N(40): [PAGE] * 3}
    a, b = sites(dict(ans), dict(ans))
    _live(N(1), fake=N(1), fake2=N(1))
    await a.search(a.keyword_search("x"))
    real = vpn_check._Walk.confirm_dead

    async def confirm(self):
        assert {s.name for s, _ in self.failures} == dead
        probe = fake.probe

        async def outage(handle, port, host):                    # the network drops right after the first check
            fake.dead, fake.down = set(names), set(names)
            return await probe(handle, port, host)

        monkeypatch.setattr(fake, "probe", outage)
        await real(self)

    monkeypatch.setattr(vpn_check._Walk, "confirm_dead", confirm)
    sh = await _search()
    day = [n for p in ("fake", "fake2") for n, (u, r) in vpn.marks(p).items() if r in ("dead", "broken")]
    assert sh.plan["servers"] == [N(40)] and day == []            # nothing marked dead for the outage


async def test_a_short_blip_of_the_network_marks_no_server(sites, fake):
    names = [N(i) for i in range(1, 31)]
    _collection(*names)
    a, b = sites({n: [NO] * 3 for n in names} | {N(1): [PAGE] * 9, N(30): [PAGE] * 3},
                 {n: [NO] * 3 for n in names} | {N(1): [PAGE] * 9, N(30): [PAGE] * 3})
    _live(N(1), fake=N(1), fake2=N(1))
    await a.search(a.keyword_search("x"))
    blip, probe = [9], fake.probe

    async def flaky(handle, port, host):                          # the next nine handshakes fail, then all work
        if blip[0] > 0:
            blip[0] -= 1
            fake.probes.append((fake.ports[port], host))
            return "handshake"
        return await probe(handle, port, host)

    fake.probe = flaky
    sh = await _search()
    day = [n for p in ("fake", "fake2") for n, (u, r) in vpn.marks(p).items() if r in ("dead", "broken")]
    assert sh.status == "done" and sh.plan["servers"] == [N(30)] and day == []   # failed, then answered again


async def test_a_walk_that_ends_without_handshakes_pauses_no_site_for_good(sites, fake):
    names = [N(i) for i in range(1, 7)]
    _collection(*names)
    fake.dead = set(names)                                         # no live server, every config without a handshake
    a, b = sites({}, {})
    sh = await _search()
    assert (sh.status, sh.reason) == ("failed", "retry")
    assert vpn.paused("fake") is None and vpn.paused("fake2") is None    # not "none" until the next regular one
    assert vpn_check.pending()["at"] == pytest.approx(time.time() + vpn_check.OFFLINE_RETRY_S, abs=5)
    assert "the search runs again in 5 min" in vpn_check.view()["summary"]


async def test_the_breaker_asks_the_server_a_request_dropped_before_the_search(sites, fake):
    names = [N(i) for i in range(1, 17)]
    _collection(*names)
    a, b = sites({n: [PAGE] * 3 for n in names}, {n: [PAGE] * 3 for n in names})
    _live(N(1), fake=N(1), fake2=N(1))                             # live, but not started yet (a restart)
    fake.dead = set(names)                                         # and the network is down
    _gap_passed()
    with pytest.raises(PlatformError):
        await a.search(a.keyword_search("x"))                      # N1 does not start: dropped, a search runs
    sh = vpn_check._run
    assert vpn.live_servers() == [] and sh.before == [N(1)]        # the search still knows it as the reference
    await asyncio.wait_for(sh.task, 10)
    assert (sh.status, sh.reason) == ("failed", "offline")
    assert vpn.live_servers() == [N(1)] and N(1) not in vpn.marks("fake")   # back as it was
    fake.dead = set()
    await a.search(a.keyword_search("x"))
    assert a.client.proxy == vpn.conn.route(N(1)).proxy


async def test_an_outage_keeps_what_the_sites_themselves_answered(sites, fake, serial):
    names = [N(i) for i in range(1, 20)]
    _collection(*names)
    a, b = sites({N(1): [PAGE] * 3, N(2): [NO]}, {N(1): [PAGE] * 3, N(2): [NO]})
    _live(N(1), fake=N(1), fake2=N(1))
    await a.search(a.keyword_search("x"))
    probe = fake.probe

    async def drop_after_n2(handle, port, host):
        if fake.ports[port] != N(2):                               # the network goes once N2 was asked
            fake.dead, fake.down = set(names), set(names)
        return await probe(handle, port, host)

    fake.probe = drop_after_n2
    sh = await _search()
    assert (sh.status, sh.reason) == ("failed", "offline")
    assert vpn.marks("fake")[N(2)][1] == "blocked"                 # a 403 of the site itself stays
    assert not [n for n, (_u, r) in vpn.marks("fake").items() if r in ("dead", "broken", "unreachable")]
    assert vpn.live_servers() == [N(1)]                            # the server in use stays


async def test_servers_out_only_for_not_answering_are_tried_when_nothing_else_is_left(sites, fake):
    _collection(N(1), N(2))
    for n in (N(1), N(2)):
        for pid in ("fake", "fake2"):
            vpn.mark(pid, n, "dead")                               # both marked dead lately
    a, b = sites({N(1): [PAGE, PAGE]}, {N(1): [PAGE, PAGE]})
    sh = await _search()
    assert sh.plan["servers"] == [N(1)] and N(1) in fake.starts   # tried anyway, as pick() did


async def test_a_server_still_finishing_requests_is_never_started_twice(sites, fake, monkeypatch):
    twice, real = [], fake.start

    async def start(conf, port):
        if conf.stem in fake.running():
            twice.append(conf.stem)
            raise AssertionError(f"{conf.stem} twice")
        return await real(conf, port)

    monkeypatch.setattr(fake, "start", start)
    monkeypatch.setattr(vpn, "DRAIN_S", 5)
    _collection(N(1), N(2), N(3))
    a, b = sites({N(1): [PAGE, PAGE, PAGE], N(2): [PAGE, PAGE], N(3): [PAGE]}, {N(1): [PAGE], N(2): [NO], N(3): [PAGE]})
    vpn.set_enabled("fake2", False)
    _live(N(1), fake=N(1))
    await a.search(a.keyword_search("x"))
    held = vpn.conn.hold(vpn.conn.route(N(1)))                     # a photo download under way through N1
    held.__enter__()
    try:
        sh = await _search()                                       # N2 for Fake: N1 drains
        assert sh.plan["servers"] == [N(2)] and N(1) in fake.running() and vpn.conn.draining() == 1
        vpn.set_enabled("fake2", True)
        vpn_check.site_on("fake2")                                 # Fake 2 refuses N2: a server for it, not N1 now
        await asyncio.wait_for(vpn_check._run.task, 5)
        assert twice == [] and N(1) not in [s.name for s in vpn_check._run.servers if not s.live]
    finally:
        held.__exit__(None, None, None)


async def test_an_ask_that_sent_nothing_is_asked_once_more_not_taken_as_an_answer(sites, fake, serial):
    _collection(N(1), N(2))
    a, b = sites({N(1): [RuntimeError("recorder: database is locked"), NO], N(2): [PAGE, PAGE]},
                 {N(1): [PAGE], N(2): [PAGE, PAGE]})
    sh = await _search()
    srv = next(s for s in sh.servers if s.name == N(1))
    assert srv.errors == {"fake": 1} and srv.answers["fake"].verdict == "blocked"   # asked again: the real answer
    assert sh.plan["servers"] == [N(2)]


async def test_a_cancelled_switch_over_leaves_no_process_behind(sites, fake, monkeypatch):
    _collection(N(1), N(2))
    sites({N(1): [PAGE]})
    _live(N(1), fake=N(1))
    await vpn.tunnel_for("fake").ensure()
    gate, real = asyncio.Event(), fake.stop

    async def slow_stop(handle):
        await gate.wait()
        await real(handle)

    monkeypatch.setattr(fake, "stop", slow_stop)
    task = asyncio.create_task(vpn.switch([N(2)], {"fake": N(2)}, {N(2): "api.fake.example"}))
    await _until(lambda: vpn.live_servers() == [N(2)])
    task.cancel()                                                  # cancelled while N1 stops
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await vpn.conn.disconnect()
    assert fake.alive() == 0                                       # N1 was not left without an owner


def test_parallel_starts_never_share_a_port(monkeypatch):
    ports = iter([40001, 40001, 40002, 40002, 40003])

    class Sock:
        def __init__(self, *a):
            self.port = next(ports)

        def bind(self, addr):
            pass

        def getsockname(self):
            return ("127.0.0.1", self.port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(vpn.socket, "socket", Sock)
    first = vpn._free_port()
    assert vpn._free_port({first}) == 40002 and vpn._free_port({40001, 40002}) == 40003


async def test_a_refusal_while_a_search_finishes_starts_the_next_one(sites, fake):
    _collection(N(1), N(2))
    gate = asyncio.Event()
    a, b = sites({N(1): [gate, PAGE]}, {N(1): [PAGE]})
    assert vpn_check.start("manual") is None
    sh = vpn_check._run
    await _until(lambda: a.calls(N(1)) == 1)
    sh.closing = True                                              # as when it is past its loop
    assert vpn_check.trigger("fake2", "blocked", "HTTP 403") is True
    p = vpn_check.pending()
    assert (p["by"], p["why"]) == ("fake2", "blocked") and p["at"] >= time.time() + vpn_check.AUTO_GAP_S - 5
    gate.set()
    await asyncio.wait_for(sh.task, 5)
    assert vpn_check.pending() is not None                         # still there for tick()


def test_a_site_never_falls_back_to_a_server_it_refused(fake):
    _collection(N(1), N(2))
    vpn.set_enabled("willhaben", True)
    vpn.update_conn(servers=[N(1)], assign={})
    assert vpn.server_for("willhaben") == N(1)
    vpn.mark("willhaben", N(1), "blocked")
    assert vpn.server_for("willhaben") is None and vpn.waits_on("willhaben") == N(1)


async def test_while_the_pickiest_site_waits_for_its_budget_another_is_asked(sites, fake, serial, monkeypatch):
    Clock(monkeypatch)
    _collection(N(1), N(2))
    for old in ("de-old-wg-001", "de-old-wg-002"):
        vpn.mark("fake", old, "blocked")                           # Fake: the pickiest
    a, b = sites({N(2): [PAGE, PAGE]}, {N(2): [NO]}, per_hour=(4, 0))
    vpn_check._spend("fake", 1)                                    # its quarter of 4 is used for this hour
    _live(N(1), fake=N(1), fake2=N(1))
    assert vpn_check.start("manual") is None
    sh = vpn_check._run
    await asyncio.wait_for(sh.task, 5)
    assert b.calls(N(2)) == 1 and a.calls(N(2)) == 0                # Fake 2 asked while Fake's hour is used ...
    assert _asked(sh, N(2)) == {"fake2": "blocked"}                # ... and its refusal ended N2 without Fake


async def test_processes_still_draining_count_against_the_tunnel_cap(sites, fake, monkeypatch):
    _collection(N(1), N(2))
    sites({N(2): [PAGE]})
    draining = [vpn.SEARCH_TUNNELS]
    monkeypatch.setattr(vpn.conn, "draining", lambda: draining[0])
    assert vpn_check.start("manual") is None
    sh = vpn_check._run
    await asyncio.sleep(0.1)
    assert fake.starts == [] and not sh.task.done()                # no room: it waits, nothing started
    draining[0] = 0
    await asyncio.wait_for(sh.task, 5)
    assert sh.plan["servers"] == [N(2)]


async def test_a_search_that_leaves_no_site_covered_reports_failed_and_stops_the_server(sites, fake):
    _collection(N(1), N(2))
    a, b = sites({}, {}, default=NO)
    _live(N(1), fake=N(1), fake2=N(1))
    for pid in ("fake", "fake2"):
        vpn.mark(pid, N(1), "blocked")
        vpn.pause(pid, "blocked", "HTTP 403")                      # both refused the live server
    sh = await _search()
    assert sh.status == "failed" and fake.alive() == 0 and vpn.live_servers() == []


async def test_a_slow_ask_stops_once_its_server_cannot_help_a_quick_one_is_kept(sites, fake, serial):
    _collection(N(1), N(2), N(3))
    never, quick = asyncio.Event(), asyncio.Event()
    for old in ("de-old-wg-001", "de-old-wg-002"):
        vpn.mark("fake", old, "blocked")                           # Fake: the pickiest, asked first
    a, b, c, e = sites({N(2): [PAGE], N(3): [PAGE]}, {N(2): [quick, PAGE], N(3): [PAGE]},
                       {N(2): [NO], N(3): [PAGE]}, {N(2): [never], N(3): [PAGE, PAGE, PAGE]}, cost=(1, 1, 1, 3))
    _live(N(1), fake=N(1), fake2=N(1), fake3=N(1), fake4=N(1))
    assert vpn_check.start("manual") is None
    sh = vpn_check._run
    await _until(lambda: _asked(sh, N(2)).get("fake3") == "blocked")   # Fake 3 refused: N2 can no longer help
    await asyncio.sleep(0.05)
    assert _asked(sh, N(2))["fake4"] == "skipped"                   # the slow warm-up stopped, not waited for
    assert _asked(sh, N(2))["fake2"] == "asking"                    # a single request under way is kept
    quick.set()
    await asyncio.wait_for(sh.task, 5)                              # `never` never came
    assert _asked(sh, N(2)) == {"fake": "ok", "fake2": "ok", "fake3": "blocked", "fake4": "skipped"}
    assert sh.plan["servers"] == [N(3)] and N(2) in vpn.oks("fake2") and vpn.marks("fake4") == {}


# ------------------------------------------------------------------ Geizhals, switches
async def test_a_switch_turned_on_is_tested_on_the_server_in_use_first(sites, fake, monkeypatch):
    _collection(N(1), N(2))
    (a,) = sites({N(1): [PAGE]})
    _live(N(1), fake=N(1))
    await a.search(a.keyword_search("x"))
    seen = gz(monkeypatch, fake, {N(1): [(200, "<html></html>"), (200, "<html></html>")]})
    vpn_check.site_on(vpn.GEIZHALS)                                 # Settings → VPN: Geizhals switched on
    sh = vpn_check._run
    await asyncio.wait_for(sh.task, 5)
    assert (sh.trigger, sh.by) == ("switch", "geizhals") and seen == [N(1)]
    assert fake.starts == [N(1)] and sh.plan["servers"] == [N(1)]   # no other server started
    assert vpn.conn_state()["assign"] == {"fake": N(1), "geizhals": N(1)}
    assert [s.pid for s in sh.sites] == ["fake", "geizhals"] and sh.servers[0].live
    assert await geizhals.search("x") == [] and seen == [N(1), N(1)]
    assert vpn_check.view()["trigger"] == "Geizhals switched on"



async def test_test_again_asks_every_site_through_its_server_and_switches_nothing(sites, fake):
    """"Test again": every site asks once through the server it uses (started again if its process is not up); no
    other server starts, no search, no mark, the servers stay; the result shows per site."""
    _collection(N(1), N(2))
    sites({N(1): [PAGE]}, {N(1): [NO]})
    _live(N(1), fake=N(1), fake2=N(1))
    assert vpn_check.retest() is None and vpn_check.retest() == "A test is running"      # no second one meanwhile
    await asyncio.wait_for(vpn_check._retest, 5)
    assert fake.starts == [N(1)] and vpn_check._run is None                            # no other server, no search
    assert vpn.live_servers() == [N(1)] and _reasons("fake2") == {} and vpn.paused("fake2") is None
    rows = {r["name"]: (r["label"], r["server"]) for r in vpn_check.view()["retest"]["rows"]}
    assert rows == {"Fake": ("Works", N(1)), "Fake 2": ("Refused", N(1))}
    from app.main import templates
    html = templates.get_template("settings/_vpn_test.html").render(vpn_note=None)
    assert 'action="/settings/vpn/retest"' in html and "Test again" in html and "Tested again" in html
    vpn.update_conn(servers=[])
    assert vpn_check.retest() == "No server yet: the first search finds one."

async def test_geizhals_refusing_the_server_in_use_gets_one_found_and_never_goes_direct(sites, fake, monkeypatch):
    _collection(N(1), N(2))
    (a,) = sites({N(1): [PAGE], N(2): [PAGE, PAGE]})
    _live(N(1), fake=N(1))
    await a.search(a.keyword_search("x"))
    seen = gz(monkeypatch, fake, {N(1): [(403, "")], N(2): [(200, "<html></html>"), (200, "<html></html>")],
                                  "direct": []})
    vpn_check.site_on(vpn.GEIZHALS)
    sh = vpn_check._run
    await asyncio.wait_for(sh.task, 5)
    assert sh.plan["servers"] == [N(2)] and seen == [N(1), N(2)]   # tested through N1, refused; N2 serves both
    assert geizhals._limiter.cooling_down_s == 0                    # a refused server is no block of the home IP
    assert await geizhals.search("x") == [] and seen[-1] == N(2)
    vpn.set_enabled(vpn.GEIZHALS, False)                            # off: directly, as before, and not searched
    assert [t.pid for t in vpn_check.targets()] == ["fake"]


async def test_geizhals_asks_again_through_its_server_before_a_refusal_counts(sites, fake, monkeypatch):
    _collection(N(1), N(2))
    seen = gz(monkeypatch, fake, {N(1): [(403, ""), (403, ""), (200, "<html></html>")], "direct": []})
    _live(N(1), geizhals=N(1))
    assert await geizhals.search("x") == [] and seen == [N(1)] * 3       # two 403s, then the answer: it stays
    assert vpn.marks(vpn.GEIZHALS) == {} and vpn.paused(vpn.GEIZHALS) is None and not vpn_check.running()


async def test_a_403_in_a_search_marks_the_server_for_hours_not_a_day(sites, fake):
    _collection(N(1), N(2))
    sites({N(1): [NO], N(2): [PAGE]})
    sh = await _search()
    assert sh.plan["servers"] == [N(2)]
    until, reason = vpn.marks("fake")[N(1)]
    assert reason == "blocked" and until == pytest.approx(time.time() + vpn_check.TRIAL_BLOCKED_TTL_S, abs=30)


async def test_a_site_no_server_answered_gets_the_next_search_after_its_back_off(sites, fake, monkeypatch):
    _collection(N(1))
    sites({N(1): []})
    started = []
    monkeypatch.setattr(vpn_check, "start", lambda trigger="manual", **kw: started.append((trigger, kw)))
    now = time.time()
    vpn.update_conn(shuffled=now - 60, paused={"fake": ["none", "40/40"]}, triggered={"fake": [now - 60, 1]}, auto=[])
    assert vpn_check.next_at() == pytest.approx(now - 60 + vpn_check.RETRY_S[0])   # not the regular one in 6 h
    await vpn_check.tick()
    assert started == []
    vpn.update_conn(triggered={"fake": [now - vpn_check.RETRY_S[0] - 1, 1]}, shuffled=now - vpn_check.AUTO_GAP_S - 1)
    await vpn_check.tick()
    assert started == [("auto", {"by": "fake", "why": "none", "detail": "40/40"})]


async def test_geizhals_queued_when_its_switch_goes_on_goes_through_the_vpn(fake, monkeypatch):
    _collection(N(1))
    vpn.update_conn(servers=[N(1)])
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
    monkeypatch.setattr(vpn, "_search", lambda *a: False)
    with pytest.raises(geizhals.GeizhalsError, match="no WireGuard config"):
        await geizhals.search("x")
    _collection(N(1))
    with pytest.raises(geizhals.GeizhalsError, match="No working VPN server for Geizhals"):
        await geizhals.search("x")
    assert seen == [] and fake.starts == []


async def test_a_site_switched_off_meanwhile_leaves_the_search_and_nothing_goes_direct(sites, fake):
    _collection(N(1), N(2))
    gate = asyncio.Event()
    a, b = sites({N(1): [gate, PAGE]}, {N(1): [PAGE]})
    assert vpn_check.start("manual") is None
    sh = vpn_check._run
    await _until(lambda: a.calls(N(1)) == 1)
    vpn.set_enabled("fake", False)
    gate.set()
    await asyncio.wait_for(sh.task, 5)
    assert _asked(sh, N(1))["fake"] in ("off", "skipped")     # its answer no longer counts; never sent directly
    assert sh.plan["assign"] == {"fake2": N(1)} and N(1) in a.built


# ------------------------------------------------------------------ pacing
class Clock:
    """The search's hour without waiting an hour: its sleeps move the clock on (and can be held)."""

    def __init__(self, monkeypatch):
        self.now, self.slept, self.hold = 1000.0, [], None
        monkeypatch.setattr(vpn_check, "_clock", lambda: self.now)
        monkeypatch.setattr(vpn_check, "_sleep", self.sleep)

    async def sleep(self, s):
        self.slept.append(s)
        if self.hold is not None:
            await self.hold.wait()
        self.now += s
        await asyncio.sleep(0)


async def test_the_search_waits_for_a_sites_budget_instead_of_skipping_it(sites, fake, serial, monkeypatch):
    clock = Clock(monkeypatch)
    clock.hold = asyncio.Event()
    _collection(N(1), N(2), N(3), N(4))
    (a,) = sites({N(2): [NO], N(3): [NO], N(4): [PAGE]}, per_hour=(8,))     # a quarter of 8: 2 an hour
    _live(N(1), fake=N(1))
    assert vpn_check.start("manual") is None
    sh = vpn_check._run
    await _until(lambda: sh.wait)
    assert a.calls(N(2)) == a.calls(N(3)) == 1 and a.calls(N(4)) == 0       # two asked, the third waits
    v = vpn_check.view()
    assert v["wait"].startswith("Waits for Fake: the search sends it at most a quarter of its 8 requests an hour (2)")
    assert "Waits for Fake" in jobs.snapshot()["active"][0]["step"]
    clock.hold.set()
    await asyncio.wait_for(sh.task, 5)
    assert sh.plan["servers"] == [N(4)] and a.calls(N(4)) == 1              # asked, not skipped
    assert sum(clock.slept) >= 3500                                         # an hour later on its clock
    stamps = sorted(vpn_check._spent["fake"])
    assert all(stamps[i + 2] - stamps[i] >= 3600 for i in range(len(stamps) - 2))   # never 3 within an hour


async def test_the_search_never_takes_a_site_past_ninety_percent(sites, fake, serial, monkeypatch):
    clock = Clock(monkeypatch)
    clock.hold = asyncio.Event()
    _collection(N(1), N(2))
    (a,) = sites({N(2): [PAGE]}, per_hour=(10,))
    a.limiter._recent.extend([time.monotonic()] * 9)                # the agents used 9 of 10 this hour
    _live(N(1), fake=N(1))
    assert vpn_check.start("manual") is None
    sh = vpn_check._run
    await _until(lambda: sh.wait)
    assert sh.wait["kind"] == "full" and a.calls(N(2)) == 0
    assert vpn_check.view()["wait"].startswith("Waits for Fake: 9 of its 10 requests this hour are used")
    await vpn_check.cancel("stopped")
    assert sh.status == "stopped" and a.calls(N(2)) == 0 and a.limiter.last_hour == 9


async def test_a_site_paused_after_a_block_is_waited_for(sites, fake, serial, monkeypatch):
    clock = Clock(monkeypatch)
    _collection(N(1), N(2))
    (a,) = sites({N(2): [PAGE]})
    a.limiter._cooldown_until = time.monotonic() + 600              # its limiter pauses after a block
    _live(N(1), fake=N(1))
    real = clock.sleep

    async def sleep(s):
        a.limiter._cooldown_until = 0                               # the pause ends while it waits
        await real(s)

    monkeypatch.setattr(vpn_check, "_sleep", sleep)
    sh = await _search()
    assert clock.slept and sh.plan["servers"] == [N(2)] and a.calls(N(2)) == 1


# ------------------------------------------------------------------ triggers and guards
async def test_a_block_between_searches_triggers_one_search_not_a_storm(sites, fake):
    _collection(N(1), N(2), N(3))
    a, b = sites({N(1): [NO] * vpn.CONFIRM, N(2): [PAGE, NO], N(3): [PAGE]},
                 {N(1): [PAGE], N(2): [PAGE] + [NO] * vpn.CONFIRM, N(3): [PAGE]})
    _live(N(1), fake=N(1), fake2=N(1))
    _gap_passed()
    with pytest.raises(vpn.VpnWait):
        await a.search(a.keyword_search("x"))                       # 403s through N1: it waits, a search starts
    sh2 = vpn_check._run
    await asyncio.wait_for(sh2.task, 5)
    assert sh2.plan["servers"] == [N(2)] and vpn.paused("fake") is None
    assert vpn_check.view()["trigger"] == f"Fake: {N(1)} blocked by the site (HTTP 403)"
    with pytest.raises(vpn.VpnWait, match="refused the VPN server"):
        await b.search(b.keyword_search("x"))                       # the other site refused N2 right away
    assert vpn_check._run is sh2 and not vpn_check.running() and vpn.paused("fake2") == ("blocked", "HTTP 403")
    p = vpn_check.pending()
    assert (p["trigger"], p["by"], p["why"]) == ("auto", "fake2", "blocked")
    assert p["at"] == pytest.approx(vpn.conn_state()["shuffled"] + vpn_check.AUTO_GAP_S)
    assert vpn_check.view()["pending"] == "Brought forward: Fake 2 refused the server (blocked by the site)."
    await vpn_check.tick()
    assert vpn_check._run is sh2                                    # not its turn yet
    _gap_passed()
    vpn.update_conn(pending={**p, "at": time.time() - 1})
    await vpn_check.tick()
    sh3 = vpn_check._run
    await asyncio.wait_for(sh3.task, 5)
    assert (sh3.trigger, sh3.by) == ("auto", "fake2") and vpn.paused("fake2") is None
    assert vpn.conn_state()["pending"] is None and len(vpn.conn_state()["auto"]) == 2


async def test_a_site_that_refuses_again_gets_its_next_search_after_a_growing_back_off(sites, fake, monkeypatch):
    """Not only the regular search hours later: a site whose new server refuses it too waits RETRY_S, longer each time
    (with no standby server to take: tests/test_vpn.py has those)."""
    monkeypatch.setattr(vpn, "STANDBY_TRIES", 0)
    _collection(N(1), N(2), N(3))
    a, b = sites({N(1): [NO] * vpn.CONFIRM, N(2): [PAGE] + [NO] * vpn.CONFIRM, N(3): [PAGE, PAGE]},
                 {N(1): [PAGE], N(2): [PAGE], N(3): [PAGE, PAGE]})
    _live(N(1), fake=N(1), fake2=N(1))
    _gap_passed()
    with pytest.raises(vpn.VpnWait):
        await a.search(a.keyword_search("x"))                       # refused N1: one automatic search, N2
    sh2 = vpn_check._run
    await asyncio.wait_for(sh2.task, 5)
    assert sh2.plan["servers"] == [N(2)]
    _gap_passed()
    with pytest.raises(vpn.VpnWait, match="refused the VPN server"):
        await a.search(a.keyword_search("x"))                       # answers the test, refuses live: again
    when, n = vpn_check._triggered(vpn.conn_state())["fake"]
    p = vpn_check.pending()
    assert vpn_check._run is sh2 and vpn.paused("fake") and n == 1
    assert (p["by"], p["why"]) == ("fake", "blocked") and p["at"] == pytest.approx(when + vpn_check.RETRY_S[0])
    assert vpn.health()[0] == "unknown" and vpn_check.next_at() == p["at"]   # it waits, and its next search comes
    await vpn_check.tick()
    assert vpn_check._run is sh2                                    # not yet
    vpn.update_conn(pending={**p, "at": time.time() - 1})
    await vpn_check.tick()
    sh3 = vpn_check._run
    await asyncio.wait_for(sh3.task, 5)
    assert (sh3.trigger, sh3.by) == ("auto", "fake") and sh3.plan["servers"] == [N(3)] and vpn.paused("fake") is None
    when, n = vpn_check._triggered(vpn.conn_state())["fake"]
    assert n == 2 and vpn_check._again_at(vpn.conn_state(), "fake") == when + vpn_check.RETRY_S[1]   # longer now


async def test_a_site_whose_pages_changed_does_not_keep_the_searches_going(fake, serial):
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
        sh = await _search()
        assert [_asked(sh, N(i))["fake"] for i in (1, 2, 3)] == ["botcheck", "botcheck", "odd_page"]
        assert sh.plan["servers"] == [N(3)] and vpn.conn_state()["odd"] == ["fake"] and vpn.paused("fake") is None
        _gap_passed()
        with pytest.raises(PlatformError, match="without a result list"):
            await s.search(s.keyword_search("x"))                   # the same page live: the site's error
        assert vpn_check._run is sh and vpn.paused("fake") is None and set(vpn.marks("fake")) == {N(1), N(2)}
        rows = vpn_check.view()["rows"]
        assert rows[0]["label"] == "Site error" and "page the app does not know" in rows[0]["message"]
    finally:
        platforms.reset()


def test_automatic_searches_keep_a_gap_and_a_cap_per_hour():
    now = 100_000.0
    assert vpn_check._auto_at({}, now) == vpn_check.AUTO_GAP_S
    st = {"shuffled": now - 60, "auto": [now - 3000]}
    assert vpn_check._auto_at(st, now) == now - 60 + vpn_check.AUTO_GAP_S
    st = {"shuffled": now - 1000, "auto": [now - 3000, now - 2000, now - 1500]}
    assert vpn_check._auto_at(st, now) == now - 3000 + 3600               # three in the hour: the oldest leaves it
    st["auto"][0] = now - 4000
    assert vpn_check._auto_at(st, now) == now - 1000 + vpn_check.AUTO_GAP_S
    assert vpn_check.pending({"pending": [5.0, "fake", "blocked", "HTTP 403"]})["by"] == "fake"   # an older state


async def test_the_schedule_survives_a_restart(sites, fake):
    _collection(N(1), N(2))
    (a,) = sites({N(1): [PAGE, PAGE, PAGE], N(2): [PAGE]})
    await vpn_check.tick()                             # never searched: due at once
    sh = vpn_check._run
    await asyncio.wait_for(sh.task, 5)
    assert (sh.trigger, sh.plan["servers"]) == ("schedule", [N(1)])
    assert vpn_check.next_at() == pytest.approx(vpn.conn_state()["shuffled"] + 6 * 3600)
    await vpn_check.tick()
    assert vpn_check._run is sh                        # not due
    vpn_check.set_interval(2)
    assert vpn_check.interval_h() == 2

    await vpn.shutdown()                               # a restart: no process, no search in memory
    vpn.reset()
    vpn_check.reset()
    vpn.update_conn(shuffled=time.time() - 3600)
    await vpn_check.tick()
    assert vpn_check._run is None                      # 1 h of 2: the stored time counts
    await a.search(a.keyword_search("x"))              # the stored server comes back
    assert fake.starts[-1] == N(1) and vpn_check._run is None
    assert vpn_check.view()["connections"][0]["name"] == N(1) and vpn_check.view()["run"]
    vpn.update_conn(shuffled=time.time() - 3 * 3600)
    await vpn_check.tick()
    sh2 = vpn_check._run
    await asyncio.wait_for(sh2.task, 5)
    assert (sh2.trigger, sh2.before, sh2.plan["servers"]) == ("schedule", [N(1)], [N(2)])   # another server


# ------------------------------------------------------------------ stop, cancel, shutdown, restart
async def test_stop_keeps_what_was_found_and_leaves_no_search_tunnel(sites, fake):
    from app.main import settings_vpn_stop

    _collection(N(1), N(2), N(3))
    gate = asyncio.Event()
    a, b = sites({N(1): [PAGE, PAGE]}, {N(1): [NO], N(2): [gate]})
    assert vpn_check.start("manual") is None
    sh = vpn_check._run
    await _until(lambda: b.calls(N(2)) == 1 and vpn.live_servers() == [N(1)])   # N1 live for Fake already
    assert vpn_check.view()["best"] == f"{N(1)} for Fake"
    await settings_vpn_stop(type("R", (), {"headers": {}})())
    assert (sh.status, sh.reason) == ("stopped", "stopped") and not vpn_check.running()
    assert fake.running() == [N(1)] and vpn.conn.testing() == []       # what it found stays, its tunnels went
    assert vpn_check.view()["summary"] == "Stopped by you; the servers found so far stay"
    assert vpn.conn_state()["shuffled"] >= sh.started                   # the schedule moves on
    await a.search(a.keyword_search("x"))
    assert a.client.proxy == vpn.conn.route(N(1)).proxy


async def test_new_configs_or_shutdown_cancel_the_search(sites, fake):
    _collection(N(1))
    g = asyncio.Event()
    (a,) = sites({N(1): [g, PAGE]})
    assert vpn_check.start() is None
    assert vpn_check.start() == "A VPN search is running"              # one at a time
    await _until(lambda: a.calls(N(1)) == 1)
    await vpn_check.cancel("uploaded")
    sh = vpn_check._run
    assert (sh.status, sh.reason) == ("stopped", "uploaded") and fake.alive() == 0
    assert json.loads(get_setting("vpn_test"))["status"] == "stopped"
    assert vpn_check.view()["summary"] == "Stopped: new configs were uploaded"

    a.factory.transports[N(1)].answers[:] = [g, PAGE]
    g.clear()
    assert vpn_check.start() is None
    await _until(lambda: a.calls(N(1)) == 2)
    await vpn_check.shutdown()
    await vpn.shutdown()
    assert vpn_check._run.status == "interrupted" and fake.alive() == 0
    assert vpn_check.pending()["trigger"] == "manual"                   # it runs again after the restart

    stored = json.loads(get_setting("vpn_test"))                        # a hard kill: stored while it ran
    stored["status"] = "running"
    set_setting("vpn_test", json.dumps(stored))
    vpn_check._run = None
    assert vpn_check.view()["summary"] == "Interrupted by a restart of the app"


async def test_a_restart_during_a_search_runs_it_again(sites, fake):
    _collection(N(1), N(2), N(3))
    g = asyncio.Event()
    a, b = sites({N(1): [NO], N(2): [g, PAGE]}, {N(1): [PAGE, PAGE], N(2): [PAGE, PAGE]})
    _live(N(1), fake=N(1), fake2=N(1))
    _gap_passed()
    with pytest.raises(vpn.VpnWait):
        await a.search(a.keyword_search("x"))                     # refused: it waits, a search starts
    await _until(lambda: a.calls(N(2)) == 1)
    shuffled = vpn.conn_state().get("shuffled")
    await vpn_check.shutdown()                                     # the app stops while it asks through N2
    await vpn.shutdown()
    st = vpn.conn_state()
    assert st["shuffled"] == shuffled and vpn.paused("fake")       # not done: the schedule does not move on
    assert (st["pending"]["trigger"], st["pending"]["by"]) == ("auto", "fake") and st["pending"]["at"] <= time.time()
    vpn.reset()                                                    # the restart
    vpn_check.reset()
    a.factory.transports[N(2)].answers[:] = [PAGE, PAGE]
    await vpn_check.tick()                                         # the first tick runs it again
    sh = vpn_check._run
    await asyncio.wait_for(sh.task, 5)
    assert (sh.trigger, sh.by, sh.plan["servers"]) == ("auto", "fake", [N(2)]) and vpn.paused("fake") is None

    vpn_check.reset()                                              # a hard kill: no shutdown noted it
    vpn.update_conn(pending=None)
    stored = json.loads(get_setting("vpn_test"))
    stored["status"] = "running"
    set_setting("vpn_test", json.dumps(stored))
    await vpn_check.tick()
    assert vpn_check._run is not None and vpn_check._run.trigger == "auto"
    await asyncio.wait_for(vpn_check._run.task, 5)


async def test_removing_configs_stops_the_processes_even_when_the_search_fails_to_stop(monkeypatch):
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


# ------------------------------------------------------------------ fail closed, EU only, profiles
async def test_fail_closed_when_no_server_answers_any_site(sites, fake):
    _collection(N(1), N(2))
    sites({}, {}, default=NO)
    sh = await _search()
    assert sh.status == "failed" and sh.plan["servers"] == [] and fake.alive() == 0
    st = vpn.conn_state()
    assert st["servers"] == [] and st["error"] == "No server works: none of the 2 checked answers any site"
    v = vpn_check.view()
    assert (v["summary"], v["tone"]) == (st["error"], "bad") and not v["connected"]
    assert all(r["label"] == "No server" for r in v["rows"])
    assert jobs.snapshot()["recent"][0]["problems"] == 3


async def test_a_collection_with_servers_outside_the_eu_never_uses_them(sites, fake):
    _collection("ch-zrh-wg-001", "gb-lon-wg-001", "al-tia-wg-001", N(1))
    sites({}, {}, default=NO)
    sh = await _search()
    assert [s.name for s in sh.servers] == [N(1)] and fake.starts == [N(1)]   # no fallback to ch, gb or al
    assert sh.status == "failed" and sh.reason == "none"
    vpn.remove_source()
    _collection("ch-zrh-wg-001", "gb-lon-wg-001")
    sh = await _search()
    assert (sh.status, sh.reason, sh.servers) == ("failed", "no_eu", [])
    assert vpn_check.view()["summary"] == "No server works: the collection has no EU server left to try"
    assert not {"ch-zrh-wg-001", "gb-lon-wg-001", "al-tia-wg-001"} & set(fake.starts)


async def test_a_profile_is_tested_and_never_swapped(sites, fake):
    vpn.import_upload("home.conf", _conf().encode())
    a, b = sites({"home": [NO, PAGE]}, {"home": [PAGE, PAGE]})
    sh = await _search()
    assert sh.profile and [s.name for s in sh.servers] == ["home"] and sh.plan["servers"] == ["home"]
    assert vpn.paused("fake") == ("blocked", "HTTP 403") and vpn.paused("fake2") is None
    assert vpn_check.view()["summary"] == "home: 1 of 2 sites answer"
    with pytest.raises(vpn.VpnWait):
        await a.search(a.keyword_search("x"))
    sh = await _search()                              # the next test asks the profile again despite its mark
    assert sh.plan["servers"] == ["home"] and vpn.paused("fake") is None and fake.starts == ["home"]
    assert jobs.snapshot()["recent"][0]["label"] == "VPN test"


async def test_a_site_error_is_an_answer(sites, fake, serial):
    _collection(N(1), N(2))
    sites({N(1): [(503, b"{}", JSON)]}, {N(1): [PAGE]})
    sh = await _search()
    assert sh.plan["servers"] == [N(1)] and vpn.marks("fake") == {}
    assert "That is not the server's fault" in vpn_check.view()["rows"][0]["message"]


# ------------------------------------------------------------------ the real adapters' rules
async def test_kleinanzeigen_credentials_are_not_the_servers_fault(fake, request):
    from app.platforms import kleinanzeigen

    _collection(N(1), N(2))
    platforms.set_platform("kleinanzeigen", kleinanzeigen.KleinanzeigenPlatform())
    request.addfinalizer(platforms.reset)
    c = kleinanzeigen.KleinanzeigenClient(transport=ExitTransport([]))
    built = []
    c._factory = exit_factory(fake, {N(1): [(403, b'{"error":"unauthorized"}', JSON)]}, built)
    c._tunnel, c._live.transport = (lambda: vpn.tunnel_for("kleinanzeigen")), None
    c.limiter = RateLimiter(0, name="Kleinanzeigen")
    kleinanzeigen.set_client(c)
    request.addfinalizer(lambda: kleinanzeigen.set_client(None))
    vpn.set_enabled("kleinanzeigen", True)
    sh = await _search()
    ans = sh.servers[0].answers["kleinanzeigen"]
    assert sh.plan["servers"] == [N(1)] and ans.verdict == "site_error" and "app credentials" in ans.detail
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
    c._tunnel, c._live.transport = (lambda: vpn.tunnel_for("ebay")), None
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
    sh = await _search()
    _gap_passed()
    d = await c.item("920036165580")                 # the description (itm.ebaydesc.com) does not answer
    assert d is not None and c._factory.transports[N(1)].calls == 3        # the ad shows without it
    assert vpn_check._run is sh and vpn.paused("ebay") is None and vpn.marks("ebay") == {}


async def test_ebay_warms_up_on_a_server_and_sends_at_most_three_requests_there(fake, request, monkeypatch, serial):
    from app.platforms import ebay
    from tests.conftest import FIXTURES

    _collection(N(1), N(2))
    page = (FIXTURES / "ebay_search.html").read_bytes()
    c, built = _ebay(fake, request, monkeypatch, {N(1): [(403, b"", HTML)] * 4,
                                                  N(2): [(403, b"", HTML), (200, page, HTML)]})
    sh = await _search()
    one, two = sh.servers[0].answers["ebay"], sh.servers[1].answers["ebay"]
    assert (one.verdict, one.requests, two.verdict, two.requests) == ("blocked", 3, "ok", 2)
    assert sh.plan["servers"] == [N(2)] and c._factory.transports[N(1)].calls == 3
    assert _reasons("ebay") == {N(1): "blocked"} and ebay.ebay_limiter().cooling_down_s == 0
    assert c._live.transport is None                                    # the tests' visitors were their own


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
async def test_the_live_panel_polls_its_body_and_keeps_its_header(sites, fake, serial):
    from app.main import templates

    _collection(N(1), N(2))
    g = asyncio.Event()
    a, b = sites({N(1): [g, PAGE], N(2): [PAGE]}, {N(1): [PAGE], N(2): [PAGE]})
    _live(N(2), fake2=N(2))                                          # Fake waits for a server, Fake 2 has one
    vpn.mark("fake", N(2), "blocked")
    vpn.pause("fake", "blocked", "HTTP 403")
    assert vpn_check.start("auto", by="fake", why="blocked", detail="HTTP 403") is None
    await _until(lambda: a.calls(N(1)) == 1)
    v = vpn_check.view()
    assert v["live"] and v["progress"] == "Checked 0 of 2 EU servers" and v["now"] == N(1)
    assert v["waiting"][0]["name"] == "Fake" and v["best"] == f"{N(2)} for Fake 2"
    html = templates.get_template("settings/_vpn_test.html").render(vpn_note=None)
    body = html.index('id="vpn-test-body"')
    assert html.index('aria-live="polite"') < body and html.count('hx-trigger="every 3s"') == 1
    assert 'hx-select-oob="#vpn-test-progress:innerHTML,#vpn-test-clock:innerHTML,#vpn-live:innerHTML"' in html
    assert "<details" not in html and 'action="/settings/vpn/stop"' in html and "Search now" not in html
    assert ">asking</span>" in html and "Reason: Fake: at-vie-wg-002 blocked by the site" in html
    assert "Waits for a server: refused at-vie-wg-002" in html and "Best so far:" in html
    g.set()
    await asyncio.wait_for(vpn_check._run.task, 5)
    html = templates.get_template("settings/_vpn_test.html").render(vpn_note=None)
    assert "hx-trigger" not in html and "Servers asked (" in html and "Search now" in html


async def test_the_panel_in_german(sites, fake, serial):
    from app.i18n import set_request_lang
    from app.main import templates

    _collection(N(1), N(2))
    sites({N(1): [NO], N(2): [PAGE]}, {N(1): [PAGE], N(2): [PAGE]})
    await _search()
    set_request_lang("de")
    try:
        v = vpn_check.view()
        assert v["summary"] == f"{N(2)} für alle 2 Seiten" and v["trigger"] == "Von dir gestartet"
        html = templates.get_template("settings/_vpn_test.html").render(vpn_note=None)
        assert "Anlass: Von dir gestartet" in html and "Jetzt suchen" in html
        assert v["servers"][0]["parts"] == ["Fake: von der Seite gesperrt (HTTP\u00a0403)", "geantwortet: Fake 2"]
        assert (v["servers"][0]["label"], v["servers"][1]["label"]) == ("Abgewiesen", "alle antworten")
    finally:
        set_request_lang("en")


def _wait_search(timeout=5.0):
    """TestClient: the search runs in the app's loop thread; wait until it ended (also before its job showed up)."""
    from tests.conftest import wait_jobs

    end = time.monotonic() + timeout
    while vpn_check.running() and time.monotonic() < end:
        time.sleep(0.02)
    assert not vpn_check.running(), "the search still runs"
    wait_jobs()


def test_settings_page_search_now_interval_and_panel(fake):
    from app.main import app

    _collection(N(1), N(2))
    platforms.set_platform("fake", FakeSite(fake, {N(1): [PAGE], N(2): [PAGE]}))
    vpn.set_enabled("fake", True)
    try:
        with TestClient(app) as c:
            page = c.get("/settings/vpn").text
            assert 'id="vpn-test"' in page and "Search now" in page and "every 3s" not in page
            assert "No server yet: the first search finds one." in page and 'value="6"' in page
            assert 'id="vpn-test-body"' not in page                         # no search yet

            r = c.post("/settings/vpn/search", headers={"HX-Request": "true"})
            assert r.status_code == 200 and "<html" not in r.text and 'id="vpn-test"' in r.text
            _wait_search()
            panel = c.get("/settings/vpn/test").text
            assert f"{N(1)} for Fake" in panel and "Servers asked (1)" in panel and "every 3s" not in panel
            poll = c.get("/settings/vpn/test", headers={"HX-Request": "true", "HX-Target": "vpn-test-body"})
            assert (poll.headers["HX-Retarget"], poll.headers["HX-Reselect"]) == ("#vpn-test", "#vpn-test")
            set_setting("ui_lang", "de")
            assert "Jetzt suchen" in c.get("/settings/vpn/test").text
            set_setting("ui_lang", "en")
            assert any(j["lane"] == "vpn" and j["label"] == "VPN server search" for j in c.get("/jobs").json()["recent"])
            assert "Works" in c.get("/settings/vpn").text

            r = c.post("/settings/vpn/interval", data={"hours": "3"}, follow_redirects=False)
            assert r.status_code == 303 and vpn_check.interval_h() == 3
            for bad in ("x", "²", "0", "999", "2.5"):
                r = c.post("/settings/vpn/interval", data={"hours": bad})
                assert r.status_code == 200 and "Enter whole hours, 1 to 168" in r.text
            assert vpn_check.interval_h() == 3                              # nothing clamped silently
            r = c.post("/settings/vpn/shuffle", follow_redirects=False)     # the old path still works
            assert r.status_code == 303 and r.headers["location"].startswith("/settings/vpn?msg=Search%20started")
            _wait_search()
            assert vpn.conn_state()["servers"] == [N(2)]
            r = c.post("/settings/vpn/stop", follow_redirects=False)
            assert r.status_code == 303 and "Search%20stopped" in r.headers["location"]
            assert "Blocks forgotten" in c.post("/settings/vpn/forget").text
            assert 'id="vpn-test"' not in c.post("/settings/vpn/clear").text
    finally:
        platforms.reset()
