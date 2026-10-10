"""Background jobs: lanes, queued chat answers (state in the database) and queued publishing."""
import asyncio

from app import db as dbmod
from app.db import Conversation, Draft, session
from app.services import ad_creator, chats, jobs
from tests.conftest import finish_jobs


def _conv_and_draft(text="Ja, noch da."):
    with session() as db:
        db.add(Conversation(id="c1", partner_name="Erika", role="seller"))
        d = Draft(conversation_id="c1", text=text)
        db.add(d)
        db.commit()
        db.refresh(d)
    return d.id


async def test_lanes_run_side_by_side_and_in_order_within():
    gate, order = asyncio.Event(), []

    async def slow(job):
        await gate.wait()
        order.append("ads-1")

    async def quick(job):
        order.append(job.label)

    a1 = jobs.submit("x", "ads-1", slow)
    a2 = jobs.submit("x", "ads-2", quick)
    c1 = jobs.submit("y", "chat-1", quick, lane="chat")
    await c1.task
    assert order == ["chat-1"] and a2.status == "queued"      # the chat lane does not wait behind the ads lane
    gate.set()
    await finish_jobs()
    assert order == ["chat-1", "ads-1", "ads-2"] and a1.status == a2.status == "done"


async def test_queued_answer_is_marked_and_sent(monkeypatch):
    draft_id = _conv_and_draft()
    sent = []

    async def fake_send(did, text):
        sent.append((did, text))
        return chats.mark_sent(did, text)

    monkeypatch.setattr(chats, "send_draft", fake_send)
    assert chats.enqueue_send(draft_id, "  Ja, noch da!  ")
    with session() as db:
        assert db.get(Draft, draft_id).status == "sending"     # in the database right away
    assert not chats.enqueue_send(draft_id, "nochmal")          # not open any more: never twice
    job = jobs.snapshot()["active"][0]
    assert job["lane"] == "chat" and job["label"] == "Answer to Erika" and job["current"] == f"draft:{draft_id}"
    await finish_jobs()
    assert sent == [(draft_id, "Ja, noch da!")]
    with session() as db:
        assert db.get(Draft, draft_id).status == "sent"


async def test_failed_answer_goes_back_to_an_open_draft(monkeypatch):
    draft_id = _conv_and_draft()

    async def boom(did, text):
        raise RuntimeError("willhaben antwortet 500")

    monkeypatch.setattr(chats, "send_draft", boom)
    chats.enqueue_send(draft_id, "Text bleibt")
    await finish_jobs()
    with session() as db:
        d = db.get(Draft, draft_id)
    assert (d.status, d.text) == ("draft", "Text bleibt") and "500" in d.error
    assert jobs.snapshot()["recent"][0]["status"] == "failed"


def test_restart_never_resends_silently():
    draft_id = _conv_and_draft()
    with session() as db:
        d = db.get(Draft, draft_id)
        d.status = "sending"
        db.add(d)
        db.commit()
    chats.recover_sending()
    with session() as db:
        d = db.get(Draft, draft_id)
    assert d.status == "draft" and "reload the history" in d.error


def _ad_draft(**kw):
    plan = {"options": [{"path": [{"label": "X", "code": "X"}], "label": "X",
                         "fields": [{"code": "Zustand", "label": "Zustand", "values": [["neu", "Neu"]]}]}]}
    with session() as db:
        d = dbmod.AdDraft(input_text="x", title="Lampe", description="Schön.", wh_plan=plan, **kw)
        db.add(d)
        db.commit()
        db.refresh(d)
    return d.id


async def test_publish_input_errors_show_at_once_and_queue_nothing():
    draft_id = _ad_draft()
    assert not ad_creator.enqueue_publish(draft_id, 0, {})                     # required field missing
    with session() as db:
        d = db.get(dbmod.AdDraft, draft_id)
    assert "Zustand" in d.error and not d.publishing and not jobs.busy()


async def test_publish_runs_as_job_with_progress(monkeypatch):
    draft_id = _ad_draft()
    seen = []

    async def fake_publish(did, option, values, stop_after=None, shipping=None, job=None):
        job.progress(1, "Lade Foto 1/1 hoch", f"addraft:{did}")
        seen.append((jobs.working(), values))
        return ad_creator.update_draft(did, status="published", wh_ad_id="1000000009")

    monkeypatch.setattr(ad_creator, "publish_willhaben", fake_publish)
    assert ad_creator.enqueue_publish(draft_id, 0, {"Zustand": "neu"})
    assert not ad_creator.enqueue_publish(draft_id, 0, {"Zustand": "neu"})     # already on its way
    with session() as db:
        assert db.get(dbmod.AdDraft, draft_id).publishing is True
    await finish_jobs()
    assert seen == [({f"addraft:{draft_id}"}, {"Zustand": "neu"})]
    with session() as db:
        d = db.get(dbmod.AdDraft, draft_id)
    assert d.status == "published" and not d.publishing
    assert jobs.snapshot()["recent"][0]["result"] == "Published on willhaben"


def test_publish_interrupted_by_restart():
    draft_id = _ad_draft(publishing=True)
    ad_creator.recover_publishing()
    with session() as db:
        d = db.get(dbmod.AdDraft, draft_id)
    assert not d.publishing and "restart" in d.error


def test_jobs_endpoint_lists_every_waiting_element():
    from fastapi.testclient import TestClient

    from app.main import app
    draft_id = _conv_and_draft()
    ad_draft_id = _ad_draft()
    with TestClient(app) as c:      # startup clears stale markers, so set them afterwards
        with session() as db:
            d = db.get(Draft, draft_id)
            d.status = "sending"
            db.add(d)
            db.add(dbmod.MyAd(ad_id="5", pending_action="delete"))
            ad = db.get(dbmod.AdDraft, ad_draft_id)
            ad.publishing = True
            db.add(ad)
            db.commit()
        assert sorted(c.get("/jobs").json()["pending"]) == sorted(["ad:5", f"draft:{draft_id}", f"addraft:{ad_draft_id}"])
        page = c.get("/chats/c1").text
        assert f'data-pending-key="draft:{draft_id}"' in page and "Sending…" in page
        assert f'/drafts/{draft_id}/send"' not in page                           # nothing to send twice


def test_api_never_sends_a_queued_answer_twice():
    from fastapi.testclient import TestClient

    from app.main import app
    draft_id = _conv_and_draft()
    with TestClient(app) as c:
        with session() as db:
            d = db.get(Draft, draft_id)
            d.status = "sending"
            db.add(d)
            db.commit()
        r = c.post(f"/api/drafts/{draft_id}/send", json={"text": "nochmal"})
    assert r.status_code == 409 and r.json()["sent"] is False
