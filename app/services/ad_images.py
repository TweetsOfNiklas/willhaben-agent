"""One ad photo, downloaded for a Claude call (screening, price check, product search). Anonymous: no session."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.willhaben import access_log, recorder

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 3 * 1024 * 1024
_EXT = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


async def download(url: str | None, folder: Path, name: str,
                   transport: httpx.AsyncBaseTransport | None = None) -> Path | None:
    """Save the image at `url` as folder/name.<ext>; None unless https, an image and at most MAX_IMAGE_BYTES.
    Downloads from the willhaben CDN count as anonymous willhaben accesses (access_log)."""
    if not (url or "").startswith("https://"):
        return None
    from app import platforms

    site = platforms.for_host(urlsplit(url).hostname)      # a CDN image of one of the platforms: counts as its access
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, transport=transport) as c:
            r = await c.get(url)
        if site:
            access_log.record("anonymous", "other", access_log.outcome_for_status(r.status_code), platform=site.id)
            recorder.record_http("download", "other", "GET", url, status=r.status_code, resp_headers=dict(r.headers),
                                 resp_body=r.content, final_url=str(r.url), platform=site.id,
                                 duration_ms=int((time.monotonic() - started) * 1000))   # images: size + hash only
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/") \
                or len(r.content) > MAX_IMAGE_BYTES:
            return None
        p = folder / f"{name}{_EXT.get(r.headers['content-type'].split(';')[0], '.jpg')}"
        p.write_bytes(r.content)
        return p
    except httpx.HTTPError as e:
        if site:
            access_log.record("anonymous", "other", "error", platform=site.id)
            recorder.record_http("download", "other", "GET", url, outcome="error", error=str(e), platform=site.id)
        log.info("ad image download failed: %s", e)
        return None
