"""A saved hit keeps its photo locally: the sites drop the photos of sold or deleted ads (Vinted's links expire)."""
import httpx
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import Hit, session
from app.services import ad_images, search_agents


def _hit(**kw):
    with session() as db:
        h = Hit(agent_id=1, platform="vinted", ad_id="9", heading="HomePod", state="saved", url="https://www.vinted.at/items/9",
                image_url="https://images1.vinted.net/t/x.webp?s=1", **kw)
        db.add(h)
        db.commit()
        db.refresh(h)
        return h.id


async def test_a_saved_hit_keeps_its_photo_and_the_card_shows_it(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    real = ad_images.download
    asked = []

    async def download(url, folder, name, transport=None, platform=None):
        asked.append(url)
        ok = httpx.MockTransport(lambda r: httpx.Response(200, content=b"img", headers={"content-type": "image/webp"})
                                 if "fresh" in str(r.url) else httpx.Response(404))
        return await real(url, folder, name, transport=ok, platform=platform)
    monkeypatch.setattr(ad_images, "download", download)
    hid = _hit()
    assert await search_agents.keep_image(hid, "https://images1.vinted.net/t/fresh.webp")
    with session() as db:
        h = db.get(Hit, hid)
    assert h.image_path == f"hit-{hid}.webp" and (tmp_path / "hit-images" / h.image_path).read_bytes() == b"img"
    assert asked == ["https://images1.vinted.net/t/fresh.webp"]
    from app.main import app
    with TestClient(app) as c:
        assert c.get(f"/hits/{hid}/image").content == b"img"
        page = c.get("/hits?state=saved").text
    assert f'src="/hits/{hid}/image"' in page
    assert await search_agents.keep_image(hid) and len(asked) == 1      # kept once


async def test_no_photo_when_both_links_are_dead(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)

    async def download(url, folder, name, transport=None, platform=None):
        return None
    monkeypatch.setattr(ad_images, "download", download)
    hid = _hit()
    assert not await search_agents.keep_image(hid, "https://images1.vinted.net/t/gone.webp")
    from app.main import app
    with TestClient(app) as c:
        assert c.get(f"/hits/{hid}/image").status_code == 404


def test_the_ad_viewer_shows_the_kept_photo_when_the_sold_page_has_none(monkeypatch, tmp_path):
    """Vinted's page of a sold item has no photos any more: the viewer shows the hit's kept one."""
    from app import platforms
    from app.main import app

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    hid = _hit(image_path="hit-x.webp", site_status="sold")
    with session() as db:
        h = db.get(Hit, hid)
    sold = platforms.AdDetail.from_hit(h)
    sold.images, sold.status = [], "sold"

    async def detail(ad_id, url=None):
        return sold
    monkeypatch.setattr(platforms.get("vinted"), "detail", detail)
    monkeypatch.setattr(search_agents, "keep_image_later", lambda *a, **kw: None)
    with TestClient(app) as c:
        frag = c.get(f"/hits/ad/vinted/9?hit={hid}").text
    assert f'src="/hits/{hid}/image"' in frag and "No photos" not in frag


async def test_a_saved_hit_keeps_every_photo_the_viewer_showed(monkeypatch, tmp_path):
    """Saving after the viewer showed the ad keeps all its photos; once sold, the viewer shows them from the disk."""
    from app import platforms
    from app.main import app

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)

    async def download(url, folder, name, transport=None, platform=None):
        p = folder / f"{name}.webp"
        p.write_bytes(url.encode())
        return p
    monkeypatch.setattr(ad_images, "download", download)
    hid = _hit()
    shots = [f"https://images1.vinted.net/t/{i}.webp" for i in range(3)]
    search_agents.remember_photos(hid, shots)
    assert await search_agents.keep_image(hid, search_agents.shown_photos(hid))
    with session() as db:
        h = db.get(Hit, hid)
    assert search_agents.kept_photos(h) == [f"hit-{hid}.webp", f"hit-{hid}-1.webp", f"hit-{hid}-2.webp"]
    sold = platforms.AdDetail.from_hit(h)
    sold.images, sold.status = [], "sold"

    async def detail(ad_id, url=None):
        return sold
    monkeypatch.setattr(platforms.get("vinted"), "detail", detail)
    with TestClient(app) as c:
        frag = c.get(f"/hits/ad/vinted/9?hit={hid}").text
        assert c.get(f"/hits/{hid}/image/2").content == shots[2].encode()
        assert c.get(f"/hits/{hid}/image/5").status_code == 404
    assert f'src="/hits/{hid}/image"' in frag and f'data-full="/hits/{hid}/image/2"' in frag
