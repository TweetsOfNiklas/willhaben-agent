"""One ad photo, downloaded for a Claude call (screening, price check, product search). Anonymous: no session."""
from __future__ import annotations

import logging
import time
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.willhaben import access_log, recorder

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 3 * 1024 * 1024
_EXT = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


def _site(url: str, platform: str | None):
    """The platform an image download counts for: the ad's own site when its CDN fits (Kleinanzeigen and eBay share
    i.ebayimg.com), else whichever site owns the host."""
    from app import platforms

    host = urlsplit(url).hostname
    if platform:
        try:
            p = platforms.get(platform)
        except platforms.UnknownPlatform:
            p = None
        if p is not None and p.matches_host(host):
            return p
    return platforms.for_host(host)


async def download(url: str | None, folder: Path, name: str,
                   transport: httpx.AsyncBaseTransport | None = None, platform: str | None = None) -> Path | None:
    """Save the image at `url` as folder/name.<ext>; None unless https, an image and at most MAX_IMAGE_BYTES.
    Downloads from a platform's CDN count as anonymous accesses of that platform (access_log); `platform` is the
    ad's site. With the site's VPN on, the photo comes through the VPN too; with no working server there is no photo
    (never a direct download)."""
    if not (url or "").startswith("https://"):
        return None
    from app.platforms import PlatformError, vpn

    site = _site(url, platform)                           # a CDN image of one of the platforms: counts as its access
    proxy, held = None, nullcontext()
    if site and (tunnel := vpn.tunnel_for(site.id)) is not None:
        try:
            vroute = await tunnel.ensure()
        except PlatformError as e:
            log.info("ad image not downloaded, no VPN server: %s", e)
            return None
        proxy, held = vroute.proxy, tunnel.hold(vroute)   # a switch-over lets the download finish
    started = time.monotonic()
    try:
        route = {"transport": transport} if transport is not None else {"proxy": proxy}   # tests: the mock, no proxy
        with held:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True, **route) as c:
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
