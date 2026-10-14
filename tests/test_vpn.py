"""The VPN connection for the anonymous requests (app/platforms/vpn.py): uploads, the EU-only pick, marks, the one
shared process, fail closed, health, settings. Fake wireproxy (FakeRunner) only: no tunnel, no site request."""
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
    def __init__(self):
        self.returncode = None

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class FakeRunner:
    """wireproxy without wireproxy: `unreachable` configs fail the probe, `broken` ones do not start. `most` is the
    largest number of processes that ran at the same time."""

    def __init__(self):
        self.starts, self.stopped, self.probes = [], [], []
        self.unreachable, self.broken = set(), set()
        self.ports: dict[int, str] = {}
        self.procs: list[FakeProc] = []
        self.installed = True
        self.most = 0

    def binary(self):
        return "/usr/local/bin/wireproxy" if self.installed else None

    def alive(self):
        return sum(p.returncode is None for p in self.procs)

    async def start(self, conf, port):
        assert conf.is_file() and conf.suffix == ".conf"
        if conf.stem in self.broken:
            raise OSError("wireproxy stopped: bad config")
        self.starts.append(conf.stem)
        self.ports[port] = conf.stem
        await asyncio.sleep(0)
        proc = FakeProc()
        self.procs.append(proc)
        self.most = max(self.most, self.alive())
        return proc

    async def probe(self, port, host):
        self.probes.append((self.ports[port], host))
        return self.ports[port] not in self.unreachable

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
    monkeypatch.setattr(vpn, "_choice", lambda names: sorted(names)[0])     # the "random" pick: the first by name
    yield r


@pytest.fixture
def no_shuffle(monkeypatch):
    """Shuffles asked for by vpn.py are recorded, not run (tests/test_vpn_check.py runs them)."""
    asked = []
    monkeypatch.setattr(vpn, "_shuffle", lambda pid, reason, detail: asked.append((pid, reason, detail)) or False)
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


# ------------------------------------------------------------------ the pick: EU only, home first, marks
def test_the_pick_never_leaves_the_eu_and_starts_at_home():
    _collection("ch-zrh-wg-001", "gb-lon-wg-001", "al-tia-wg-001", "us-nyc-wg-301", "no-osl-wg-001", "rs-beg-wg-101",
                "custom", "nl-ams-wg-001", "de-fra-wg-001", "at-vie-wg-001", "at-vie-wg-002", "es-mad-wg-101")
    outside = {"ch-zrh-wg-001", "gb-lon-wg-001", "al-tia-wg-001", "us-nyc-wg-301", "no-osl-wg-001", "rs-beg-wg-101",
               "custom"}
    picks = {vpn.pick() for _ in range(200)}                  # the real random choice
    assert picks == {"at-vie-wg-001", "at-vie-wg-002"}         # home first
    assert vpn.pick({"at-vie-wg-001", "at-vie-wg-002"}) == "de-fra-wg-001"       # then DE and the neighbours
    assert vpn.pick({"at-vie-wg-001", "at-vie-wg-002", "de-fra-wg-001"}) == "nl-ams-wg-001"
    assert vpn.pick({"at-vie-wg-001", "at-vie-wg-002", "de-fra-wg-001", "nl-ams-wg-001"}) == "es-mad-wg-101"
    eu = {"at-vie-wg-001", "at-vie-wg-002", "de-fra-wg-001", "nl-ams-wg-001", "es-mad-wg-101"}
    assert vpn.pick(eu) is None                                # never one outside the EU, not even as a fallback
    assert not picks & outside and vpn.eu_count(vpn.source()[1]) == 5

    vpn.mark("willhaben", "at-vie-wg-001", "blocked")
    vpn.mark("ebay", "at-vie-wg-002", "rate")
    assert vpn.pick(skip_for=["willhaben", "ebay"]) == "de-fra-wg-001"           # marked for any site that is on
    assert vpn.pick(skip_for=["willhaben"]) == "at-vie-wg-002"
    for n in eu:
        vpn.mark("willhaben", n, "blocked")
    assert vpn.pick(skip_for=["willhaben"]) in {"at-vie-wg-001", "at-vie-wg-002"}   # all marked: marks ignored


def test_a_profile_is_picked_as_it_is():
    vpn.import_upload("ch-zrh-wg-001.conf", _conf().encode())   # the user's choice: used even outside the EU
    assert vpn.pick() == "ch-zrh-wg-001" and vpn.pick({"ch-zrh-wg-001"}) is None


def test_marks_expire_and_are_kept_per_site(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(vpn.time, "time", lambda: now)
    vpn.mark("willhaben", "at-vie-wg-001", "blocked")
    vpn.mark("willhaben", "at-vie-wg-002", "rate")
    assert set(vpn.marks("willhaben")) == {"at-vie-wg-001", "at-vie-wg-002"} and vpn.marks("kleinanzeigen") == {}
    now += 2 * 3600
    assert set(vpn.marks("willhaben")) == {"at-vie-wg-001"}   # a 429 counts for an hour, a block for a day
    now += 23 * 3600
    assert vpn.marks("willhaben") == {}
    vpn.mark("willhaben", "x", "blocked")
    vpn.mark("kleinanzeigen", "x", "botcheck")
    vpn.pause("willhaben", "blocked", "HTTP 403")
    vpn.pause("kleinanzeigen", "rate", "HTTP 429")
    vpn.forget("willhaben")
    assert vpn.marks("willhaben") == {} and vpn.paused("willhaben") is None
    assert vpn.marks("kleinanzeigen") and vpn.paused("kleinanzeigen") == ("rate", "HTTP 429")
    vpn.forget()
    assert vpn.marks("kleinanzeigen") == {} and vpn.paused("kleinanzeigen") is None


# ------------------------------------------------------------------ the one connection
async def test_switch_off_means_no_tunnel_and_geizhals_has_its_own(fake):
    _collection("at-vie-wg-001")
    assert vpn.tunnel_for("willhaben") is None and vpn.tunnel_for(vpn.GEIZHALS) is None
    _on("willhaben", vpn.GEIZHALS)
    tn = vpn.tunnel_for("willhaben")
    assert tn is not None and tn.host == "www.willhaben.at" and tn is vpn.tunnel_for("willhaben")
    gz = vpn.tunnel_for(vpn.GEIZHALS)
    assert (gz.host, gz.label) == ("geizhals.at", "Geizhals")
    assert vpn.users() == [("willhaben", "willhaben"), ("geizhals", "Geizhals")]


async def test_every_site_shares_one_process(fake, no_shuffle):
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben", "kleinanzeigen", vpn.GEIZHALS)
    vpn.update_conn(server="at-vie-wg-001", since=time.time() - 60)       # as a shuffle leaves it
    a, b, c = await asyncio.gather(vpn.tunnel_for("willhaben").ensure(), vpn.tunnel_for("kleinanzeigen").ensure(),
                                   vpn.tunnel_for(vpn.GEIZHALS).ensure())
    assert a == b == c and a.name == "at-vie-wg-001" and fake.starts == ["at-vie-wg-001"] and no_shuffle == []
    assert fake.probes == [("at-vie-wg-001", "www.willhaben.at")]          # started once, for the first that asked

    r = await vpn.conn.connect("at-vie-wg-002", "www.willhaben.at")       # another server replaces the process
    assert fake.most == 1 and len(fake.stopped) == 1 and r.gen == a.gen + 1
    assert vpn.tunnel_for("kleinanzeigen").current() == r
    await vpn.site_off("kleinanzeigen")
    assert vpn.conn.current() == r                                      # still used by the others
    for pid in ("willhaben", "kleinanzeigen", vpn.GEIZHALS):
        vpn.set_enabled(pid, False)
    await vpn.site_off("willhaben")
    assert vpn.conn.current() is None and len(fake.stopped) == 2        # nobody uses it: stopped


async def test_a_crashed_process_or_a_restart_starts_the_same_server_again(fake, no_shuffle):
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben")
    vpn.update_conn(server="at-vie-wg-001", since=1.0)
    tn = vpn.tunnel_for("willhaben")
    r1 = await tn.ensure()
    vpn.conn.proc.handle.returncode = 1                      # wireproxy died
    assert await tn.failed(r1, "unreachable") is False       # not the server's fault: no mark, no shuffle
    assert vpn.marks("willhaben") == {} and no_shuffle == []
    await tn.ensure()
    vpn.reset()                                              # a restart: the stored server comes back
    await vpn.tunnel_for("willhaben").ensure()
    assert fake.starts == ["at-vie-wg-001"] * 3 and no_shuffle == [] and vpn.conn_state()["since"] == 1.0


async def test_a_refusal_marks_pauses_and_asks_for_a_shuffle(fake, no_shuffle):
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben", "kleinanzeigen")
    vpn.update_conn(server="at-vie-wg-001")
    wh, ka = vpn.tunnel_for("willhaben"), vpn.tunnel_for("kleinanzeigen")
    r = await wh.ensure()
    assert await wh.failed(r, "blocked", "HTTP 403") is True
    assert no_shuffle == [("willhaben", "blocked", "HTTP 403")]
    assert vpn.marks("willhaben")["at-vie-wg-001"][1] == "blocked" and vpn.paused("willhaben") == ("blocked", "HTTP 403")
    assert await wh.failed(r, "blocked", "HTTP 403") is False          # paused already: never a second shuffle
    assert len(no_shuffle) == 1
    with pytest.raises(PlatformError, match="refused the VPN server at-vie-wg-001"):
        await wh.ensure()                                              # fail closed while paused
    assert await ka.ensure() == r                                      # the others go on with it
    old = vpn.Route(r.proxy, r.gen - 1, r.name)
    assert await ka.failed(old, "blocked") is False                    # a route of an earlier connection


async def test_requests_wait_for_a_shuffle_or_fail_closed(fake, monkeypatch):
    _collection("at-vie-wg-001")
    _on("willhaben")
    vpn.update_conn(server="at-vie-wg-001")
    monkeypatch.setattr(vpn, "SHUFFLE_WAIT_S", 0.05)
    vpn.conn.begin()
    with pytest.raises(PlatformError, match="changing its server"):
        await vpn.tunnel_for("willhaben").ensure()
    assert fake.starts == []                                           # nothing started, nothing sent
    task = asyncio.create_task(vpn.tunnel_for("willhaben").ensure())
    await asyncio.sleep(0.01)
    vpn.conn.end()
    assert (await task).name == "at-vie-wg-001"


async def test_no_server_asks_for_a_shuffle_and_fails_closed_when_none_comes(fake, no_shuffle):
    _collection("at-vie-wg-001")
    _on("willhaben")
    with pytest.raises(PlatformError, match="No working VPN server for willhaben"):
        await vpn.tunnel_for("willhaben").ensure()
    assert no_shuffle == [("willhaben", "none", "")] and fake.starts == []

    vpn.update_conn(server="at-vie-wg-001")
    fake.unreachable = {"at-vie-wg-001"}                               # the stored server no longer answers
    with pytest.raises(PlatformError, match="not reachable"):
        await vpn.tunnel_for("willhaben").ensure()
    assert no_shuffle[-1][:2] == ("willhaben", "unreachable") and vpn.conn_state()["server"] is None
    assert vpn.marks("willhaben")["at-vie-wg-001"][1] == "unreachable" and fake.alive() == 0


async def test_switched_on_without_configs_or_wireproxy_fails_closed(fake, no_shuffle):
    _on("willhaben")
    tn = vpn.tunnel_for("willhaben")
    with pytest.raises(PlatformError, match="no WireGuard config"):
        await tn.ensure()
    assert vpn.health()[0] == "problem"
    _collection("at-vie-wg-001")
    fake.installed = False
    with pytest.raises(PlatformError, match="wireproxy"):
        await tn.ensure()
    assert fake.starts == [] and no_shuffle == []


async def test_health(fake, no_shuffle):
    assert vpn.health()[0] == "off"
    _collection("at-vie-wg-001")
    _on("willhaben")
    assert vpn.health() == ("unknown", "Not used yet")
    vpn.update_conn(server="at-vie-wg-001")
    assert vpn.health() == ("ok", "at-vie-wg-001, starts with the next request")
    await vpn.tunnel_for("willhaben").ensure()
    assert vpn.health() == ("ok", "at-vie-wg-001")
    vpn.pause("willhaben", "blocked", "HTTP 403")
    assert vpn.health() == ("problem", "willhaben paused: refused at-vie-wg-001")
    vpn.unpause()
    vpn.conn.begin()
    assert vpn.health()[0] == "unknown"                                # a shuffle: the watchdog waits
    vpn.conn.end()
    vpn.update_conn(server=None, error="No server works")
    await vpn.conn.disconnect()
    assert vpn.health() == ("problem", "No server works")


async def test_a_cancelled_connect_leaves_no_process(monkeypatch):
    class Hanging(FakeRunner):
        async def probe(self, port, host):
            await asyncio.Event().wait()

    r = Hanging()
    monkeypatch.setattr(vpn, "runner", r)
    _collection("at-vie-wg-001")
    task = asyncio.create_task(vpn.conn.connect("at-vie-wg-001", "www.willhaben.at"))
    while not r.procs:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert vpn.conn.current() is None and r.alive() == 0 and len(r.stopped) == 1


async def test_a_cancelled_start_leaves_no_wireproxy(monkeypatch, tmp_path):
    proc = FakeProc()

    async def spawn(*_a, **_kw):
        return proc

    async def never(*_a, **_kw):
        await asyncio.Event().wait()

    monkeypatch.setattr(vpn.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(vpn.asyncio, "open_connection", never)
    monkeypatch.setattr(vpn.Runner, "binary", lambda self: "/usr/bin/true")
    conf = tmp_path / "x.conf"
    conf.write_text(_conf())
    task = asyncio.create_task(vpn.Runner().start(conf, 1))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.returncode == 0                               # terminated, not left without owner


# ------------------------------------------------------------------ photos
async def test_photos_of_a_site_on_the_vpn_never_go_direct(fake, no_shuffle, tmp_path):
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
        assert started == ["new"]                                                 # and a shuffle right away
        page = c.get("/settings/vpn").text
        assert "Config collection · 3 configs" in page and "AT 1" in page and PRIVATE not in page
        assert "the 2 servers in EU countries only" in page and 'id="vpn-test"' in page
        assert "Anonymous requests through the VPN" in c.get("/settings/willhaben").text

        r = c.post("/settings/vpn/site/kleinanzeigen", data={"enabled": "1", "next": "/settings/kleinanzeigen"},
                   follow_redirects=False)
        assert r.headers["location"].startswith("/settings/kleinanzeigen?msg=") and vpn.enabled("kleinanzeigen")
        c.post("/settings/vpn/site/geizhals", data={"enabled": "1"})
        assert vpn.enabled(vpn.GEIZHALS)
        vpn.pause(vpn.GEIZHALS, "blocked", "HTTP 403")
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
