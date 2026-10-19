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


async def test_claude_draft_runs_in_the_background(monkeypatch):
    _conv_and_draft()
    gate, calls = asyncio.Event(), []

    async def fake_draft(cid, hint=None, auto=False):
        await gate.wait()
        calls.append((cid, hint))
        with session() as db:
            db.add(Draft(conversation_id=cid, text="Ja, gerne."))
            db.commit()

    monkeypatch.setattr(chats, "draft_reply", fake_draft)
    before = chats.live_state("c1")["composer"]
    assert chats.enqueue_draft("c1", "freundlich")
    assert not chats.enqueue_draft("c1")                                      # one at a time per chat
    with session() as db:
        assert db.get(Conversation, "c1").drafting is True
    assert chats.live_state("c1")["composer"] != before                       # the open chat shows "writing…"
    assert jobs.snapshot()["active"][0]["lane"] == "claude"
    gate.set()
    await finish_jobs()
    with session() as db:
        conv = db.get(Conversation, "c1")
    assert calls == [("c1", "freundlich")] and not conv.drafting and not conv.draft_error


async def test_claude_draft_failure_shows_in_the_chat(monkeypatch):
    from app.llm.base import LLMError
    _conv_and_draft()

    async def boom(cid, hint=None, auto=False):
        raise LLMError("Limit erreicht")

    monkeypatch.setattr(chats, "draft_reply", boom)
    chats.enqueue_draft("c1")
    await finish_jobs()
    with session() as db:
        conv = db.get(Conversation, "c1")
    assert not conv.drafting and "Limit erreicht" in conv.draft_error


async def test_refine_runs_in_the_background(monkeypatch):
    draft_id = _ad_draft()

    async def fake_refine(did, instruction, label=None):
        return ad_creator.update_draft(did, title="Kürzer")

    monkeypatch.setattr(ad_creator, "refine_draft", fake_refine)
    assert ad_creator.enqueue_refine(draft_id, "kürzer")
    assert not ad_creator.enqueue_refine(draft_id, "nochmal")                 # already on its way
    with session() as db:
        assert db.get(dbmod.AdDraft, draft_id).refining is True
    await finish_jobs()
    with session() as db:
        d = db.get(dbmod.AdDraft, draft_id)
    assert d.title == "Kürzer" and not d.refining


async def test_edit_keeps_its_text_until_willhaben_has_it(monkeypatch):
    from app.services import my_ads
    with session() as db:
        db.add(dbmod.MyAd(ad_id="7", heading="Alt", status="active"))
        db.commit()

    async def fail(ad_id, heading, description, price):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(my_ads, "edit", fail)
    assert my_ads.enqueue_edit("7", "Neu", "Text", 12.0)
    with session() as db:
        row = db.get(dbmod.MyAd, "7")
    assert row.pending_action == "edit" and row.pending_payload["edit"]["heading"] == "Neu"
    assert not my_ads.enqueue_edit("7", "Noch", "x", None)                     # never two actions at once
    await finish_jobs()
    with session() as db:
        row = db.get(dbmod.MyAd, "7")
    assert not row.pending_action and "500" in row.last_error and row.pending_payload["unsaved"]["heading"] == "Neu"


def test_restart_keeps_an_interrupted_edit_for_the_edit_page():
    from app.services import my_ads
    with session() as db:
        db.add(dbmod.MyAd(ad_id="8", heading="Alt", status="active", pending_action="edit",
                          pending_payload={"edit": {"heading": "Neu", "description": "Text", "price": None}}))
        db.commit()
    my_ads.recover()
    with session() as db:
        row = db.get(dbmod.MyAd, "8")
    assert not row.pending_action and row.pending_payload == {"unsaved": {"heading": "Neu", "description": "Text", "price": None}}


async def test_photos_wait_as_files_until_the_upload(monkeypatch):
    from app.services import my_ads
    with session() as db:
        db.add(dbmod.MyAd(ad_id="9", heading="Rad", status="active"))
        db.commit()
    got = []

    async def fake_set_photos(ad_id, order, files):
        got.append((order, files))

    monkeypatch.setattr(my_ads, "set_photos", fake_set_photos)
    assert my_ads.enqueue_photos("9", ["new:0", "1/9/a.jpg"], [("neu.png", b"PNG")])
    assert (my_ads._photo_dir("9") / "00.png").read_bytes() == b"PNG"        # survives until the job runs
    await finish_jobs()
    assert got == [(["new:0", "1/9/a.jpg"], [("neu.png", b"PNG")])] and not my_ads._photo_dir("9").exists()


async def test_sold_step_leaves_willhabens_reasons_on_the_ad(monkeypatch):
    from app.services import sold
    from app.willhaben import sales
    with session() as db:
        db.add(dbmod.MyAd(ad_id="5", heading="Rad", status="active"))
        db.commit()
    seen = []

    async def on_willhaben(ad_id, kind, *, undo=False, reason_id=None):
        seen.append(reason_id)
        if reason_id is None:
            raise sales.ReasonRequired([sales.SoldReason(id="2", label="Über willhaben")])
        return "Marked as sold on willhaben"

    monkeypatch.setattr(sold, "on_willhaben", on_willhaben)
    assert sold.enqueue_willhaben("5", "sold")
    await finish_jobs()
    assert [r.label for r in sold.pending_reasons("5")] == ["Über willhaben"]
    assert sold.enqueue_willhaben("5", "sold", reason_id="2")
    await finish_jobs()
    assert seen == [None, "2"] and sold.pending_reasons("5") is None


def test_chat_sync_and_reload_answer_htmx_at_once(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    _conv_and_draft()
    ran = []

    async def fake_sync(force=False):
        ran.append(("sync", force))
        return {"conversations": 1, "changed": 0}

    async def fake_messages(cid):
        ran.append(("reload", cid))
        return "full"

    async def fake_context(cid, force=False):
        ran.append(("context", cid, force))
        return None

    monkeypatch.setattr(chats, "sync", fake_sync)
    monkeypatch.setattr(chats, "sync_messages", fake_messages)
    monkeypatch.setattr(chats, "ensure_ad_context", fake_context)
    from tests.conftest import wait_jobs
    with TestClient(app) as c:
        assert c.post("/chats/sync", headers={"HX-Request": "true"}).status_code == 204
        assert c.post("/chats/c1/refresh", headers={"HX-Request": "true"}).status_code == 204
        r = c.post("/chats/sync", follow_redirects=False)                     # without htmx: back to the list
        assert r.status_code == 303 and r.headers["location"].startswith("/chats?msg=")
        wait_jobs()
    assert ("reload", "c1") in ran and ("context", "c1", True) in ran and ("sync", True) in ran   # Reload: fresh ad data


async def test_other_actions_keep_an_unsaved_edit_text(monkeypatch):
    from app.services import my_ads, sold

    with session() as db:
        db.add(dbmod.MyAd(ad_id="6", heading="Rad", status="active", pending_payload={"unsaved": {"heading": "Neu"}}))
        db.commit()

    async def fake_set_photos(ad_id, order, files):
        return None

    async def fake_on_willhaben(ad_id, kind, *, undo=False, reason_id=None):
        return "ok"

    monkeypatch.setattr(my_ads, "set_photos", fake_set_photos)
    monkeypatch.setattr(sold, "on_willhaben", fake_on_willhaben)
    assert my_ads.enqueue_photos("6", ["1/6/a.jpg"], [])
    await finish_jobs()
    assert sold.enqueue_willhaben("6", "reserved")
    await finish_jobs()
    with session() as db:
        assert db.get(dbmod.MyAd, "6").pending_payload == {"unsaved": {"heading": "Neu"}}


# ------------------------------------------------------------------ review fixes (PR #9)
async def test_a_hung_job_times_out_and_frees_its_lane(monkeypatch):
    monkeypatch.setattr(jobs, "JOB_TIMEOUT_S", 0.05)

    async def hang(job):
        await asyncio.sleep(3600)

    async def quick(job):
        return "ok"

    stuck = jobs.submit("x", "stuck", hang)
    after = jobs.submit("x", "after", quick)
    await finish_jobs()
    assert stuck.status == "failed" and "stopped" in stuck.result and after.status == "done"


async def test_save_and_publish_wait_while_claude_revises(monkeypatch):
    draft_id = _ad_draft()
    gate = asyncio.Event()

    async def slow_refine(did, instruction, label=None):
        await gate.wait()
        return ad_creator.update_draft(did, title="Claude's title")

    monkeypatch.setattr(ad_creator, "refine_draft", slow_refine)
    assert ad_creator.enqueue_refine(draft_id, "kürzer")
    await asyncio.sleep(0)
    d = ad_creator.save_fields(draft_id, "My own title", "Mein Text", "20")
    assert d.title != "My own title" and "still revising" in d.error            # refused, not silently overwritten
    assert not ad_creator.enqueue_publish(draft_id, 0, {})                     # no upload of the text being replaced
    gate.set()
    await finish_jobs()
    assert ad_creator.save_fields(draft_id, "My own title", "Mein Text", "20").title == "My own title"


def test_a_published_draft_is_not_revised():
    draft_id = _ad_draft()
    ad_creator.update_draft(draft_id, status="published")
    assert not ad_creator.enqueue_refine(draft_id, "kürzer")


async def test_a_used_up_budget_pauses_a_job_instead_of_failing_it(monkeypatch):
    """The hourly limit is full: the job waits (the header says until when), its timeout does not run meanwhile, and
    it goes on by itself once the budget has room again."""
    from app.platforms import ratelimit
    from app.platforms.ratelimit import RateLimiter

    monkeypatch.setattr(ratelimit, "WAKE_S", 0.01)
    monkeypatch.setattr(ratelimit.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(jobs, "JOB_TIMEOUT_S", 0.3)
    lim = RateLimiter(0, per_hour=1, max_wait_s=0.1, name="willhaben")
    async with lim:
        pass

    async def send(job):
        job.progress(0, "Answer to Anna")
        async with lim:
            return "sent"

    job = jobs.submit("chat-send", "answer", send, lane="chat")
    for _ in range(200):
        if "hourly request limit reached" in job.step:
            break
        await asyncio.sleep(0.005)
    assert job.status == "running" and job.step.startswith("willhaben: hourly request limit reached – goes on at ")
    await asyncio.sleep(0.5)                  # longer than JOB_TIMEOUT_S: waiting for the budget does not count
    assert job.status == "running"
    lim._recent[0] -= 3600                    # the hour has passed
    await finish_jobs()
    assert job.status == "done" and job.result == "sent"


async def test_a_tracked_task_shows_its_budget_wait_in_the_header():
    import time

    from app.platforms import ratelimit

    async with jobs.track("ad-batch", "batch") as job:
        job.step = "Claude writes"
        bw = jobs.budget_wait(job)
        bw.sites["willhaben"] = time.monotonic() + 600
        bw.on_change(bw)
        assert job.step.startswith("willhaben: hourly request limit reached – goes on at ")
        bw.sites.clear()
        bw.on_change(bw)
        assert job.step == "Claude writes"
        assert isinstance(bw, ratelimit.BudgetWait)
