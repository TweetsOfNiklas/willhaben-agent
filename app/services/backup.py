"""Backup export / import of everything in DATA_DIR that makes this installation (Settings → Backup).

A backup is a ZIP with the same relative paths as DATA_DIR plus a `manifest.json`:

    app.db                    consistent copy via the sqlite3 backup API (never a raw copy of the live file)
    claude-token, ha-token, telegram-token, action-secret, google-calendar.json, other small *-token/*-secret/*.json
    ad-drafts/...             ad photos (single drafts and batches)
    labels/...                downloaded parcel labels
    browser-profile/...       optional: the willhaben login (Chromium profile without caches and Singleton* locks)

Not included: captures/ (manual raw request recordings with chat content) and backups/ itself. The timeline and the
willhaben request recordings (Settings → Timeline, opt-in) are tables in app.db and therefore included.

Optionally the whole ZIP is encrypted with a passphrase (scrypt → AES-256-GCM in 1 MiB chunks, STREAM
construction: nonce = prefix | counter | last-flag, so chunks cannot be reordered, dropped or cut off).

Import: upload → `inspect_upload()` validates (manifest, version, names, sizes, sha256, SQLite check) and parks the
plain ZIP in backups/pending/ → preview page → `restore()` after an explicit confirm. `restore()` first writes a
safety backup backups/pre-import-<ts>.zip (last 5 kept), then copies app.db into the live database with the
sqlite3 backup API, writes secrets atomically with mode 600 and swaps the photo/label/profile folders. The app
then restarts itself (SIGTERM → uvicorn shuts down cleanly → supervisord `autorestart=true` starts it again).

Needs live verification: the self-restart under supervisord in the container, and that a browser profile copied
while Chromium is running restores a working willhaben login (Chromium's own SQLite files are copied as files).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import struct
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from app import __version__
from app.config import get_settings
from app.i18n import t

log = logging.getLogger(__name__)

FORMAT = "secondhandler-backup"
OLD_FORMATS = {"willhaben-agent-backup"}   # backups made before the rename still restore
FORMAT_VERSION = 1
SUPPORTED_VERSIONS = {1}
MANIFEST = "manifest.json"
DB_NAME = "app.db"
TREES = ("ad-drafts", "labels", "wireguard", "hit-images")   # always part of a backup (wireguard: the VPN configs,
#                                                              Settings → VPN; hit-images: saved hits' kept photos)
PROFILE = "browser-profile"               # only on request (can be large)
TREE_KINDS = {"ad-drafts": "photo", "labels": "label", "wireguard": "vpn", "hit-images": "photo", PROFILE: "profile"}
KNOWN_SECRETS = ("claude-token", "ha-token", "telegram-token", "action-secret", "google-calendar.json")
_SECRET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}(?:-token|-secret|\.json)")
SECRET_MAX = 1 << 20                      # a token/secret file larger than 1 MiB is not a token

# limits for uploads (an import must never fill the disk or run out of memory)
MAX_UPLOAD = 8 << 30
MAX_TOTAL = 16 << 30
MAX_MEMBER = 4 << 30
MAX_MEMBERS = 200_000
MAX_RATIO = 500                           # uncompressed/compressed above this (and > 1 MiB) = zip bomb
MANIFEST_MAX = 64 << 20
KEEP_SAFETY = 5
PENDING_TTL_S = 3 * 3600

# Chromium folders that are pure cache, and lock files that must never travel
PROFILE_SKIP_DIRS = {
    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache", "GraphiteDawnCache", "DawnCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "CacheStorage", "ScriptCache", "Crashpad", "component_crx_cache",
    "extensions_crx_cache", "optimization_guide_model_store", "Safe Browsing", "BrowserMetrics",
}
_STORED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".pdf", ".zip", ".gz"}

# encryption container
MAGIC = b"WHABAK-ENC1\n"
CHUNK = 1 << 20
SCRYPT_LOG2N = 17                         # 128 MiB memory; tests lower it
SCRYPT_R, SCRYPT_P = 8, 1
_HEADER = struct.Struct(">16sBBB7sI")    # salt, log2(n), r, p, nonce prefix, chunk size


class BackupError(Exception):
    """Validation or restore problem; the message is shown to the owner."""


class NeedsPassphrase(BackupError):
    pass


@dataclass
class Pending:
    token: str
    manifest: dict[str, Any]
    summary: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ paths
def data_dir() -> Path:
    return get_settings().data_dir


def _private_dir(d: Path) -> Path:
    """Backups hold every token: their folders are owner-only (./data is a host bind mount)."""
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if stat.S_IMODE(d.stat().st_mode) != 0o700:
            os.chmod(d, 0o700)
    except OSError as e:
        log.warning("backup: cannot restrict %s: %s", d, e)
    return d


def backups_dir() -> Path:
    return _private_dir(data_dir() / "backups")


def _pending_dir() -> Path:
    return _private_dir(backups_dir() / "pending")


def has_crypto() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt  # noqa: F401
    except ImportError:
        return False
    return True


def is_secret_name(name: str) -> bool:
    return name in KNOWN_SECRETS or bool(_SECRET_RE.fullmatch(name))


def secret_files(root: Path | None = None) -> list[Path]:
    """Small token/secret/JSON files directly in DATA_DIR (symlinks and big files are left out)."""
    root = root or data_dir()
    out = []
    for p in sorted(root.iterdir()) if root.exists() else []:
        try:
            st = p.lstat()
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_size <= SECRET_MAX and is_secret_name(p.name):
            out.append(p)
    return out


def _walk(base: Path, prefix: str, profile: bool = False) -> Iterator[tuple[Path, str]]:
    if not base.is_dir() or base.is_symlink():
        return
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        if profile:
            dirnames[:] = [d for d in dirnames if d not in PROFILE_SKIP_DIRS]
        dirnames.sort()
        for name in sorted(filenames):
            if profile and name.startswith("Singleton"):
                continue
            p = Path(dirpath) / name
            try:
                if not stat.S_ISREG(p.lstat().st_mode):    # symlinks, sockets, fifos never go in
                    continue
            except OSError:
                continue
            yield p, f"{prefix}/{p.relative_to(base).as_posix()}"


# ------------------------------------------------------------------ export
def _db_snapshot(dest: Path) -> None:
    """Consistent copy of the live database through the app's own engine (works for file and in-memory DBs)."""
    from app.db import get_engine

    with get_engine().connect() as conn:
        src = conn.connection.driver_connection
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()


def _add_file(zf: zipfile.ZipFile, src: Path | BinaryIO, arcname: str, entries: list[dict], kind: str) -> None:
    fh = open(src, "rb") if isinstance(src, Path) else src     # opened first: a vanished file adds nothing
    with fh:
        info = zipfile.ZipInfo(arcname, date_time=time.localtime()[:6])
        info.external_attr = (stat.S_IFREG | 0o600) << 16
        info.compress_type = zipfile.ZIP_STORED if Path(arcname).suffix.lower() in _STORED_EXT else zipfile.ZIP_DEFLATED
        h, size = hashlib.sha256(), 0
        with zf.open(info, "w", force_zip64=True) as out:
            while chunk := fh.read(CHUNK):
                h.update(chunk)
                size += len(chunk)
                out.write(chunk)
    entries.append({"path": arcname, "size": size, "sha256": h.hexdigest(), "kind": kind})


def create_backup(dest: Path, include_profile: bool = False, passphrase: str | None = None) -> dict[str, Any]:
    """Write a backup ZIP (or its encrypted form) to `dest`; returns the manifest."""
    root = data_dir()
    dest.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)    # .part and .db snapshot hold every token: owner-only from the start
    part = dest.with_name(dest.name + f".{secrets.token_hex(4)}.part")
    snap = dest.with_name(dest.name + f".{secrets.token_hex(4)}.db")
    entries: list[dict[str, Any]] = []
    try:
        _db_snapshot(snap)
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            _add_file(zf, snap, DB_NAME, entries, "database")
            for p in secret_files(root):
                _add_file(zf, p, p.name, entries, "secret")
            trees = [(t_, False, TREE_KINDS[t_]) for t_ in TREES]
            if include_profile:
                trees.append((PROFILE, True, "profile"))
            for tree, is_profile, kind in trees:
                for p, arc in _walk(root / tree, tree, profile=is_profile):
                    try:
                        _add_file(zf, p, arc, entries, kind)
                    except (FileNotFoundError, PermissionError) as e:   # Chromium deletes temp files while we walk
                        log.info("backup: skipped %s (%s)", arc, e)
            manifest = {
                "format": FORMAT, "version": FORMAT_VERSION, "app_version": __version__,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "includes_profile": bool(include_profile), "entries": entries,
            }
            zf.writestr(MANIFEST, json.dumps(manifest, indent=1, ensure_ascii=False))
        if passphrase:
            encrypt_file(part, dest, passphrase)
            part.unlink()
        else:
            os.replace(part, dest)
        os.chmod(dest, 0o600)
        return manifest
    finally:
        os.umask(old_umask)
        part.unlink(missing_ok=True)
        snap.unlink(missing_ok=True)


def purge_exports(max_age_s: int = 3600) -> None:
    """Downloads are written to backups/export-* first; drop leftovers of interrupted downloads."""
    now = time.time()
    for p in backups_dir().glob("export-*"):
        try:
            if now - p.stat().st_mtime > max_age_s:
                p.unlink()
        except OSError:
            pass


def export_name(encrypted: bool) -> str:
    return f"secondhandler-backup-{datetime.now():%Y%m%d-%H%M%S}.zip" + (".enc" if encrypted else "")


# ------------------------------------------------------------------ encryption
def _key(passphrase: str, salt: bytes, log2n: int, r: int, p: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    if not (10 <= log2n <= 22 and 1 <= r <= 32 and 1 <= p <= 16):
        raise BackupError(t("The backup's encryption header is damaged."))
    return Scrypt(salt=salt, length=32, n=1 << log2n, r=r, p=p).derive(passphrase.encode("utf-8"))


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    if not has_crypto():
        raise BackupError(t("Encryption is not available in this installation."))
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, prefix = secrets.token_bytes(16), secrets.token_bytes(7)
    header = MAGIC + _HEADER.pack(salt, SCRYPT_LOG2N, SCRYPT_R, SCRYPT_P, prefix, CHUNK)
    aead = AESGCM(_key(passphrase, salt, SCRYPT_LOG2N, SCRYPT_R, SCRYPT_P))
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        os.chmod(dst, 0o600)
        fout.write(header)
        cur, n = fin.read(CHUNK), 0
        while True:
            nxt = fin.read(CHUNK)
            last = not nxt
            nonce = prefix + struct.pack(">I", n) + (b"\x01" if last else b"\x00")
            fout.write(aead.encrypt(nonce, cur, header))
            if last:
                break
            cur, n = nxt, n + 1


def is_encrypted(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(len(MAGIC)) == MAGIC


def decrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    if not has_crypto():
        raise BackupError(t("This backup is encrypted, but encryption is not available in this installation."))
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    bad = t("Wrong passphrase or damaged backup.")
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        os.chmod(dst, 0o600)
        header = fin.read(len(MAGIC) + _HEADER.size)
        if len(header) != len(MAGIC) + _HEADER.size or not header.startswith(MAGIC):
            raise BackupError(bad)
        salt, log2n, r, p, prefix, chunk = _HEADER.unpack(header[len(MAGIC):])
        if not 1024 <= chunk <= 64 << 20:
            raise BackupError(bad)
        aead = AESGCM(_key(passphrase, salt, log2n, r, p))
        size = chunk + 16
        cur, n, done = fin.read(size), 0, False
        while cur:
            nxt = fin.read(size)
            last = not nxt
            nonce = prefix + struct.pack(">I", n) + (b"\x01" if last else b"\x00")
            try:
                fout.write(aead.decrypt(nonce, cur, header))
            except InvalidTag:
                raise BackupError(bad) from None
            if last:
                done = True
                break
            cur, n = nxt, n + 1
        if not done:
            raise BackupError(bad)


# ------------------------------------------------------------------ validation
def check_name(name: str) -> None:
    """Member names must be plain relative paths below DATA_DIR."""
    bad = t("Unsafe file name in backup: {name}", name=name[:200])
    if not name or len(name) > 1024 or "\\" in name or "\x00" in name or name.startswith("/"):
        raise BackupError(bad)
    parts = name.split("/")
    if ":" in parts[0] or any(p in ("", ".", "..") for p in parts):
        raise BackupError(bad)


def _allowed_target(name: str) -> bool:
    if name == DB_NAME:
        return True
    if "/" not in name:
        return is_secret_name(name)
    return name.split("/", 1)[0] in (*TREES, PROFILE)


def _manifest(zf: zipfile.ZipFile) -> dict[str, Any]:
    try:
        info = zf.getinfo(MANIFEST)
    except KeyError:
        raise BackupError(t("Not a backup of this app (manifest.json missing).")) from None
    if info.file_size > MANIFEST_MAX:
        raise BackupError(t("manifest.json is too large."))
    try:
        m = json.loads(zf.read(MANIFEST))
    except (ValueError, zipfile.BadZipFile, OSError) as e:
        raise BackupError(t("manifest.json is unreadable: {error}", error=e)) from None
    if not isinstance(m, dict) or m.get("format") not in {FORMAT, *OLD_FORMATS}:
        raise BackupError(t("Not a backup of this app (unknown format)."))
    if m.get("version") not in SUPPORTED_VERSIONS:
        raise BackupError(t("Backup format version {v} is not supported by this app version.", v=m.get("version")))
    entries = m.get("entries")
    if not isinstance(entries, list):
        raise BackupError(t("manifest.json has no file list."))
    for e in entries:
        if not (isinstance(e, dict) and isinstance(e.get("path"), str) and isinstance(e.get("size"), int)
                and e["size"] >= 0 and isinstance(e.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", e["sha256"])):
            raise BackupError(t("manifest.json has an invalid entry."))
    return m


def validate_zip(path: Path) -> dict[str, Any]:
    """Full check of a plain backup ZIP; returns the manifest. Raises BackupError."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise BackupError(t("The file is not a ZIP backup.")) from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise BackupError(t("The backup contains too many files."))
        members: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            check_name(info.filename)
            mode = info.external_attr >> 16
            if info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG):   # links, folders, devices
                raise BackupError(t("The backup contains a link or folder entry: {name}", name=info.filename[:200]))
            if info.filename in members:
                raise BackupError(t("The backup contains {name} twice.", name=info.filename[:200]))
            if info.flag_bits & 0x1:
                raise BackupError(t("Password-protected ZIP files are not supported."))
            members[info.filename] = info
        m = _manifest(zf)
        listed = {e["path"]: e for e in m["entries"]}
        if len(listed) != len(m["entries"]):
            raise BackupError(t("manifest.json lists a file twice."))
        if set(listed) != set(members) - {MANIFEST}:
            diff = sorted(set(listed) ^ (set(members) - {MANIFEST}))
            raise BackupError(t("Files and manifest.json do not match: {names}", names=", ".join(diff[:5])))
        if DB_NAME not in listed:
            raise BackupError(t("The backup contains no database."))
        total = 0
        for name, e in listed.items():
            check_name(name)
            if not _allowed_target(name):
                raise BackupError(t("Unexpected file in backup: {name}", name=name[:200]))
            info = members[name]
            if info.file_size != e["size"] or e["size"] > MAX_MEMBER:
                raise BackupError(t("Size of {name} does not match.", name=name[:200]))
            if info.file_size > (1 << 20) and info.file_size > MAX_RATIO * max(1, info.compress_size):
                raise BackupError(t("Size of {name} does not match.", name=name[:200]))
            if "/" not in name and name != DB_NAME and e["size"] > SECRET_MAX:
                raise BackupError(t("Size of {name} does not match.", name=name[:200]))
            total += e["size"]
        if total > MAX_TOTAL:
            raise BackupError(t("The backup is too large to restore."))
        for name, e in listed.items():
            _copy_checked(zf, name, e, None)
    return m


def _copy_checked(zf: zipfile.ZipFile, name: str, entry: dict, out: BinaryIO | None) -> None:
    """Stream one member, never reading more than the manifest size, and compare its sha256."""
    h, size = hashlib.sha256(), 0
    try:
        with zf.open(name) as src:
            while chunk := src.read(CHUNK):
                size += len(chunk)
                if size > entry["size"]:
                    break
                h.update(chunk)
                if out is not None:
                    out.write(chunk)
    except (zipfile.BadZipFile, OSError, EOFError, ValueError) as e:     # CRC error, truncated stream, ...
        raise BackupError(t("{name} is damaged: {error}", name=name[:200], error=e)) from None
    if size != entry["size"] or h.hexdigest() != entry["sha256"]:
        raise BackupError(t("Checksum of {name} does not match.", name=name[:200]))


def _db_summary(path: Path) -> dict[str, int]:
    tables = {"searchagent": t("search agents"), "hit": t("hits"), "conversation": t("chats"), "myad": t("own ads"),
              "addraft": t("ad drafts")}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise BackupError(t("The database in the backup is unreadable: {error}", error=e)) from None
    try:
        ok = con.execute("PRAGMA quick_check").fetchone()
        if not ok or ok[0] != "ok":
            raise BackupError(t("The database in the backup is damaged."))
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {label: con.execute(f'SELECT COUNT(*) FROM "{tb}"').fetchone()[0] for tb, label in tables.items() if tb in have}
    except sqlite3.DatabaseError as e:
        raise BackupError(t("The database in the backup is unreadable: {error}", error=e)) from None
    finally:
        con.close()


def summarize(path: Path, m: dict[str, Any]) -> dict[str, Any]:
    entries = m["entries"]

    def of(kind: str) -> list[dict]:
        return [e for e in entries if _kind(e["path"]) == kind]
    tmp = path.with_name(path.name + ".check.db")
    try:
        with zipfile.ZipFile(path) as zf, os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
            _copy_checked(zf, DB_NAME, next(e for e in entries if e["path"] == DB_NAME), out)
        counts = _db_summary(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    try:
        created = datetime.fromisoformat(m.get("created_at", ""))
    except (TypeError, ValueError):
        created = None
    return {
        "created_at": created, "app_version": str(m.get("app_version") or "?"),
        "same_version": m.get("app_version") == __version__,
        "db_size": sum(e["size"] for e in of("database")), "db_counts": counts,
        "secrets": sorted(e["path"] for e in of("secret")),
        "photos": len(of("photo")), "photos_size": sum(e["size"] for e in of("photo")),
        "labels": len(of("label")), "vpn_configs": len(of("vpn")),
        "profile": includes_profile(m), "profile_files": len(of("profile")),
        "profile_size": sum(e["size"] for e in of("profile")),
        "total_size": sum(e["size"] for e in entries),
    }


def includes_profile(m: dict[str, Any]) -> bool:
    """The manifest flag decides (an empty profile in the backup still replaces the current one), plus any
    profile entries as a fallback for hand-made manifests."""
    return bool(m.get("includes_profile")) or any(_kind(e["path"]) == "profile" for e in m["entries"])


def _kind(name: str) -> str:
    if name == DB_NAME:
        return "database"
    if "/" not in name:
        return "secret"
    return TREE_KINDS[name.split("/", 1)[0]]


# ------------------------------------------------------------------ import: upload → pending
def _purge_pending(everything: bool = False) -> None:
    """Drop parked (decrypted!) uploads: all of them, or those older than PENDING_TTL_S. Called from the settings
    panel, export, preview and restore, so an abandoned preview does not leave a plaintext backup lying around."""
    now = time.time()
    for p in _pending_dir().iterdir():
        try:
            if everything or now - p.stat().st_mtime > PENDING_TTL_S:
                p.unlink()
        except OSError:
            pass


def inspect_upload(upload: BinaryIO, passphrase: str | None = None) -> Pending:
    """Store an uploaded backup, decrypt it if needed, validate it fully and return the preview."""
    _purge_pending(everything=True)          # only one import waits for confirmation at a time
    token = secrets.token_urlsafe(16)
    raw = _pending_dir() / f"{token}.upload"
    plain = _pending_dir() / f"{token}.zip"
    try:
        size = 0
        fd = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            while chunk := upload.read(CHUNK):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise BackupError(t("The backup is too large to restore."))
                out.write(chunk)
        if is_encrypted(raw):
            if not passphrase:
                raise NeedsPassphrase(t("This backup is encrypted. Enter its passphrase."))
            decrypt_file(raw, plain, passphrase)
            raw.unlink()
        else:
            os.replace(raw, plain)
        m = validate_zip(plain)
        pending = Pending(token, m, summarize(plain, m))
        (_pending_dir() / f"{token}.json").write_text(json.dumps(m), encoding="utf-8")
        return pending
    except BaseException:
        raw.unlink(missing_ok=True)
        plain.unlink(missing_ok=True)
        raise


def _pending_zip(token: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,64}", token or ""):
        raise BackupError(t("This import is no longer available. Upload the backup again."))
    p = _pending_dir() / f"{token}.zip"
    if not p.exists():
        raise BackupError(t("This import is no longer available. Upload the backup again."))
    return p


def load_pending(token: str) -> Pending:
    _purge_pending()
    p = _pending_zip(token)
    m = json.loads((_pending_dir() / f"{token}.json").read_text(encoding="utf-8"))
    return Pending(token, m, summarize(p, m))


def cancel_pending(token: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9_-]{10,64}", token or ""):
        for suffix in (".zip", ".json", ".upload"):
            (_pending_dir() / f"{token}{suffix}").unlink(missing_ok=True)


# ------------------------------------------------------------------ safety backups
def safety_backups() -> list[dict[str, Any]]:
    out = []
    for p in sorted(backups_dir().glob("pre-import-*.zip"), reverse=True):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size,
                    "created_at": datetime.fromtimestamp(st.st_mtime, timezone.utc)})
    return out


def safety_backup(include_profile: bool) -> Path:
    """Current state before an import: backups/pre-import-<timestamp>.zip, only the newest KEEP_SAFETY kept."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = backups_dir() / f"pre-import-{stamp}.zip"
    n = 1
    while dest.exists():
        dest = backups_dir() / f"pre-import-{stamp}-{n}.zip"
        n += 1
    create_backup(dest, include_profile=include_profile)
    rotate_safety()
    return dest


def rotate_safety(keep: int = KEEP_SAFETY) -> None:
    files = sorted(backups_dir().glob("pre-import-*.zip"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    for p in files[keep:]:
        p.unlink(missing_ok=True)


def safety_path(name: str) -> Path | None:
    if not re.fullmatch(r"pre-import-[0-9-]+\.zip", name or ""):
        return None
    p = backups_dir() / name
    return p if p.is_file() else None


# ------------------------------------------------------------------ restore
def _write_secret(dest: Path, entry: dict, zf: zipfile.ZipFile) -> None:
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            _copy_checked(zf, entry["path"], entry, out)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _restore_db(path: Path) -> None:
    from app.db import get_engine

    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with get_engine().connect() as conn:
            src.backup(conn.connection.driver_connection)
    finally:
        src.close()


def restore(token: str) -> dict[str, Any]:
    """Apply a validated pending backup to DATA_DIR. The caller stops the browser first if the profile is included
    and restarts the app afterwards. Returns {"safety": <file name>, "manifest": ...}."""
    root = data_dir()
    zpath = _pending_zip(token)
    m = validate_zip(zpath)                        # again: the file sat on disk between preview and confirm
    entries = {e["path"]: e for e in m["entries"]}
    has_profile = includes_profile(m)
    safety = safety_backup(include_profile=has_profile)
    stage = backups_dir() / f"restore-{secrets.token_hex(4)}"
    stage.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(zpath) as zf:
            # 1. unpack the folders into a staging dir next to DATA_DIR (same filesystem → atomic renames)
            for name, e in entries.items():
                if "/" in name:
                    out_path = stage / name
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(out_path, "wb") as out:
                        _copy_checked(zf, name, e, out)
            db_tmp = stage / DB_NAME
            with open(db_tmp, "wb") as out:
                _copy_checked(zf, DB_NAME, entries[DB_NAME], out)
            _db_summary(db_tmp)                     # quick_check before touching the live database
            # 2. live changes from here on
            _restore_db(db_tmp)
            backed = {n for n in entries if "/" not in n and n != DB_NAME}
            for p in secret_files(root):
                if p.name not in backed:
                    p.unlink(missing_ok=True)      # the restored state did not have this login
            for name in sorted(backed):
                _write_secret(root / name, entries[name], zf)
        for tree in (*TREES, *((PROFILE,) if has_profile else ())):
            new = stage / tree
            new.mkdir(parents=True, exist_ok=True)
            cur, old = root / tree, stage / f"old-{tree}"
            if cur.is_symlink():
                cur.unlink()
            elif cur.exists():
                os.replace(cur, old)
            os.replace(new, cur)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        cancel_pending(token)
    log.info("backup restored (created %s, %d files), safety backup %s", m.get("created_at"), len(entries), safety.name)
    return {"safety": safety.name, "manifest": m}


def schedule_restart(delay: float = 2.0) -> None:
    """Let the response go out, then stop cleanly; supervisord (autorestart=true) starts the app again.
    Needs live verification in the container."""
    import asyncio

    asyncio.get_running_loop().call_later(delay, os.kill, os.getpid(), signal.SIGTERM)
