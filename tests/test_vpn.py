"""VPN exits for the anonymous requests (app/platforms/vpn.py): uploads, order, marks, tunnels, fail closed, settings."""
import asyncio
import base64
import io
import os
import stat
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
    """wireproxy without wireproxy: `unreachable` configs fail the probe, `broken` ones do not start."""

    def __init__(self):
        self.starts, self.stopped, self.probes = [], [], []
        self.unreachable, self.broken = set(), set()
        self.ports: dict[int, str] = {}
        self.installed = True

    def binary(self):
        return "/usr/local/bin/wireproxy" if self.installed else None

    async def start(self, conf, port):
        assert conf.is_file() and conf.suffix == ".conf"
        if conf.stem in self.broken:
            raise OSError("wireproxy stopped: bad config")
        self.starts.append(conf.stem)
        self.ports[port] = conf.stem
        await asyncio.sleep(0)
        return FakeProc()

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
    yield r


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


# ------------------------------------------------------------------ order and marks
def test_order_starts_near_the_site():
    names = ["us-nyc-wg-301", "de-fra-wg-001", "at-vie-wg-002", "at-vie-wg-001", "al-tia-wg-001", "custom", "ch-zrh-wg-001"]
    assert vpn.ordered(names, "AT") == ["at-vie-wg-001", "at-vie-wg-002", "de-fra-wg-001", "ch-zrh-wg-001",
                                        "al-tia-wg-001", "custom", "us-nyc-wg-301"]
    assert vpn.ordered(names, "DE")[:3] == ["de-fra-wg-001", "at-vie-wg-001", "at-vie-wg-002"]
    assert vpn.ordered(names, "")[0] == "at-vie-wg-001"       # unknown home: the near list, not the unnamed ones


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
    vpn.forget()
    assert vpn.marks("willhaben") == {}


# ------------------------------------------------------------------ tunnels
async def test_switch_off_means_no_tunnel(fake):
    _collection("at-vie-wg-001")
    assert vpn.tunnel_for("willhaben") is None
    _on("willhaben")
    tn = vpn.tunnel_for("willhaben")
    assert tn is not None and tn.host == "www.willhaben.at" and tn is vpn.tunnel_for("willhaben")


async def test_a_blocked_exit_moves_on_to_the_next(fake):
    _collection("de-fra-wg-001", "at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben")
    tn = vpn.tunnel_for("willhaben")
    r1 = await tn.ensure()
    assert tn.config == "at-vie-wg-001" and r1.proxy.startswith("http://127.0.0.1:")
    assert fake.probes == [("at-vie-wg-001", "www.willhaben.at")]
    assert await tn.ensure() is r1 and len(fake.starts) == 1          # alive: the same route, no new process
    await tn.failed(r1, "blocked")
    assert "at-vie-wg-001" in vpn.marks("willhaben") and len(fake.stopped) == 1
    r2 = await tn.ensure()
    assert tn.config == "at-vie-wg-002" and r2.gen == r1.gen + 1
    await tn.failed(r1, "blocked")                           # late failure of the old route: changes nothing
    assert tn.route is r2 and "at-vie-wg-002" not in vpn.marks("willhaben")
    assert vpn._load()["willhaben"]["current"] == "at-vie-wg-002"


async def test_sites_spread_over_the_collection_and_share_a_profile(fake):
    _collection("at-vie-wg-001", "de-fra-wg-001")
    _on("willhaben", "kleinanzeigen")
    wh, ka = vpn.tunnel_for("willhaben"), vpn.tunnel_for("kleinanzeigen")
    await wh.ensure()
    await ka.ensure()
    assert (wh.config, ka.config) == ("at-vie-wg-001", "de-fra-wg-001")
    assert ka.host == "api.kleinanzeigen.de"
    await vpn.source_changed()
    assert len(fake.stopped) == 2 and vpn._procs == {}
    vpn.import_upload("mine.conf", _conf().encode())
    r1, r2 = await wh.ensure(), await ka.ensure()
    assert r1.proxy == r2.proxy and fake.starts[-1:] == ["mine"] and fake.starts.count("mine") == 1   # one process
    await wh.stop()
    assert not fake.stopped[2:]                              # kleinanzeigen still uses it
    await ka.stop()
    assert len(fake.stopped) == 3


async def test_concurrent_requests_start_one_process(fake):
    _collection("at-vie-wg-001")
    _on("willhaben")
    tn = vpn.tunnel_for("willhaben")
    a, b = await asyncio.gather(tn.ensure(), tn.ensure())
    assert a is b and fake.starts == ["at-vie-wg-001"]


async def test_a_crashed_process_restarts_the_same_exit(fake):
    _collection("at-vie-wg-001", "at-vie-wg-002")
    _on("willhaben")
    tn = vpn.tunnel_for("willhaben")
    r1 = await tn.ensure()
    vpn._procs["at-vie-wg-001"].handle.returncode = 1        # wireproxy died
    await tn.failed(r1, "unreachable")                       # the request saw a connection error
    assert vpn.marks("willhaben") == {}                      # not the exit's fault
    await tn.ensure()
    assert tn.config == "at-vie-wg-001" and fake.starts == ["at-vie-wg-001", "at-vie-wg-001"]


async def test_no_exit_fails_closed_and_waits_before_probing_again(fake, monkeypatch):
    names = [f"at-vie-wg-00{i}" for i in range(1, 7)]
    _collection(*names)
    fake.unreachable = set(names)
    fake.broken = {"at-vie-wg-006"}
    _on("willhaben")
    tn = vpn.tunnel_for("willhaben")
    with pytest.raises(PlatformError, match="tries more"):
        await tn.ensure()
    assert len(fake.probes) == vpn.MAX_PROBES and not tn.failing      # more left for the next request
    with pytest.raises(PlatformError, match="all 6 configs"):
        await tn.ensure()
    assert len(fake.probes) == 5 and tn.failing                      # 006 does not even start
    assert vpn.health() == ("problem", "No working exit for willhaben")
    with pytest.raises(PlatformError):
        await tn.ensure()
    assert len(fake.probes) == 5                                      # exhausted: no probing for EXHAUSTED_S
    fake.unreachable.clear()
    vpn.forget()
    await tn.ensure()
    assert tn.config == "at-vie-wg-001" and vpn.health()[0] == "ok"


async def test_a_blocked_profile_pauses_until_forgotten(fake):
    vpn.import_upload("home.conf", _conf().encode())
    _on("willhaben")
    tn = vpn.tunnel_for("willhaben")
    r = await tn.ensure()
    await tn.failed(r, "blocked")
    with pytest.raises(PlatformError, match="profile"):
        await tn.ensure()
    vpn.forget()
    assert (await tn.ensure()).gen == r.gen + 1


async def test_switched_on_without_configs_or_wireproxy_fails_closed(fake):
    _on("willhaben")
    tn = vpn.tunnel_for("willhaben")
    with pytest.raises(PlatformError, match="no WireGuard config"):
        await tn.ensure()
    assert vpn.health()[0] == "problem"
    _collection("at-vie-wg-001")
    fake.installed = False
    with pytest.raises(PlatformError, match="wireproxy"):
        await tn.ensure()
    assert fake.starts == []


def test_health_is_off_without_switches():
    assert vpn.health()[0] == "off"


# ------------------------------------------------------------------ for the VPN test (app/services/vpn_check.py)
N1, N2, N3 = "at-vie-wg-001", "at-vie-wg-002", "at-vie-wg-003"


async def test_listeners_hear_every_exit(fake):
    _collection(N1, N2, N3)
    fake.unreachable, fake.broken = {N1}, {N2}
    _on("willhaben")
    tn = vpn.tunnel_for("willhaben")
    heard = []

    def broken_hook(*_):
        raise RuntimeError("a display hook")

    with tn.listen(lambda *a: heard.append(a)), tn.listen(broken_hook):     # a failing listener breaks nothing
        r = await tn.ensure()
        assert heard == [(N1, "trying", ""), (N1, "unreachable", "no connection to www.willhaben.at"),
                         (N2, "trying", ""), (N2, "broken", "wireproxy stopped: bad config"),
                         (N3, "trying", ""), (N3, "using", "")]
        assert await tn.failed(r, "botcheck", detail="x") is True
        assert heard[-1] == (N3, "botcheck", "x") and vpn.marks("willhaben")[N3][1] == "botcheck"
        assert await tn.failed(r, "botcheck", detail="x") is False          # the same, now old route
    assert tn._listeners == [] and len(heard) == 7


async def test_ensure_probes_as_many_as_asked(fake):
    _collection(N1, N2)
    fake.unreachable = {N1}
    _on("willhaben")
    tn = vpn.tunnel_for("willhaben")
    with pytest.raises(PlatformError, match="1 tried"):
        await tn.ensure(probes=1)
    assert len(fake.probes) == 1 and not tn.failing
    r = await tn.ensure(probes=1)
    assert r.name == tn.config == N2 and tn.current() == r
    vpn._procs[N2].handle.returncode = 1                       # wireproxy died
    assert tn.current() is None


async def test_a_cancelled_probe_leaves_no_process(monkeypatch):
    class Hanging(FakeRunner):
        async def probe(self, port, host):
            await asyncio.Event().wait()

    r = Hanging()
    monkeypatch.setattr(vpn, "runner", r)
    _collection(N1)
    _on("willhaben")
    task = asyncio.create_task(vpn.tunnel_for("willhaben").ensure())
    while not vpn._procs:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert vpn._procs == {} and len(r.stopped) == 1


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


def test_botcheck_mark_forget_one_site_and_unmark():
    assert vpn.MARK_TTL_S["botcheck"] == 3600 and vpn.reason_label("botcheck") == "answered with a bot check"
    vpn.mark("willhaben", N1, "botcheck")
    vpn.mark("willhaben", N2, "blocked")
    vpn.mark("kleinanzeigen", N1, "blocked")
    vpn.unmark("willhaben", N1)
    vpn.unmark("willhaben", "nope")
    assert set(vpn.marks("willhaben")) == {N2}
    vpn.forget("willhaben")
    assert vpn.marks("willhaben") == {} and set(vpn.marks("kleinanzeigen")) == {N1}


# ------------------------------------------------------------------ photos
async def test_photos_of_a_site_on_the_vpn_never_go_direct(fake, tmp_path):
    from app.services import ad_images

    _on("willhaben")                                          # no configs uploaded: no exit
    calls = []
    mock = httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(200, content=b"x",
                                                                               headers={"content-type": "image/jpeg"}))
    assert await ad_images.download("https://cache.willhaben.at/mmo/1/2.jpg", tmp_path, "a", transport=mock) is None
    assert calls == []
    vpn.set_enabled("willhaben", False)
    assert await ad_images.download("https://cache.willhaben.at/mmo/1/2.jpg", tmp_path, "a", transport=mock)
    assert len(calls) == 1


# ------------------------------------------------------------------ settings
def test_settings_upload_switch_and_remove(fake):
    from app.main import app

    with TestClient(app) as c:
        page = c.get("/settings/vpn").text
        assert "Nothing uploaded" in page and 'href="/settings/vpn"' in page
        zip_ = _zip({f"mullvad_wireguard_linux_all_all/{n}.conf": _conf() for n in ("at-vie-wg-001", "de-fra-wg-001")})
        r = c.post("/settings/vpn/upload", files={"file": ("mullvad_wireguard_linux_all_all.zip", zip_, "application/zip")},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/settings/vpn?msg=2%20configs")
        assert vpn.enabled("willhaben") and not vpn.enabled("kleinanzeigen")     # the first upload: willhaben on
        page = c.get("/settings/vpn").text
        assert "Config collection · 2 configs" in page and "AT 1" in page and PRIVATE not in page
        assert "Starts with the next request" in page
        assert "Anonymous requests through the VPN" in c.get("/settings/willhaben").text

        r = c.post("/settings/vpn/site/kleinanzeigen", data={"enabled": "1", "next": "/settings/kleinanzeigen"},
                   follow_redirects=False)
        assert r.headers["location"].startswith("/settings/kleinanzeigen?msg=") and vpn.enabled("kleinanzeigen")
        c.post("/settings/vpn/site/willhaben", data={"next": "//evil.example"})
        assert not vpn.enabled("willhaben")
        c.post("/settings/vpn/site/willhaben", data={"enabled": "1"})
        assert c.post("/settings/vpn/site/nope", data={"enabled": "1"}).status_code == 404

        vpn.set_enabled("willhaben", False)                    # switched off by hand: a new upload keeps it off
        r = c.post("/settings/vpn/upload", files={"file": ("Home.conf", _conf().encode(), "application/octet-stream")})
        assert "Dedicated profile · home" in r.text and PRIVATE not in r.text and not vpn.enabled("willhaben")
        r = c.post("/settings/vpn/upload", files={"file": ("x.txt", b"hello", "text/plain")})
        assert "Upload a .zip" in r.text and vpn.source()[0] == "profile"
        r = c.post("/settings/vpn/clear")
        assert "Nothing uploaded" in r.text and vpn.source() == (None, [])
