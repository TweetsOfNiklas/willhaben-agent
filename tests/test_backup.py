"""Backup export/import (Settings → Backup): contents, manifest, validation, safety backups, round trip."""
import hashlib
import io
import json
import os
import sqlite3
import stat
import time
import zipfile
from pathlib import Path

import pytest
from sqlmodel import select

from app import __version__
from app.config import get_settings
from app.db import SearchAgent, get_setting, session, set_setting
from app.services import backup


@pytest.fixture
def ddir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(get_settings(), "data_dir", d)
    monkeypatch.setattr(backup, "SCRYPT_LOG2N", 10)     # fast key derivation in tests
    (d / "claude-token").write_text("sk-ant-oat-test")
    (d / "ha-token").write_text("ha-test-token")
    (d / "action-secret").write_bytes(b"\x01" * 32)
    (d / "google-calendar.json").write_text('{"client_id": "x"}')
    (d / "notes.txt").write_text("not a secret file")
    (d / "app.db").write_bytes(b"live file, must never be copied raw")
    (d / "ad-drafts" / "1").mkdir(parents=True)
    (d / "ad-drafts" / "1" / "foto1.jpg").write_bytes(b"\xff\xd8jpeg-1")
    (d / "ad-drafts" / "batch-2").mkdir()
    (d / "ad-drafts" / "batch-2" / "a.jpg").write_bytes(b"\xff\xd8jpeg-2")
    (d / "labels").mkdir()
    (d / "labels" / "label-1.pdf").write_bytes(b"%PDF-1.4 test")
    prof = d / "browser-profile"
    (prof / "Default" / "Cache").mkdir(parents=True)
    (prof / "Default" / "Cookies").write_bytes(b"cookie-db")
    (prof / "Default" / "Cache" / "data_0").write_bytes(b"cache")
    (prof / "Local State").write_text("{}")
    os.symlink("host-1234", prof / "SingletonLock")
    (d / "captures").mkdir()
    (d / "captures" / "chat.json").write_text("{}")
    with session() as db:
        db.add(SearchAgent(name="Max Muster Suche", source_url="https://www.willhaben.at/iad/x", webapi_url="x"))
        db.commit()
    set_setting("reply_rules", "Preis fix")
    return d


def _names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


def _manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        return json.loads(zf.read("manifest.json"))


# ------------------------------------------------------------------ export
def test_export_contents_and_manifest(ddir, tmp_path):
    out = tmp_path / "b.zip"
    m = backup.create_backup(out)
    names = _names(out)
    assert {"manifest.json", "app.db", "claude-token", "ha-token", "action-secret", "google-calendar.json",
            "ad-drafts/1/foto1.jpg", "ad-drafts/batch-2/a.jpg", "labels/label-1.pdf"} == names
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert m["format"] == backup.FORMAT and m["version"] == backup.FORMAT_VERSION
    assert m["app_version"] == __version__ and m["created_at"] and m["includes_profile"] is False
    with zipfile.ZipFile(out) as zf:
        for e in m["entries"]:
            data = zf.read(e["path"])
            assert len(data) == e["size"] and hashlib.sha256(data).hexdigest() == e["sha256"]
        db_bytes = zf.read("app.db")
    assert db_bytes.startswith(b"SQLite format 3")       # snapshot of the engine, not the raw file on disk
    snap = tmp_path / "snap.db"
    snap.write_bytes(db_bytes)
    con = sqlite3.connect(snap)
    assert con.execute("SELECT name FROM searchagent").fetchall() == [("Max Muster Suche",)]
    con.close()


def test_export_with_profile_skips_cache_and_locks(ddir, tmp_path):
    out = tmp_path / "b.zip"
    m = backup.create_backup(out, include_profile=True)
    names = _names(out)
    assert "browser-profile/Default/Cookies" in names and "browser-profile/Local State" in names
    assert not any("Cache" in n or "Singleton" in n for n in names)
    assert m["includes_profile"] is True


def test_encrypted_export_round_trip(ddir, tmp_path):
    out = tmp_path / "b.zip.enc"
    backup.create_backup(out, passphrase="richtig-lang")
    assert backup.is_encrypted(out)
    with pytest.raises(backup.NeedsPassphrase):
        backup.inspect_upload(io.BytesIO(out.read_bytes()))
    with pytest.raises(backup.BackupError, match="passphrase"):
        backup.inspect_upload(io.BytesIO(out.read_bytes()), "falsch-falsch")
    with pytest.raises(backup.BackupError):                          # cut off: last-chunk flag missing
        backup.inspect_upload(io.BytesIO(out.read_bytes()[:-40]), "richtig-lang")
    p = backup.inspect_upload(io.BytesIO(out.read_bytes()), "richtig-lang")
    assert "claude-token" in p.summary["secrets"] and p.summary["db_counts"]


def test_encryption_multi_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "SCRYPT_LOG2N", 10)
    monkeypatch.setattr(backup, "CHUNK", 1024)
    src, enc, dec = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    src.write_bytes(os.urandom(5000))
    backup.encrypt_file(src, enc, "pw-12345678")
    backup.decrypt_file(enc, dec, "pw-12345678")
    assert dec.read_bytes() == src.read_bytes()
    raw = enc.read_bytes()
    hdr = len(backup.MAGIC) + backup._HEADER.size
    swapped = raw[:hdr] + raw[hdr + 1040:hdr + 2080] + raw[hdr:hdr + 1040] + raw[hdr + 2080:]
    enc.write_bytes(swapped)
    with pytest.raises(backup.BackupError):
        backup.decrypt_file(enc, dec, "pw-12345678")


# ------------------------------------------------------------------ validation
def _zip(tmp_path, files: dict, manifest: dict | None = None, extra: list | None = None, fix=None) -> Path:
    entries = [{"path": n, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()} for n, b in files.items()]
    m = {"format": backup.FORMAT, "version": 1, "app_version": __version__, "created_at": "2026-10-01T10:00:00+00:00",
         "entries": entries}
    m.update(manifest or {})
    if fix:
        fix(m)
    p = tmp_path / "x.zip"
    with zipfile.ZipFile(p, "w") as zf:
        for n, b in files.items():
            zf.writestr(n, b)
        for info, b in extra or []:
            zf.writestr(info, b)
        zf.writestr("manifest.json", json.dumps(m))
    return p


def _db_bytes(tmp_path) -> bytes:
    p = tmp_path / "ok.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    con.close()
    return p.read_bytes()


@pytest.mark.parametrize("name", ["../evil", "ad-drafts/../../evil", "/etc/passwd", "ad-drafts\\x.jpg", "C:/x",
                                  "ad-drafts//x.jpg", "./claude-token"])
def test_rejects_unsafe_names(ddir, tmp_path, name):
    p = _zip(tmp_path, {"app.db": _db_bytes(tmp_path), name: b"x"})
    with pytest.raises(backup.BackupError, match="(?i)unsafe"):
        backup.validate_zip(p)


def test_rejects_symlink_member(ddir, tmp_path):
    info = zipfile.ZipInfo("ad-drafts/link.jpg")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    p = _zip(tmp_path, {"app.db": _db_bytes(tmp_path)}, extra=[(info, "/etc/passwd")])
    with pytest.raises(backup.BackupError, match="link"):
        backup.validate_zip(p)


def test_rejects_bad_checksum(ddir, tmp_path):
    def fix(m):
        m["entries"][1]["sha256"] = "0" * 64
    p = _zip(tmp_path, {"app.db": _db_bytes(tmp_path), "ha-token": b"abc"}, fix=fix)
    with pytest.raises(backup.BackupError, match="Checksum"):
        backup.validate_zip(p)


def test_rejects_unknown_version_and_foreign_zip(ddir, tmp_path):
    p = _zip(tmp_path, {"app.db": _db_bytes(tmp_path)}, manifest={"version": 99})
    with pytest.raises(backup.BackupError, match="99"):
        backup.validate_zip(p)
    p = _zip(tmp_path, {"app.db": _db_bytes(tmp_path)}, manifest={"format": "something-else"})
    with pytest.raises(backup.BackupError, match="format"):
        backup.validate_zip(p)
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("app.db", b"x")
    with pytest.raises(backup.BackupError, match="manifest"):
        backup.validate_zip(plain)
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip at all")
    with pytest.raises(backup.BackupError, match="ZIP"):
        backup.validate_zip(junk)


def test_rejects_unlisted_unexpected_and_wrong_size(ddir, tmp_path):
    def drop(m):
        m["entries"] = m["entries"][:1]
    p = _zip(tmp_path, {"app.db": _db_bytes(tmp_path), "ha-token": b"abc"}, fix=drop)
    with pytest.raises(backup.BackupError, match="do not match"):
        backup.validate_zip(p)
    p = _zip(tmp_path, {"app.db": _db_bytes(tmp_path), "evil.sh": b"rm -rf /"})
    with pytest.raises(backup.BackupError, match="Unexpected"):
        backup.validate_zip(p)
    p = _zip(tmp_path, {"app.db": _db_bytes(tmp_path), "captures/x.json": b"{}"})
    with pytest.raises(backup.BackupError, match="Unexpected"):
        backup.validate_zip(p)

    def size(m):
        m["entries"][1]["size"] = 1
    p = _zip(tmp_path, {"app.db": _db_bytes(tmp_path), "ha-token": b"abc"}, fix=size)
    with pytest.raises(backup.BackupError, match="Size"):
        backup.validate_zip(p)
    p = _zip(tmp_path, {"ha-token": b"abc"})
    with pytest.raises(backup.BackupError, match="database"):
        backup.validate_zip(p)


def test_size_limit(ddir, tmp_path, monkeypatch):
    p = _zip(tmp_path, {"app.db": _db_bytes(tmp_path), "ad-drafts/1/a.jpg": b"x" * 5000})
    monkeypatch.setattr(backup, "MAX_TOTAL", 4096)
    with pytest.raises(backup.BackupError, match="too large"):
        backup.validate_zip(p)
    monkeypatch.setattr(backup, "MAX_UPLOAD", 100)
    with pytest.raises(backup.BackupError, match="too large"):
        backup.inspect_upload(io.BytesIO(p.read_bytes()))
    assert not list(backup._pending_dir().iterdir())          # nothing left behind


def test_rejects_damaged_database(ddir, tmp_path):
    p = _zip(tmp_path, {"app.db": b"SQLite format 3\x00" + b"\x00" * 200})
    with pytest.raises(backup.BackupError, match="database"):
        backup.inspect_upload(io.BytesIO(p.read_bytes()))


# ------------------------------------------------------------------ safety backups
def test_safety_backup_rotation(ddir):
    bdir = backup.backups_dir()
    for i in range(7):
        f = bdir / f"pre-import-2026010{i}-120000.zip"
        f.write_bytes(b"x")
        os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))
    backup.rotate_safety()
    left = sorted(p.name for p in bdir.glob("pre-import-*.zip"))
    assert left == [f"pre-import-2026010{i}-120000.zip" for i in range(2, 7)]
    new = backup.safety_backup(include_profile=False)
    assert new.exists() and len(list(bdir.glob("pre-import-*.zip"))) == backup.KEEP_SAFETY
    assert backup.validate_zip(new)["includes_profile"] is False
    assert backup.safety_path(new.name) == new
    assert backup.safety_path("../app.db") is None and backup.safety_path("pending") is None


# ------------------------------------------------------------------ round trip
def test_import_round_trip(ddir, tmp_path):
    out = tmp_path / "b.zip"
    backup.create_backup(out, include_profile=True)
    # the state changes after the backup ...
    with session() as db:
        for a in db.exec(select(SearchAgent)).all():
            db.delete(a)
        db.add(SearchAgent(name="Neu", source_url="u", webapi_url="u"))
        db.commit()
    set_setting("reply_rules", "anders")
    (ddir / "claude-token").write_text("other-token")
    (ddir / "telegram-token").write_text("123:new")
    (ddir / "ad-drafts" / "1" / "foto1.jpg").unlink()
    (ddir / "ad-drafts" / "9").mkdir()
    (ddir / "ad-drafts" / "9" / "new.jpg").write_bytes(b"new")
    (ddir / "browser-profile" / "Default" / "Cookies").write_bytes(b"other-cookies")

    pending = backup.inspect_upload(io.BytesIO(out.read_bytes()))
    s = pending.summary
    assert s["photos"] == 2 and s["labels"] == 1 and s["profile"] and s["same_version"]
    assert s["db_counts"]["search agents"] == 1 and "claude-token" in s["secrets"]
    assert s["created_at"].tzinfo is not None
    res = backup.restore(pending.token)

    with session() as db:
        assert [a.name for a in db.exec(select(SearchAgent)).all()] == ["Max Muster Suche"]
    assert get_setting("reply_rules") == "Preis fix"
    tok = ddir / "claude-token"
    assert tok.read_text() == "sk-ant-oat-test" and stat.S_IMODE(tok.stat().st_mode) == 0o600
    assert not (ddir / "telegram-token").exists()            # not part of the restored state
    assert (ddir / "ad-drafts" / "1" / "foto1.jpg").read_bytes() == b"\xff\xd8jpeg-1"
    assert not (ddir / "ad-drafts" / "9").exists()
    assert (ddir / "browser-profile" / "Default" / "Cookies").read_bytes() == b"cookie-db"
    assert (ddir / "notes.txt").exists() and (ddir / "captures" / "chat.json").exists()   # untouched
    assert not list(backup._pending_dir().iterdir())
    assert not [p for p in backup.backups_dir().iterdir() if p.name.startswith("restore-")]
    # the safety backup holds the state from just before the import
    safety = backup.backups_dir() / res["safety"]
    with zipfile.ZipFile(safety) as zf:
        assert zf.read("claude-token") == b"other-token" and "ad-drafts/9/new.jpg" in zf.namelist()
        assert zf.read("browser-profile/Default/Cookies") == b"other-cookies"
    with pytest.raises(backup.BackupError):
        backup.restore(pending.token)                         # a pending import is used once


def test_restore_without_profile_keeps_login(ddir, tmp_path):
    out = tmp_path / "b.zip"
    backup.create_backup(out)
    (ddir / "browser-profile" / "Default" / "Cookies").write_bytes(b"current-login")
    pending = backup.inspect_upload(io.BytesIO(out.read_bytes()))
    assert not pending.summary["profile"]
    backup.restore(pending.token)
    assert (ddir / "browser-profile" / "Default" / "Cookies").read_bytes() == b"current-login"


# ------------------------------------------------------------------ web
def test_web_flow(ddir, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    restarts = []
    monkeypatch.setattr(backup, "schedule_restart", lambda *a, **k: restarts.append(1))
    monkeypatch.setattr(get_settings(), "dashboard_password", "pw-test")
    import base64
    basic = base64.b64encode(f"{get_settings().dashboard_user}:pw-test".encode()).decode()
    with TestClient(app, headers={"Authorization": f"Basic {basic}"}) as c:
        page = c.get("/settings/backup").text
        assert "/settings/backup/export" in page
        r = c.post("/settings/backup/export", data={"passphrase": "abc", "passphrase2": "abd"}, follow_redirects=False)
        assert r.status_code == 303 and "/settings/backup?" in r.headers["location"]
        r = c.post("/settings/backup/export", data={})
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert "attachment" in r.headers["content-disposition"]
        blob = r.content
        assert not [p for p in backup.backups_dir().iterdir() if p.name.startswith("export-")]   # temp file removed
        r = c.post("/settings/backup/import", files={"file": ("b.zip", b"junk")}, follow_redirects=False)
        assert r.status_code == 303
        r = c.post("/settings/backup/import", files={"file": ("b.zip", blob)})
        assert r.status_code == 200 and 'name="confirm"' in r.text
        token = r.text.split('name="token" value="')[1].split('"')[0]
        assert c.get(f"/settings/backup/preview/{token}").status_code == 200
        r = c.post("/settings/backup/restore", data={"token": token}, follow_redirects=False)
        assert r.status_code == 303 and not restarts                  # no confirm tick → nothing happens
        r = c.post("/settings/backup/restore", data={"token": token, "confirm": "on"})
        assert r.status_code == 200 and "/healthz" in r.text and restarts == [1]
        safety = backup.safety_backups()
        assert len(safety) == 1
        assert c.get(f"/settings/backup/file/{safety[0]['name']}").status_code == 200
        assert c.get("/settings/backup/file/app.db").status_code == 404


def test_downloads_need_dashboard_password(ddir, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(get_settings(), "dashboard_password", None)
    backup.safety_backup(include_profile=False)
    name = backup.safety_backups()[0]["name"]
    with TestClient(app) as c:
        page = c.get("/settings/backup").text
        assert "Download backup" not in page and "DASHBOARD_PASSWORD" in page
        assert f"/settings/backup/file/{name}" not in page
        r = c.post("/settings/backup/export", data={}, follow_redirects=False)
        assert r.status_code == 303 and "/settings/backup?" in r.headers["location"]
        assert c.get(f"/settings/backup/file/{name}").status_code == 403
    assert not [p for p in backup.backups_dir().iterdir() if p.name.startswith("export-")]


def test_import_refuses_oversized_body_before_parsing(ddir, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(backup, "MAX_UPLOAD", 10)
    parsed = []
    from starlette.requests import Request
    orig = Request.form
    monkeypatch.setattr(Request, "form", lambda self, *a, **k: parsed.append(1) or orig(self, *a, **k))
    with TestClient(app) as c:
        r = c.post("/settings/backup/import", files={"file": ("b.zip", b"x" * (2 << 20))}, follow_redirects=False)
    assert r.status_code == 303 and not parsed
    assert not list(backup._pending_dir().iterdir())


def test_pending_upload_expires(ddir, tmp_path):
    out = tmp_path / "b.zip"
    backup.create_backup(out)
    pending = backup.inspect_upload(io.BytesIO(out.read_bytes()))
    old = time.time() - backup.PENDING_TTL_S - 60
    for p in backup._pending_dir().iterdir():
        os.utime(p, (old, old))
    from app.backup_routes import panel
    panel()                                                  # the settings page purges stale previews
    assert not list(backup._pending_dir().iterdir())
    with pytest.raises(backup.BackupError):
        backup.load_pending(pending.token)
    # a fresh one survives the TTL purge
    fresh = backup.inspect_upload(io.BytesIO(out.read_bytes()))
    backup._purge_pending()
    assert backup.load_pending(fresh.token).token == fresh.token


def test_backup_dirs_are_private(ddir, tmp_path):
    (ddir / "backups").mkdir(mode=0o755)
    os.chmod(ddir / "backups", 0o755)
    assert stat.S_IMODE(backup.backups_dir().stat().st_mode) == 0o700
    assert stat.S_IMODE(backup._pending_dir().stat().st_mode) == 0o700
    seen = []
    orig = backup._db_snapshot

    def snap(dest):
        orig(dest)
        seen.append(stat.S_IMODE(dest.stat().st_mode))
    backup._db_snapshot = snap
    try:
        backup.create_backup(backup.backups_dir() / "x.zip")
    finally:
        backup._db_snapshot = orig
    assert seen == [0o600]


def test_empty_profile_in_backup_still_replaces_login(ddir, tmp_path):
    import shutil
    shutil.rmtree(ddir / "browser-profile")
    out = tmp_path / "b.zip"
    backup.create_backup(out, include_profile=True)
    assert _manifest(out)["includes_profile"] is True
    (ddir / "browser-profile" / "Default").mkdir(parents=True)
    (ddir / "browser-profile" / "Default" / "Cookies").write_bytes(b"other-login")
    pending = backup.inspect_upload(io.BytesIO(out.read_bytes()))
    assert pending.summary["profile"] and pending.summary["profile_files"] == 0
    backup.restore(pending.token)
    assert not (ddir / "browser-profile" / "Default" / "Cookies").exists()


def test_restore_stops_writers_first(ddir, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient

    from app import main
    from app import scheduler as sched
    from app.services import chats
    from app.willhaben.session import get_session
    monkeypatch.setattr(backup, "schedule_restart", lambda *a, **k: None)
    monkeypatch.setattr(get_settings(), "dashboard_password", None)
    out = ddir.parent / "b.zip"
    backup.create_backup(out)
    pending = backup.inspect_upload(io.BytesIO(out.read_bytes()))
    sess = get_session()
    stops, state = [], {}

    async def fake_stop():
        stops.append(1)
    monkeypatch.setattr(sess, "stop", fake_stop)
    orig_restore_db = backup._restore_db

    def check_restore_db(path):
        state.update(cb=sess._chat_cb, sched=sched.scheduler.running, stops=len(stops),
                     tasks=[x.cancelled() for x in tasks])
        orig_restore_db(path)
    monkeypatch.setattr(backup, "_restore_db", check_restore_db)
    tasks: list = []

    async def forever():
        await asyncio.sleep(3600)

    async def arm():
        sess._chat_cb = chats.on_live_event
        for bucket in ("live", "humanized", "background", "listener"):
            task = asyncio.create_task(forever())
            tasks.append(task)
            if bucket == "live":
                chats._live["task"] = task
            elif bucket == "humanized":
                chats._humanized["conv-1"] = task
            elif bucket == "background":
                main._background.add(task)
            else:
                main.app.state.listener_tasks = [task]

    with TestClient(main.app) as c:
        c.portal.call(arm)
        r = c.post("/settings/backup/restore", data={"token": pending.token, "confirm": "on"})
        assert r.status_code == 200
    chats._humanized.pop("conv-1", None)
    chats._live["task"] = None
    main._background.difference_update(tasks)
    assert state == {"cb": None, "sched": False, "stops": 1, "tasks": [True, True, True, True]}
