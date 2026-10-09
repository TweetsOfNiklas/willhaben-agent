"""One ad photo, downloaded for a Claude call (screening, price check, product search). Anonymous: no session."""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.willhaben import access_log

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 3 * 1024 * 1024
_EXT = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


async def download(url: str | None, folder: Path, name: str,
                   transport: httpx.AsyncBaseTransport | None = None) -> Path | None:
    """Save the image at `url` as folder/name.<ext>; None unless https, an image and at most MAX_IMAGE_BYTES.
    Downloads from the willhaben CDN count as anonymous willhaben accesses (access_log)."""
    if not (url or "").startswith("https://"):
        return None
    willhaben = (urlsplit(url).hostname or "").endswith("willhaben.at")   # CDN image: a willhaben access
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, transport=transport) as c:
            r = await c.get(url)
        if willhaben:
            access_log.record("anonymous", "other", access_log.outcome_for_status(r.status_code))
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/") \
                or len(r.content) > MAX_IMAGE_BYTES:
            return None
        p = folder / f"{name}{_EXT.get(r.headers['content-type'].split(';')[0], '.jpg')}"
        p.write_bytes(r.content)
        return p
    except httpx.HTTPError as e:
        if willhaben:
            access_log.record("anonymous", "other", "error")
        log.info("ad image download failed: %s", e)
        return None
