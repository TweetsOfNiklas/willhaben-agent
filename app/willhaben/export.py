"""Turn stored recordings (willhaben and the other sites) into redacted fixture files for tests.

    uv run python -m app.willhaben.export --out tests/fixtures/rec --since 7d --kinds search,detail,chat
        [--channels anonymous,account] [--platforms willhaben,vinted] [--all] [--keep-texts] [--raw]

One JSON file per request, the newest one per canonical request unless --all:
    <out>/<kind>/<METHOD>-<path-slug>-<key8>.json              (willhaben, as the fake willhaben in tests/ expects)
    <out>/<platform>/<kind>/<METHOD>-<path-slug>-<key8>.json   (the other sites)
    {"method", "url", "final_url", "kind", "channel", "status", "ws_status", "content_type", "recorded_at",
     "truncated", "request": {"headers", "body"}, "response": {"headers", "body"}}
Bodies are parsed JSON when they are JSON, else text. Images and emptied (pruned) recordings are skipped. Personal data
is replaced (app/willhaben/redact.py) unless --raw; chat texts become "Nachricht 1", … unless --keep-texts.
Review the files before committing them.

Anonymous modules import this file: it must never import the logged-in session (tests/test_anonymous_public.py).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.willhaben import recorder, redact


def parse_since(value: str | None) -> timedelta | None:
    """"7d", "12h", "30m" -> timedelta; empty -> None (everything)."""
    if not value:
        return None
    m = re.fullmatch(r"(\d+)\s*([dhm])", value.strip().lower())
    if not m:
        raise ValueError(f"--since: use e.g. 7d, 12h or 30m, not {value!r}")
    n = int(m.group(1))
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[m.group(2)]


def _decode(blob: bytes | None, content_type: str | None) -> Any:
    raw = recorder.decode_body(blob)
    if raw is None:
        return None
    text = raw.decode("utf-8", "replace")
    if "json" in (content_type or "") or text.lstrip()[:1] in "{[":
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text


def _clean(value: Any, content_type: str | None, ctx: redact.RedactContext | None) -> Any:
    if ctx is None or value is None:
        return value
    if isinstance(value, (dict, list)):
        return redact.redact_json(value, ctx)
    if "x-component" in (content_type or "") or redact.FLIGHT_LINE_RE.match(value.split("\n", 1)[0]):
        return redact.redact_flight(value, ctx)
    if "html" in (content_type or "") or "__NEXT_DATA__" in value or "data-vue3" in value:
        return redact.redact_html(value, ctx)
    return value


def _slug(url: str) -> str:
    try:
        path = urlsplit(url).path
    except ValueError:
        path = url
    return re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-")[-60:] or "root"


def export_recordings(out: Path, *, since: timedelta | None = None, kinds: set[str] | None = None,
                      channels: set[str] | None = None, all_versions: bool = False, keep_texts: bool = False,
                      raw: bool = False, platforms: set[str] | None = None) -> list[Path]:
    """Write one fixture file per recording (newest per request unless all_versions). Returns the files written."""
    from sqlalchemy import or_
    from sqlmodel import col, select

    from app.db import WhRecording, session, utcnow

    q = select(WhRecording).where(col(WhRecording.resp_body).is_not(None), col(WhRecording.pruned).is_(False),
                                  col(WhRecording.cached).is_(False))
    if since:
        q = q.where(WhRecording.ts >= utcnow() - since)
    if kinds:
        q = q.where(col(WhRecording.kind).in_(sorted(kinds)))
    if channels:
        q = q.where(col(WhRecording.channel).in_(sorted(channels)))
    if platforms:
        site = col(WhRecording.platform).in_(sorted(platforms))
        q = q.where(or_(site, col(WhRecording.platform).is_(None)) if "willhaben" in platforms else site)
    with session() as db:
        rows = list(db.exec(q.order_by(col(WhRecording.id).desc())))
    ctx = None if raw else redact.RedactContext(keep_texts=keep_texts)
    seen: set[tuple[str, str]] = set()
    picked = []
    for r in rows:                      # newest first: the first one of each request wins
        if (r.content_type or "").startswith("image/"):
            continue
        if not all_versions and (r.method, r.canonical_key) in seen:
            continue
        seen.add((r.method, r.canonical_key))
        picked.append(r)
    # pass 1: redact the bodies (oldest first, so placeholders are numbered in the order things happened); this also
    # collects every name, id and text found, which pass 2 then replaces wherever else it appears (URLs, other files)
    entries = []
    for r in sorted(picked, key=lambda r: r.id):
        req_ct = recorder._sniff(recorder.decode_body(r.req_body)) if r.req_body is not None else None
        entries.append((r, {
            "method": r.method, "url": r.url, "final_url": r.final_url, "platform": r.platform or "willhaben",
            "kind": r.kind, "channel": r.channel, "status": r.status, "ws_status": r.ws_status,
            "content_type": r.content_type, "recorded_at": r.ts.isoformat(timespec="seconds"),
            "truncated": r.resp_truncated,
            "request": {"headers": redact.headers_for_fixture(json.loads(r.req_headers) if r.req_headers else None),
                        "body": _clean(_decode(r.req_body, req_ct), req_ct, ctx)},
            "response": {"headers": redact.headers_for_fixture(json.loads(r.resp_headers) if r.resp_headers else None),
                         "body": _clean(_decode(r.resp_body, r.content_type), r.content_type, ctx)},
        }))
    written: list[Path] = []
    for r, entry in entries:            # pass 2
        text = json.dumps(entry, ensure_ascii=False, indent=1)
        if ctx:
            text = redact.redact_text(text, ctx)
            entry = json.loads(text)
        body = entry["request"]["body"]
        key = recorder.canonical_key(r.method, entry["url"], body, upload=_upload_of(r.method, body))
        folder = out if (r.platform or "willhaben") == "willhaben" else out / r.platform
        path = folder / r.kind / f"{r.method}-{_slug(entry['url'])}-{key[:8]}{'-' + str(r.id) if all_versions else ''}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        written.append(path)
    return written


def _upload_of(method: str, body: Any) -> tuple[str, str] | None:
    """Uploads are matched by form field and file type (the recorder stores those instead of the file)."""
    if method == "UPLOAD" and isinstance(body, dict):
        return str(body.get("field")), str(body.get("mime"))
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.willhaben.export", description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="target folder, e.g. tests/fixtures/rec")
    ap.add_argument("--since", default="", help="only recordings of the last 7d / 12h / 30m")
    ap.add_argument("--kinds", default="", help="comma-separated: search,detail,chat,send,myads,publish,upload,…")
    ap.add_argument("--channels", default="", help="comma-separated: anonymous,account,chat,navigation,download,browser")
    ap.add_argument("--platforms", default="", help="comma-separated: willhaben,kleinanzeigen,shpock,vinted,preisjaeger")
    ap.add_argument("--all", action="store_true", help="every recording, not only the newest per request")
    ap.add_argument("--keep-texts", action="store_true", help="keep chat message texts (names etc. are still replaced)")
    ap.add_argument("--raw", action="store_true", help="no redaction at all (never commit these files)")
    a = ap.parse_args(argv)
    from app.db import init_db

    init_db()
    split = lambda v: {x.strip() for x in v.split(",") if x.strip()} or None  # noqa: E731
    files = export_recordings(a.out, since=parse_since(a.since), kinds=split(a.kinds), channels=split(a.channels),
                              all_versions=a.all, keep_texts=a.keep_texts, raw=a.raw, platforms=split(a.platforms))
    print(f"{len(files)} files written to {a.out}" + (" (NOT redacted)" if a.raw else " – review them before committing"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
