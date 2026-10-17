"""Background work in the jobs indicator (tracked Claude calls, ad batches, the product search) and the live part of
GET /jobs: sidebar badges and new inbox events as notices on an open page."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlmodel import func, select

from app.db import EventLog, Hit, ProductSearch, SearchAgent, log_event, recent_events, session, utcnow
from app.services import ad_batch, jobs, notify, product_search


async def test_track_shows_work_while_it_runs():
    async with jobs.track("geizhals-claude", "Claude sucht", link="/deals/ad/1", quiet=True) as job:
        running = jobs.snapshot()["active"]
        assert [j["label"] for j in running] == ["Claude sucht"] and running[0]["status"] == "running"
        job.result = "Bosch GSR 18V-55"
    done = jobs.snapshot()
    assert done["active"] == [] and done["recent"][0]["status"] == "done"
    assert done["recent"][0]["result"] == "Bosch GSR 18V-55" and done["recent"][0]["link"] == "/deals/ad/1"
    assert done["recent"][0]["quiet"] is True

    with pytest.raises(RuntimeError):
        async with jobs.track("deal-claude", "Claude bewertet"):
            raise RuntimeError("rate limited")
    assert jobs.snapshot()["recent"][0]["status"] == "failed" and jobs.snapshot()["recent"][0]["result"] == "rate limited"


def test_sources_join_the_snapshot_and_a_broken_one_is_skipped(monkeypatch):
    monkeypatch.setattr(jobs, "_sources", [])
    jobs.add_source(lambda: {"active": [{"id": "x-1", "label": "läuft", "status": "running"}],
                             "recent": [{"id": "x-2", "label": "fertig", "status": "done", "finished_at": "2026-10-13T10:00:00+00:00"}]})

    def broken():
        raise ValueError("db gone")
    jobs.add_source(broken)
    snap = jobs.snapshot()
    assert [j["id"] for j in snap["active"]] == ["x-1"] and [j["id"] for j in snap["recent"]] == ["x-2"]


async def test_ad_batch_shows_as_a_job_with_its_result():
    b = ad_batch.new_batch("Abholung in Wien")

    async def sorted_into_groups():
        await asyncio.sleep(0)
        ad_batch._update(b.id, status="review", groups=[{"id": "g1", "photos": [0]}, {"id": "g2", "photos": [1]}])

    ad_batch.spawn(sorted_into_groups(), batch_id=b.id, label="Claude sortiert 2 Fotos")
    await asyncio.sleep(0)
    assert jobs.snapshot()["active"][0]["label"] == "Claude sortiert 2 Fotos"
    await asyncio.gather(*ad_batch._tasks)
    done = jobs.snapshot()["recent"][0]
    assert done["status"] == "done" and done["result"] == "2 articles found – check the groups"
    assert done["link"] == f"/new-ad/batch/{b.id}" and done["quiet"] is False

    async def wrote_one_draft():
        ad_batch._update(b.id, groups=[{"id": "g1", "photos": [0], "draft_id": 7}, {"id": "g2", "photos": [1], "error": "x"}])

    ad_batch.spawn(wrote_one_draft(), batch_id=b.id, label="Claude schreibt 2 Entwürfe")
    await asyncio.gather(*ad_batch._tasks)
    done = jobs.snapshot()["recent"][0]
    assert done["result"] == "1 drafts written" and done["problems"] == 1

    async def failed():
        ad_batch._update(b.id, status="error", error="Sorting failed: timeout")

    ad_batch.spawn(failed(), batch_id=b.id, label="Claude sortiert")
    await asyncio.gather(*ad_batch._tasks)
    assert jobs.snapshot()["recent"][0]["status"] == "failed"


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_product_search_in_the_indicator(client, monkeypatch):
    with session() as db:
        running = ProductSearch(description="Ubiquiti Switch mit PoE für das Rack", status="searching",
                                progress={"done": 2, "total": 5}, started_at=utcnow())
        done = ProductSearch(description="Raspberry Pi 5", status="done", results=[{"x": 1}, {"x": 2}],
                             started_at=utcnow(), finished_at=utcnow())
        db.add(running)
        db.add(done)
        db.commit()
        db.refresh(running)
        db.refresh(done)
    monkeypatch.setattr(product_search, "_active", {running.id})
    data = client.get("/jobs").json()
    ps = [j for j in data["active"] if j["id"] == f"ps-{running.id}"]
    assert ps and ps[0]["step"] == "Searching the sites" and (ps[0]["done"], ps[0]["total"]) == (2, 5)
    assert ps[0]["label"] == "Product search: „Ubiquiti Switch mit PoE für das Rack“"
    fin = [j for j in data["recent"] if j["id"] == f"ps-{done.id}-done"]
    assert fin and fin[0]["result"] == "2 offers" and fin[0]["link"] == f"/deals?search={done.id}#ps-result"


def test_jobs_bring_badges_and_new_events_but_not_quiet_ones(client):
    first = client.get("/jobs").json()
    assert first["events"] == [] and "counts" in first
    cursor = first["last_event"]

    with session() as db:
        db.add(SearchAgent(name="x", source_url="https://www.willhaben.at/iad/x", webapi_url="https://www.willhaben.at/webapi/x"))
        db.commit()
        db.add(Hit(agent_id=1, ad_id="1", state="new", heading="t", url="https://www.willhaben.at/iad/object?adId=1"))
        db.commit()
    log_event("message", "Neue Nachricht: Ist das Rad noch da?", conversation_id="c1")
    asyncio.run(notify.notify("hit", "Unifi", "held back by the screening", push=False))   # no notice for it
    asyncio.run(notify.notify("hit", "Unifi", "USW-24-POE · € 150", url="https://www.willhaben.at/iad/object?adId=1",
                              data={"ad_id": "1"}))

    data = client.get(f"/jobs?since={cursor}").json()
    assert [e["text"] for e in data["events"]] == ["Neue Nachricht: Ist das Rad noch da?", "Unifi: USW-24-POE · € 150"]
    assert data["events"][0]["title"] == "New message" and data["events"][0]["link"]["href"].startswith("/chats")
    assert data["events"][1]["link"] == {"href": "/hits?view=1", "ad": "1", "external": False}
    with session() as db:
        newest = db.exec(select(func.max(EventLog.id))).one()
        total = db.exec(select(func.count()).select_from(EventLog)).one()
    assert data["counts"]["hits"] == 1 and data["counts"]["inbox"] == total - 1        # the held-back hit: no badge
    assert data["last_event"] == newest
    assert client.get(f"/jobs?since={newest}").json()["events"] == []      # nothing new since
    assert "held back by the screening" not in " ".join(e.message for e in recent_events())   # inbox, Home


def test_page_carries_the_cursor_badge_keys_and_the_inbox_fragment(client):
    log_event("hit", "Neuer Treffer: Rad", ad_id="5")
    page = client.get("/myads").text
    assert '"last_event": ' in page and 'data-nav-count="hits"' in page and 'data-nav-count="inbox"' in page
    assert 'id="inbox-box" data-refresh="/inbox/items"' in page
    frag = client.get("/inbox/items").text
    assert frag.lstrip().startswith("<div id=\"inbox-box\"") and "Neuer Treffer: Rad" in frag and "<html" not in frag


def test_clearing_the_inbox_empties_the_popover(client):
    from app.db import log_event

    log_event("hit", "Neuer Treffer: Rad", "https://example.invalid/ad/1")
    assert "Neuer Treffer: Rad" in client.get("/inbox/items").text
    client.post("/inbox/read-all")
    frag = client.get("/inbox/items").text
    assert "Neuer Treffer: Rad" not in frag and "Nothing new" in frag
    assert "Neuer Treffer: Rad" in client.get("/").text.split('id="activity"')[1]     # Home's activity keeps it
    log_event("hit", "Neuer Treffer: Lampe", "https://example.invalid/ad/2")
    assert "Neuer Treffer: Lampe" in client.get("/inbox/items").text
