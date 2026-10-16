"""The VPN connections for the anonymous requests (app/platforms/vpn.py): uploads, the EU-only order, marks, one or
two live servers and the search's tunnels, switch-overs, the fast probe, fail closed, health, settings. Fake wireproxy
(FakeRunner) only: no tunnel, no site request."""
import asyncio
import base64
import io
import os
import stat
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from app.platforms import PlatformError, vpn


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


PRIVATE = _key()


def _conf(endpoint: str = "146.70.116.130:51820", private: str = PRIVATE) -> str:
    return (f"[Interface]\n# Device: Test Wasp\nPrivateKey = {private}\nAddress = 10.64.1.2/32,fc00:bbbb::1/128\n"
            f"DNS = 10.64.0.1\n\n[Peer]\nPublicKey = {_key()}\nAllowedIPs = 0.0.0.0/0,::0/0\nEndpoint = {endpoint}")


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _collection(*names: str) -> None:
    vpn.import_upload("mullvad.zip", _zip({f"mullvad_wireguard_linux_all_all/{n}.conf": _conf() for n in names}))




class FakeProc:
    def __init__(self, name: str = ""):
        self.returncode = None
        self.name, self.port = name, 0

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class FakeRunner:
    """wireproxy without wireproxy: `dead` configs give no handshake, `unreachable` ones no CONNECT to the host,
    `broken` ones do not start, `gates` hold a server's probe until set; `down`: servers whose running tunnel no
    longer gets through (reach()). `most` is the largest number of processes that ran at the same time; a server
    never runs twice at once, and no two processes share a port."""

    def __init__(self):
        self.starts, self.stopped, self.probes, self.reached = [], [], [], []
        self.unreachable, self.broken, self.dead, self.down = set(), set(), set(), set()
        self.gates: dict[str, asyncio.Event] = {}
        self.ports: dict[int, str] = {}
        self.procs: list[FakeProc] = []
        self.installed = True
        self.most = 0

    def binary(self):
        return "/usr/local/bin/wireproxy" if self.installed else None

    def alive(self):
        return sum(p.returncode is None for p in self.procs)

    def running(self):
        return sorted(p.name for p in self.procs if p.returncode is None)

    async def start(self, conf, port):
        assert conf.is_file() and conf.suffix == ".conf"
        assert conf.stem not in self.running(), f"{conf.stem} runs twice"       # one key on one server twice
        assert all(p.port != port for p in self.procs if p.returncode is None), f"port {port} taken"
        if conf.stem in self.broken:
            raise OSError("wireproxy stopped: bad config")
        self.starts.append(conf.stem)
        self.ports[port] = conf.stem
        await asyncio.sleep(0)
        proc = FakeProc(conf.stem)
        proc.port = port
        self.procs.append(proc)
        self.most = max(self.most, self.alive())
        return proc

    async def reach(self, port, host):
        name = self.ports[port]
        self.reached.append(name)
        await asyncio.sleep(0)
        return name not in self.down and name not in self.dead

    async def probe(self, handle, port, host):
        name = self.ports[port]
        self.probes.append((name, host))
        if name in self.gates:
            await self.gates[name].wait()
        return "handshake" if name in self.dead else "connect" if name in self.unreachable else ""

    async def stop(self, handle):
        if handle.returncode is None:
            handle.terminate()
            self.stopped.append(handle)


@pytest.fixture(autouse=True)
def no_source():
    """DATA_DIR lives for the whole test run: every test starts and ends without uploaded configs."""
    vpn.remove_source()
    yield
    vpn.remove_source()


@pytest.fixture
def fake(monkeypatch):
    r = FakeRunner()
    monkeypatch.setattr(vpn, "runner", r)
    monkeypatch.setattr(vpn, "_shuffled", sorted)          # the "random" order within a tier: by name
    yield r


class Asked(list):
    comes = False             # what vpn_check.trigger() answers: True when a search runs or comes


@pytest.fixture
def no_search(monkeypatch):
    """Searches asked for by vpn.py are recorded, not run (tests/test_vpn_check.py runs them)."""
    asked = Asked()

    def record(pid, reason, detail):
        asked.append((pid, reason, detail))
        return asked.comes

    monkeypatch.setattr(vpn, "_search", record)
    return asked


def _on(*pids: str) -> None:
    for pid in pids:
        vpn.set_enabled(pid, True)


# ------------------------------------------------------------------ uploads
def test_zip_upload_takes_the_configs_of_mullvads_download():
    data = _zip({"mullvad_wireguard_linux_all_all/at-vie-wg-001.conf": _conf(),
                 "mullvad_wireguard_linux_all_all/de-fra-wg-002.conf": _conf(),
                 "mullvad_wireguard_linux_all_all/Weird Name#1.conf": _conf(),
                 "__MACOSX/mullvad_wireguard_linux_all_all/._at-vie-wg-001.conf": b"\x00\x05junk",
                 "README.txt": "hello",
                 "broken.conf": "[Interface]\nPrivateKey = nope\n"})
    mode, n, skipped = vpn.import_upload("mullvad_wireguard_linux_all_all.zip", data)
    assert (mode, n) == ("collection", 3)
    assert len(skipped) == 1 and skipped[0].startswith("broken.conf: ")
    assert vpn.source() == ("collection", ["at-vie-wg-001", "de-fra-wg-002", "weird-name-1"])
    root = vpn.root()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700 and stat.S_IMODE((root / "collection").stat().st_mode) == 0o700
    f = root / "collection" / "at-vie-wg-001.conf"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and PRIVATE in f.read_text()


def test_a_bad_upload_keeps_the_old_source():
    _collection("at-vie-wg-001")
    for name, data in [("x.zip", _zip({"a.conf": "[Interface]\nPrivateKey = x\n"})),   # nothing usable
                       ("x.zip", b"not a zip at all"), ("notes.txt", b"hi"),
                       ("p.conf", _conf().replace("Endpoint", "# Endpoint").encode()),
                       ("p.conf", (_conf() + "\n[Socks5]\nBindAddress = 0.0.0.0:1080\n").encode())]:   # no wireproxy sections
        with pytest.raises(ValueError):
            vpn.import_upload(name, data)
    assert vpn.source() == ("collection", ["at-vie-wg-001"])


def test_a_conf_upload_is_the_dedicated_profile_and_replaces_the_collection():
    _collection("at-vie-wg-001", "de-fra-wg-001")
    mode, n, _ = vpn.import_upload("My Profile.conf", _conf().encode())
    assert (mode, n) == ("profile", 1)
    assert vpn.source() == ("profile", ["my-profile"])
    assert not (vpn.root() / "collection").exists()
    _collection("ch-zrh-wg-001")                              # and back: a zip replaces the profile
    assert vpn.source() == ("collection", ["ch-zrh-wg-001"])


def test_zip_limits(monkeypatch):
    big = _conf() + "\n#" + "x" * (vpn.MAX_CONF + 10)       # compresses to almost nothing
    _mode, n, skipped = vpn.import_upload("z.zip", _zip({"ok.conf": _conf(), "big.conf": big}))
    assert n == 1 and skipped and "big.conf" in skipped[0]
    monkeypatch.setattr(vpn, "MAX_MEMBERS", 3)
    with pytest.raises(ValueError):
        vpn.import_upload("many.zip", _zip({f"s{i}.conf": _conf() for i in range(5)}))
    names = vpn.import_upload("same.zip", _zip({"a/x.conf": _conf(), "b/x.conf": _conf()}))[1]
    assert names == 2 and vpn.source()[1] == ["x", "x-2"]


def test_check_wants_a_complete_wireguard_config():
    assert vpn.check(_conf()) is None
    assert vpn.check(_conf(endpoint="vpn.example.com:51820")) is None
    assert "endpoint" in vpn.check(_conf(endpoint="no port"))
    assert "privatekey" in vpn.check(_conf(private="tooshort="))
    assert vpn.check("just text") is not None


# ------------------------------------------------------------------ the order: EU only, home first, marks
def test_the_order_never_leaves_the_eu_and_starts_at_home():
    _collection("ch-zrh-wg-001", "gb-lon-wg-001", "al-tia-wg-001", "us-nyc-wg-301", "no-osl-wg-001", "rs-beg-wg-101",
                "custom", "nl-ams-wg-001", "de-fra-wg-001", "at-vie-wg-001", "at-vie-wg-002", "es-mad-wg-101")
    outside = {"ch-zrh-wg-001", "gb-lon-wg-001", "al-tia-wg-001", "us-nyc-wg-301", "no-osl-wg-001", "rs-beg-wg-101",
               "custom"}
    for _ in range(50):                                       # the real random order within a tier
        order = vpn.order()
        assert set(order[:2]) == {"at-vie-wg-001", "at-vie-wg-002"}          # home first
        assert order[2:] == ["de-fra-wg-001", "nl-ams-wg-001", "es-mad-wg-101"]   # DE, the neighbours, any EU
    assert vpn.order({"at-vie-wg-001", "de-fra-wg-001"})[1] == "nl-ams-wg-001"
    eu = {"at-vie-wg-001", "at-vie-wg-002", "de-fra-wg-001", "nl-ams-wg-001", "es-mad-wg-101"}
    assert vpn.order(eu) == []                                # never one outside the EU, not even as a fallback
    assert not set(vpn.order()) & outside and vpn.eu_count(vpn.source()[1]) == 5
    assert not vpn.allowed("ch-zrh-wg-001") and vpn.allowed("at-vie-wg-001")


def test_a_profile_is_ordered_as_it_is():
    vpn.import_upload("ch-zrh-wg-001.conf", _conf().encode())   # the user's choice: used even outside the EU
    assert vpn.order() == ["ch-zrh-wg-001"] and vpn.order({"ch-zrh-wg-001"}) == []
    assert vpn.allowed("ch-zrh-wg-001")


def test_marks_expire_and_are_kept_per_site(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(vpn.time, "time", lambda: now)
    vpn.mark("willhaben", "at-vie-wg-001", "blocked")
    vpn.mark("willhaben", "at-vie-wg-002", "rate")
    vpn.mark("willhaben", "at-vie-wg-003", "dead")
    vpn.mark("willhaben", "at-vie-wg-004", "ok")
    vpn.mark("willhaben", "at-vie-wg-005", "unreachable")
    assert set(vpn.marks("willhaben")) == {"at-vie-wg-001", "at-vie-wg-002", "at-vie-wg-003", "at-vie-wg-005"}
    assert set(vpn.oks("willhaben")) == {"at-vie-wg-004"} and vpn.marks("kleinanzeigen") == {}
    now += 2 * 3600
    assert set(vpn.marks("willhaben")) == {"at-vie-wg-001", "at-vie-wg-003"}    # a 429 or no answer: an hour,
    now += 23 * 3600                                                             # a confirmed dead server: a day
    assert vpn.marks("willhaben") == {} and vpn.oks("willhaben") == {}
    vpn.mark("willhaben", "x", "ok")
    vpn.mark("willhaben", "x", "blocked")                       # the newer note replaces the older one
    assert set(vpn.marks("willhaben")) == {"x"} and vpn.oks("willhaben") == {}
    vpn.mark("willhaben", "x", "ok")
    assert vpn.marks("willhaben") == {} and set(vpn.oks("willhaben")) == {"x"}
    vpn.mark("willhaben", "y", "blocked")
    vpn.mark("kleinanzeigen", "y", "botcheck")
    vpn.pause("willhaben", "blocked", "HTTP 403")
    vpn.pause("kleinanzeigen", "rate", "HTTP 429")
    vpn.forget("willhaben")
    assert vpn.marks("willhaben") == {} and vpn.paused("willhaben") is None and set(vpn.oks("willhaben")) == {"x"}
    assert vpn.marks("kleinanzeigen") and vpn.paused("kleinanzeigen") == ("rate", "HTTP 429")
    vpn.forget()
    assert vpn.marks("kleinanzeigen") == {} and vpn.paused("kleinanzeigen") is None


# ------------------------------------------------------------------ the connections
async def test_switch_off_means_no_tunnel_and_geizhals_has_its_own(fake):
    _collection("at-vie-wg-001")
    assert vpn.tunnel_for("willhaben") is None and vpn.tunnel_for(vpn.GEIZHALS) is None
    _on("willhaben", vpn.GEIZHALS)
    tn = vpn.tunnel_for("willhaben")
    assert tn is not None and tn.host == "www.willhaben.at" and tn is vpn.tunnel_for("willhaben")
    gz = vpn.tunnel_for(vpn.GEIZHALS)
    assert (gz.host, gz.label) == ("geizhals.at", "Geizhals")
    assert vpn.users() == [("willhaben", "willhaben"), ("geizhals", "Geizhals")]


async def test_sites_share_one_server_or_two_and_unused_ones_stop(fake, no_search):
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben", "kleinanzeigen", vpn.GEIZHALS)
    vpn.update_conn(server="at-vie-wg-001", since=time.time() - 60)      # a state from before the search
    a, b, c = await asyncio.gather(vpn.tunnel_for("willhaben").ensure(), vpn.tunnel_for("kleinanzeigen").ensure(),
                                   vpn.tunnel_for(vpn.GEIZHALS).ensure())
    assert a == b == c and a.name == "at-vie-wg-001" and fake.starts == ["at-vie-wg-001"] and no_search == []
    assert fake.probes == [("at-vie-wg-001", "www.willhaben.at")]          # started once, for the first that asked
    assert vpn.live_servers() == ["at-vie-wg-001"] and vpn.since("at-vie-wg-001") < time.time() - 50

    await vpn.switch(["at-vie-wg-001", "at-vie-wg-002"], {"willhaben": "at-vie-wg-001",
                     "kleinanzeigen": "at-vie-wg-001", vpn.GEIZHALS: "at-vie-wg-002"}, {})
    wh, gz = vpn.tunnel_for("willhaben").current(), vpn.tunnel_for(vpn.GEIZHALS).current()
    assert wh == a and gz.name == "at-vie-wg-002" and gz.proxy != wh.proxy     # the first keeps its route
    assert fake.alive() == 2 and vpn.server_for("kleinanzeigen") == "at-vie-wg-001"
    assert vpn.since("at-vie-wg-001") < time.time() - 50 < vpn.since("at-vie-wg-002")

    vpn.set_enabled(vpn.GEIZHALS, False)
    await vpn.site_off(vpn.GEIZHALS)                          # nobody uses the second one: it stops
    assert vpn.live_servers() == ["at-vie-wg-001"] and fake.alive() == 1
    for pid in ("willhaben", "kleinanzeigen"):
        vpn.set_enabled(pid, False)
    await vpn.site_off("willhaben")
    assert fake.alive() == 0 and vpn.live_servers() == ["at-vie-wg-001"]   # stopped; switched on, it starts again


async def test_a_crashed_process_or_a_restart_starts_the_same_server_again(fake, no_search):
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben")
    vpn.update_conn(server="at-vie-wg-001", since=1.0)
    tn = vpn.tunnel_for("willhaben")
    r1 = await tn.ensure()
    vpn.conn.live["at-vie-wg-001"][0].handle.returncode = 1    # wireproxy died
    assert await tn.failed(r1, "unreachable") is False         # not the server's fault: no mark, no search
    assert vpn.marks("willhaben") == {} and no_search == []
    await tn.ensure()
    await vpn.shutdown()                                       # a restart: the processes end with the app,
    vpn.reset()                                                # the stored server comes back
    await vpn.tunnel_for("willhaben").ensure()
    assert fake.starts == ["at-vie-wg-001"] * 3 and no_search == [] and vpn.since("at-vie-wg-001") == 1.0


async def test_a_refusal_marks_pauses_and_asks_for_a_search_the_others_go_on(fake, no_search):
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben", "kleinanzeigen")
    vpn.update_conn(servers=["at-vie-wg-001"])
    wh, ka = vpn.tunnel_for("willhaben"), vpn.tunnel_for("kleinanzeigen")
    r = await wh.ensure()
    assert await wh.failed(r, "blocked", "HTTP 403") is True
    assert no_search == [("willhaben", "blocked", "HTTP 403")]
    assert vpn.marks("willhaben")["at-vie-wg-001"][1] == "blocked" and vpn.paused("willhaben") == ("blocked", "HTTP 403")
    assert await wh.failed(r, "blocked", "HTTP 403") is False          # paused already: never a second search
    assert len(no_search) == 1
    with pytest.raises(vpn.VpnWait, match="refused the VPN server at-vie-wg-001 .* until the search finds another"):
        await wh.ensure()                                              # waits (fail closed), no error of the agent
    assert await ka.ensure() == r                                      # the others go on with it
    old = vpn.Route(r.proxy, r.gen - 1, r.name)
    assert await ka.failed(old, "blocked") is False                    # a route of an earlier process
    await vpn.switch(["at-vie-wg-001", "at-vie-wg-002"], {"kleinanzeigen": "at-vie-wg-002"}, {})
    assert await ka.failed(r, "blocked") is False                      # no longer its server
    vpn.pause("willhaben", "none", "12/40")
    with pytest.raises(vpn.VpnWait, match="No VPN server answers willhaben right now"):
        await wh.ensure()


async def test_a_refusal_counts_only_after_several_in_a_row_and_an_answer_starts_the_count_again(fake, no_search):
    """eBay's bot manager refuses now and then and lets the next request in: one 403 says little about the server."""
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben")
    vpn.update_conn(servers=["at-vie-wg-001"])
    tn = vpn.tunnel_for("willhaben")
    r = await tn.ensure()
    for _ in range(vpn.CONFIRM - 1):
        assert await tn.refused(r, "blocked", "HTTP 403") is False      # asked again through the same server
    tn.answered(r)                                                      # an answer between: from none again
    for _ in range(vpn.CONFIRM - 1):
        assert await tn.refused(r, "blocked", "HTTP 403") is False
    assert vpn.marks("willhaben") == {} and vpn.paused("willhaben") is None and no_search == []
    assert await tn.refused(r, "blocked", "HTTP 403") is True           # the third in a row counts
    assert vpn.paused("willhaben") == ("blocked", "HTTP 403") and no_search == [("willhaben", "blocked", "HTTP 403")]
    assert vpn.marks("willhaben")["at-vie-wg-001"][1] == "blocked"


async def test_a_run_of_refusals_within_seconds_keeps_the_server(fake, no_search, monkeypatch):
    """eBay refuses every other request for half a minute right after a search found it answering; Geizhals a burst:
    a run of refusals counts only once it lasted CONFIRM_SPAN_S (a server that is blocked refuses on and on)."""
    monkeypatch.setattr(vpn, "CONFIRM_SPAN_S", 90)
    _collection("at-vie-wg-001")
    _on("ebay")
    vpn.update_conn(servers=["at-vie-wg-001"])
    tn = vpn.tunnel_for("ebay")
    r = await tn.ensure()
    for _ in range(vpn.CONFIRM + 3):
        assert await tn.refused(r, "blocked", "HTTP 403") is False       # five 403s within seconds
    assert vpn.marks("ebay") == {} and vpn.paused("ebay") is None and no_search == []
    key = ("ebay", r.name)
    vpn._strikes[key] = [x - 100 for x in vpn._strikes[key]]            # ... the run began 100 s ago
    assert await tn.refused(r, "blocked", "HTTP 403") is True           # still refusing: it counts
    assert vpn.marks("ebay")["at-vie-wg-001"][1] == "blocked"


async def test_network_errors_count_after_two_and_old_refusals_no_longer_count(fake, no_search):
    _collection("at-vie-wg-001")
    _on("willhaben")
    vpn.update_conn(servers=["at-vie-wg-001"])
    tn = vpn.tunnel_for("willhaben")
    r = await tn.ensure()
    vpn._strikes[("willhaben", r.name)] = [time.monotonic() - vpn.CONFIRM_WINDOW_S - 1] * 5   # long ago
    assert await tn.refused(r, "unreachable", "timed out") is False
    assert await tn.refused(r, "unreachable", "timed out") is True
    assert vpn.paused("willhaben") == ("unreachable", "timed out")
    old = vpn.Route(r.proxy, r.gen - 1, r.name)
    assert await tn.refused(old, "blocked") is False and len(no_search) == 1   # not its route: never counts


async def test_a_refusal_that_counts_moves_the_site_to_the_other_live_server_without_a_pause(fake, no_search):
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben", "kleinanzeigen")
    vpn.update_conn(servers=["at-vie-wg-001", "at-vie-wg-002"],
                    assign={"willhaben": "at-vie-wg-001", "kleinanzeigen": "at-vie-wg-002"})
    wh = vpn.tunnel_for("willhaben")
    r = await wh.ensure()
    assert r.name == "at-vie-wg-001"
    assert await wh.failed(r, "blocked", "HTTP 403") is True
    assert vpn.paused("willhaben") is None and no_search == []          # it goes on at once: no pause, no search
    assert vpn.server_for("willhaben") == "at-vie-wg-002" and "willhaben" not in vpn.conn_state()["assign"]
    assert vpn.live_servers() == ["at-vie-wg-002"]                      # the refused one stopped: nobody uses it
    r2 = await wh.ensure()
    assert r2.name == "at-vie-wg-002"
    assert await wh.failed(r2, "blocked", "HTTP 403") is True           # refused there too: nowhere left to go
    assert vpn.paused("willhaben") == ("blocked", "HTTP 403") and no_search == [("willhaben", "blocked", "HTTP 403")]


async def test_a_site_does_not_move_to_a_live_server_it_refused_lately(fake, no_search):
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben", "kleinanzeigen")
    vpn.update_conn(servers=["at-vie-wg-001", "at-vie-wg-002"],
                    assign={"willhaben": "at-vie-wg-001", "kleinanzeigen": "at-vie-wg-002"})
    vpn.mark("willhaben", "at-vie-wg-002", "blocked")
    wh = vpn.tunnel_for("willhaben")
    r = await wh.ensure()
    assert await wh.failed(r, "blocked", "HTTP 403") is True
    assert vpn.paused("willhaben") == ("blocked", "HTTP 403") and len(no_search) == 1


async def test_a_refusal_with_nowhere_left_moves_the_site_to_a_standby_server_it_answered_lately(fake, no_search):
    """The searches note the servers each site answered through: a refused site takes one of them in seconds, the
    newest first, instead of waiting for a search; one that does not start is marked and the next one tried."""
    _collection("at-vie-wg-001", "at-vie-wg-002", "de-fra-wg-001", "de-fra-wg-002", "de-ber-wg-001")
    _on("willhaben", "kleinanzeigen")
    vpn.update_conn(servers=["at-vie-wg-001"])
    for name, age in (("de-ber-wg-001", 30), ("de-fra-wg-001", 20), ("de-fra-wg-002", 10), ("at-vie-wg-002", 5)):
        vpn.mark("willhaben", name, "ok", ttl=3600 - age)               # answered lately, newest last
    vpn.mark("willhaben", "at-vie-wg-002", "blocked")                  # ... and refused since: not a standby
    fake.unreachable = {"de-fra-wg-002"}                               # starts, but does not reach the site
    assert vpn.standby("willhaben") == ["de-fra-wg-002", "de-fra-wg-001", "de-ber-wg-001"]
    wh, ka = vpn.tunnel_for("willhaben"), vpn.tunnel_for("kleinanzeigen")
    r = await wh.ensure()
    await ka.ensure()
    assert await wh.failed(r, "blocked", "HTTP 403") is True
    assert vpn.paused("willhaben") is None and no_search == []          # no pause, no search
    assert vpn.server_for("willhaben") == "de-fra-wg-001" and vpn.conn_state()["assign"]["willhaben"] == "de-fra-wg-001"
    assert vpn.marks("willhaben")["de-fra-wg-002"][1] == "unreachable"
    assert vpn.live_servers() == ["at-vie-wg-001", "de-fra-wg-001"]     # the other site keeps its server
    assert vpn.server_for("kleinanzeigen") == "at-vie-wg-001"
    assert (await wh.ensure()).name == "de-fra-wg-001"
    assert "de-fra-wg-002" not in fake.running()


async def test_a_server_that_stops_answering_leaves_and_every_site_on_it_fails_over(fake, no_search):
    _collection("at-vie-wg-001", "de-fra-wg-001", "de-fra-wg-002")
    _on("willhaben", "kleinanzeigen")
    vpn.update_conn(servers=["at-vie-wg-001"])
    for pid in ("willhaben", "kleinanzeigen"):
        vpn.mark(pid, "de-fra-wg-001", "ok")
    wh, ka = vpn.tunnel_for("willhaben"), vpn.tunnel_for("kleinanzeigen")
    r = await wh.ensure()
    assert (await ka.ensure()) == r
    fake.down = {"at-vie-wg-001"}                                      # the tunnel no longer gets through
    assert await wh.refused(r, "unreachable", "timed out") is False
    assert await wh.refused(r, "unreachable", "timed out") is True
    assert vpn.live_servers() == ["de-fra-wg-001"] and "at-vie-wg-001" not in fake.running()
    assert (await wh.ensure()).name == "de-fra-wg-001" and (await ka.ensure()).name == "de-fra-wg-001"
    assert vpn.paused("willhaben") is None and vpn.paused("kleinanzeigen") is None and no_search == []


async def test_without_a_standby_server_the_site_still_waits_for_a_search(fake, no_search):
    _collection("at-vie-wg-001", "de-fra-wg-001")
    _on("willhaben")
    vpn.update_conn(servers=["at-vie-wg-001"])
    vpn.mark("willhaben", "de-fra-wg-001", "ok")
    fake.dead = {"de-fra-wg-001"}                                      # no handshake: marked, not taken
    tn = vpn.tunnel_for("willhaben")
    r = await tn.ensure()
    assert await tn.failed(r, "blocked", "HTTP 403") is True
    assert vpn.paused("willhaben") == ("blocked", "HTTP 403") and no_search == [("willhaben", "blocked", "HTTP 403")]
    assert vpn.marks("willhaben")["de-fra-wg-001"][1] == "unreachable" and vpn.standby("willhaben") == []


async def test_a_switch_over_lets_requests_in_flight_finish_first(fake, no_search, monkeypatch):
    monkeypatch.setattr(vpn, "DRAIN_S", 5)
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben")
    vpn.update_conn(servers=["at-vie-wg-001"])
    old = await vpn.tunnel_for("willhaben").ensure()
    with vpn.conn.hold(old):                                   # a request under way through the old server
        await vpn.switch(["at-vie-wg-002"], {"willhaben": "at-vie-wg-002"}, {"at-vie-wg-002": "www.willhaben.at"})
        assert vpn.tunnel_for("willhaben").current().name == "at-vie-wg-002"   # new requests: the new server
        assert vpn.conn.alive(old) and fake.alive() == 2                      # the old one still answers it
    for _ in range(50):
        if fake.alive() == 1:
            break
        await asyncio.sleep(0.05)
    assert fake.running() == ["at-vie-wg-002"] and not vpn.conn.alive(old)


async def test_the_search_tunnels_never_run_a_live_server_or_one_twice(fake):
    _collection("at-vie-wg-001", "at-vie-wg-002", "at-vie-wg-003")
    _on("willhaben")
    vpn.update_conn(servers=["at-vie-wg-001"])
    with pytest.raises(RuntimeError):
        await vpn.conn.start_search("at-vie-wg-001", "www.willhaben.at")          # live
    r2, _ = await vpn.conn.start_search("at-vie-wg-002", "www.willhaben.at")
    r3, _ = await vpn.conn.start_search("at-vie-wg-003", "www.willhaben.at")    # several at once
    with pytest.raises(RuntimeError):
        await vpn.conn.start_search("at-vie-wg-002", "www.willhaben.at")      # twice
    assert vpn.conn.testing() == ["at-vie-wg-002", "at-vie-wg-003"] and vpn.conn.alive(r2) and vpn.conn.alive(r3)
    await vpn.switch(["at-vie-wg-002"], {"willhaben": "at-vie-wg-002"}, {})     # taken over: no new start
    assert fake.starts.count("at-vie-wg-002") == 1 and vpn.conn.alive(r2) and vpn.conn.testing() == ["at-vie-wg-003"]
    assert vpn.tunnel_for("willhaben").current().gen != r2.gen                  # its own live route
    await vpn.conn.stop_search()
    assert fake.running() == ["at-vie-wg-002"]


async def test_no_server_asks_for_a_search_and_fails_closed_when_none_comes(fake, no_search):
    _collection("at-vie-wg-001")
    _on("willhaben")
    with pytest.raises(PlatformError, match="No working VPN server for willhaben") as info:
        await vpn.tunnel_for("willhaben").ensure()
    assert not isinstance(info.value, vpn.VpnWait)
    assert no_search == [("willhaben", "none", "")] and fake.starts == []
    no_search.comes = True                                             # a search runs or comes: the site waits
    with pytest.raises(vpn.VpnWait, match="searching a server for willhaben"):
        await vpn.tunnel_for("willhaben").ensure()
    no_search.comes = False

    vpn.update_conn(servers=["at-vie-wg-001"])
    fake.unreachable = {"at-vie-wg-001"}                               # the stored server no longer answers
    with pytest.raises(PlatformError, match="not reachable"):
        await vpn.tunnel_for("willhaben").ensure()
    assert [x[:2] for x in no_search[-2:]] == [("willhaben", "unreachable"), ("willhaben", "none")]
    assert vpn.live_servers() == [] and vpn.marks("willhaben")["at-vie-wg-001"][1] == "unreachable"
    assert fake.alive() == 0


async def test_a_stored_server_outside_the_eu_never_starts_again(fake, no_search):
    _collection("ch-zrh-wg-001", "at-vie-wg-001")
    _on("willhaben")
    vpn.update_conn(server="ch-zrh-wg-001")                            # e.g. stored before the EU rule
    with pytest.raises(PlatformError, match="No working VPN server"):
        await vpn.tunnel_for("willhaben").ensure()
    assert fake.starts == [] and no_search == [("willhaben", "none", "")]


async def test_shutdown_waits_for_a_start_under_way_and_none_starts_after_it(fake, monkeypatch):
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben")
    vpn.update_conn(servers=["at-vie-wg-001"])
    gate, start = asyncio.Event(), fake.start

    async def slow(conf, port):
        await gate.wait()
        return await start(conf, port)

    monkeypatch.setattr(fake, "start", slow)
    task = asyncio.create_task(vpn.tunnel_for("willhaben").ensure())
    await asyncio.sleep(0.01)
    stopping = asyncio.create_task(vpn.shutdown())
    await asyncio.sleep(0.01)
    gate.set()
    await stopping
    await task
    assert fake.alive() == 0 and vpn.conn.routes() == []              # the start finished, then it was stopped
    with pytest.raises(PlatformError, match="shutting down"):
        await vpn.conn.start_search("at-vie-wg-002", "www.willhaben.at")
    assert len(fake.starts) == 1


def test_details_in_the_viewers_language():
    from app.i18n import set_request_lang

    set_request_lang("de")
    try:
        assert vpn.describe("no connection to www.willhaben.at") == "keine Verbindung zu www.willhaben.at"
        assert vpn.describe("no handshake in 5 s") == "kein WireGuard-Handshake in 5 s"
        assert vpn.describe("Failed to perform, curl: (28) Operation timed out after 30001 ms") == \
            "keine Antwort in der Zeit"
        assert vpn.describe("proxy connect refused") == "Verbindung abgelehnt"
        assert vpn.describe("HTTP 403") == "HTTP 403"
    finally:
        set_request_lang("en")
    assert vpn.describe("keine Verbindung zu www.ebay.at") == "no connection to www.ebay.at"


async def test_switched_on_without_configs_or_wireproxy_fails_closed(fake, no_search):
    _on("willhaben")
    tn = vpn.tunnel_for("willhaben")
    with pytest.raises(PlatformError, match="no WireGuard config"):
        await tn.ensure()
    assert vpn.health()[0] == "problem"
    _collection("at-vie-wg-001")
    fake.installed = False
    with pytest.raises(PlatformError, match="wireproxy"):
        await tn.ensure()
    assert fake.starts == [] and no_search == []


async def test_health(fake, no_search, monkeypatch):
    from app.services import vpn_check

    assert vpn.health()[0] == "off"
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben", vpn.GEIZHALS)
    assert vpn.health() == ("unknown", "Not used yet")
    vpn.update_conn(servers=["at-vie-wg-001"])
    assert vpn.health() == ("ok", "at-vie-wg-001, starts with the next request")
    await vpn.tunnel_for("willhaben").ensure()
    assert vpn.health() == ("ok", "at-vie-wg-001")
    await vpn.switch(["at-vie-wg-001", "at-vie-wg-002"], {"willhaben": "at-vie-wg-001", "geizhals": "at-vie-wg-002"},
                     {})
    assert vpn.health() == ("ok", "at-vie-wg-001 + at-vie-wg-002")
    vpn.pause("willhaben", "blocked", "HTTP 403")
    assert vpn.health() == ("problem", "willhaben paused: refused at-vie-wg-001 + at-vie-wg-002")
    monkeypatch.setattr(vpn_check, "running", lambda: True)
    assert vpn.health() == ("unknown", "Searching a server for willhaben")      # the search works on it
    vpn.pause("willhaben", "none", "40/40")
    assert vpn.health() == ("problem", "willhaben paused: no VPN server answers them")
    vpn.unpause()
    monkeypatch.setattr(vpn_check, "running", lambda: False)
    vpn.update_conn(servers=[], error="No server works")
    await vpn.conn.disconnect()
    assert vpn.health() == ("problem", "No server works")


async def test_a_cancelled_start_of_a_search_tunnel_leaves_no_process(monkeypatch):
    class Hanging(FakeRunner):
        async def probe(self, handle, port, host):
            await asyncio.Event().wait()

    r = Hanging()
    monkeypatch.setattr(vpn, "runner", r)
    _collection("at-vie-wg-001")
    task = asyncio.create_task(vpn.conn.start_search("at-vie-wg-001", "www.willhaben.at"))
    while not r.procs:
        await asyncio.sleep(0)
    assert vpn.conn.testing() == ["at-vie-wg-001"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert vpn.conn.testing() == [] and r.alive() == 0 and len(r.stopped) == 1


async def test_a_cancelled_start_leaves_no_wireproxy(monkeypatch, tmp_path):
    proc = FakeProc()

    async def spawn(*_a, **_kw):
        return proc

    async def never(*_a, **_kw):
        await asyncio.Event().wait()

    async def no_info(self, binary):
        return False

    monkeypatch.setattr(vpn.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(vpn.asyncio, "open_connection", never)
    monkeypatch.setattr(vpn.Runner, "binary", lambda self: "/usr/bin/true")
    monkeypatch.setattr(vpn.Runner, "_has_info", no_info)
    conf = tmp_path / "x.conf"
    conf.write_text(_conf())
    task = asyncio.create_task(vpn.Runner().start(conf, 1))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.returncode == 0                               # terminated, not left without owner


async def test_the_probe_tells_a_dead_server_by_its_missing_handshake(monkeypatch):
    """The real probe against wireproxy's two ports, played by local servers: /metrics on the info address and the
    proxy's CONNECT. A server without a handshake fails within HANDSHAKE_S, not after the CONNECT timeout."""
    monkeypatch.setattr(vpn, "HANDSHAKE_S", 0.3)
    state = {"handshake": 0, "connect": b""}

    async def info(reader, writer):
        await reader.readline()
        writer.write(b"HTTP/1.0 200 OK\r\n\r\nprivate_key=REDACTED\nlast_handshake_time_sec=%d\n" % state["handshake"])
        await writer.drain()
        writer.close()

    async def proxy(reader, writer):
        await reader.readline()
        if state["connect"]:
            writer.write(state["connect"])
            await writer.drain()
        else:
            await asyncio.sleep(5)                            # WireGuard still trying
        writer.close()

    s1 = await asyncio.start_server(info, "127.0.0.1", 0)
    s2 = await asyncio.start_server(proxy, "127.0.0.1", 0)
    handle = FakeProc()
    handle.info = f"127.0.0.1:{s1.sockets[0].getsockname()[1]}"
    port = s2.sockets[0].getsockname()[1]
    try:
        t0 = time.monotonic()
        assert await vpn.Runner().probe(handle, port, "www.willhaben.at") == "handshake"
        assert time.monotonic() - t0 < 2
        state.update(handshake=1760000000, connect=b"HTTP/1.1 200 Connection established\r\n\r\n")
        assert await vpn.Runner().probe(handle, port, "www.willhaben.at") == ""
        state["connect"] = b"HTTP/1.1 503 Service Unavailable\r\n\r\n"
        assert await vpn.Runner().probe(handle, port, "www.willhaben.at") == "connect"
    finally:
        s1.close()
        s2.close()
    e = vpn.Unreachable("www.willhaben.at", handshake=True)
    assert e.detail == "no handshake in 0.3 s" and "handshake" in str(e)


# ------------------------------------------------------------------ photos
async def test_photos_of_a_site_on_the_vpn_never_go_direct(fake, no_search, tmp_path):
    from app.services import ad_images

    _on("willhaben")                                          # no configs uploaded: no server
    calls = []
    mock = httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(200, content=b"x",
                                                                               headers={"content-type": "image/jpeg"}))
    assert await ad_images.download("https://cache.willhaben.at/mmo/1/2.jpg", tmp_path, "a", transport=mock) is None
    assert calls == []
    vpn.set_enabled("willhaben", False)
    assert await ad_images.download("https://cache.willhaben.at/mmo/1/2.jpg", tmp_path, "a", transport=mock)
    assert len(calls) == 1




# ------------------------------------------------------------------ settings
def test_settings_upload_switches_and_remove(fake, monkeypatch):
    from app.main import app
    from app.services import vpn_check

    started = []
    monkeypatch.setattr(vpn_check, "start", lambda trigger="manual", **kw: started.append(trigger))   # no site asked
    with TestClient(app) as c:
        page = c.get("/settings/vpn").text
        assert "Nothing uploaded" in page and 'href="/settings/vpn"' in page and "Geizhals through the VPN" in page
        zip_ = _zip({f"mullvad_wireguard_linux_all_all/{n}.conf": _conf()
                     for n in ("at-vie-wg-001", "de-fra-wg-001", "ch-zrh-wg-001")})
        r = c.post("/settings/vpn/upload", files={"file": ("mullvad_wireguard_linux_all_all.zip", zip_, "application/zip")},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/settings/vpn?msg=3%20configs")
        assert vpn.enabled("willhaben") and not vpn.enabled("kleinanzeigen")     # the first upload: willhaben on
        assert started == ["new"]                                                 # and a search right away
        page = c.get("/settings/vpn").text
        assert "Config collection · 3 configs" in page and "AT 1" in page and PRIVATE not in page
        assert "the 2 servers in EU countries only" in page and 'id="vpn-test"' in page
        assert "No server yet: the first search finds one." in page
        assert "Anonymous requests through the VPN" in c.get("/settings/willhaben").text

        r = c.post("/settings/vpn/site/kleinanzeigen", data={"enabled": "1", "next": "/settings/kleinanzeigen"},
                   follow_redirects=False)
        assert r.headers["location"].startswith("/settings/kleinanzeigen?msg=") and vpn.enabled("kleinanzeigen")
        assert started == ["new", "switch"]                                       # switched on: a search asks it
        c.post("/settings/vpn/site/geizhals", data={"enabled": "1"})
        assert vpn.enabled(vpn.GEIZHALS) and started[-1] == "switch"
        vpn.update_conn(servers=["at-vie-wg-001"], assign={"willhaben": "at-vie-wg-001"})
        vpn.pause(vpn.GEIZHALS, "blocked", "HTTP 403")
        vpn.pause("kleinanzeigen", "none", "2/2")
        page = c.get("/settings/vpn").text
        assert "Works" in page and "through at-vie-wg-001" in page                # willhaben's row
        assert "Waits for a server: refused at-vie-wg-001 (blocked by the site, HTTP 403)" in page
        assert "Paused: no VPN server answers. The last search checked 2 of 2 EU servers" in page
        assert "Paused: no VPN server answers." in c.get("/settings/kleinanzeigen").text
        c.post("/settings/vpn/site/geizhals", data={})
        assert not vpn.enabled(vpn.GEIZHALS) and vpn.paused(vpn.GEIZHALS) is None
        c.post("/settings/vpn/site/willhaben", data={"next": "//evil.example"})
        assert not vpn.enabled("willhaben")
        assert c.post("/settings/vpn/site/nope", data={"enabled": "1"}).status_code == 404

        r = c.post("/settings/vpn/upload", files={"file": ("Home.conf", _conf().encode(), "application/octet-stream")})
        assert "Dedicated profile · home" in r.text and PRIVATE not in r.text and not vpn.enabled("willhaben")
        r = c.post("/settings/vpn/upload", files={"file": ("x.txt", b"hello", "text/plain")})
        assert "Upload a .zip" in r.text and vpn.source()[0] == "profile"
        r = c.post("/settings/vpn/clear")
        assert "Nothing uploaded" in r.text and vpn.source() == (None, [])
