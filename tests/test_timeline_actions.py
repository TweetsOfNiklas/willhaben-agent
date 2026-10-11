"""Timeline: what requests, buttons, jobs, agent runs and the scheduler record, and which things it belongs to."""
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import Conversation, Draft, TimelineEvent, session, utcnow
from app.services import ha_actions, jobs, timeline
from app.services.search_agents import create_agent, run_agent
from app.willhaben import public as public_mod
from app.willhaben.public import PublicClient, RateLimiter
from tests.conftest import finish_jobs, load, wait_jobs

URL = "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad"


@pytest.fixture(autouse=True)
def recording_on():
    """Both are opt-in; these tests switch them on (test_timeline.py checks the default)."""
    timeline.set_recording(timeline=True, willhaben=True)


def _all(kind: str | None = None) -> list[TimelineEvent]:
    with session() as db:
        q = select(TimelineEvent).order_by(TimelineEvent.id)
        if kind:
            q = q.where(TimelineEvent.kind == kind)
        return list(db.exec(q))


@pytest.fixture
def chat():
    with session() as db:
        db.add(Conversation(id="c1", partner_name="Anna", ad_id="9", ad_title="Rad", role="seller", last_message_at=utcnow()))
        db.commit()
        d = Draft(conversation_id="c1", text="Ja, noch da")
        db.add(d)
        db.commit()
        db.refresh(d)
        return d.id


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_post_is_an_action_with_resolved_ids(client, chat):
    r = client.post(f"/drafts/{chat}/discard", follow_redirects=False)
    assert r.status_code == 303
    a = _all("action")[-1]
    assert a.name == "POST /drafts/{draft_id}/discard" and a.actor == "user" and a.ok
    assert a.conversation_id == "c1" and a.ad_id == "9"
    assert a.extra == {"reply_draft": str(chat), "mine": True}
    assert timeline.action_label(a.name) == "Reply draft discarded"
    assert "Reply draft discarded" in client.get("/timeline").text


def test_request_context_reaches_the_job_it_starts(client, chat):
    client.post(f"/drafts/{chat}/send", data={"text": "Ja, noch da"}, follow_redirects=False)
    wait_jobs()
    job = _all("job")[-1]
    assert job.name == "chat-send" and job.conversation_id == "c1" and job.actor == "user"
    assert job.ok is False and job.error          # no browser in tests: sending fails, the reason is kept


def test_redirect_message_and_failures_are_kept(client):
    r = client.post("/settings/web", data={"public_url": "not a url"}, follow_redirects=False)
    assert r.status_code == 303
    a = _all("action")[-1]
    assert a.name == "POST /settings/web" and "http://" in a.payload["message"]
    client.post("/drafts/999/send", data={"text": "x"})
    bad = _all("action")[-1]
    assert bad.ok is False and bad.error == "HTTP 404"


def test_reads_and_skipped_routes_are_not_actions(client):
    client.get("/timeline")
    client.post("/lang", data={"lang": "en"})
    client.post("/inbox/read-all")
    assert _all("action") == []


def test_api_and_mcp_actors(client):
    client.post("/api/agents", json={})
    client.post("/api/agents", json={}, headers={"x-wh-client-kind": "mcp"})
    assert [a.actor for a in _all("action")] == ["api", "mcp"]


def test_path_ids():
    from app.timeline_web import path_ids

    assert path_ids("/myads/{ad_id}/delete", {"ad_id": "5"}) == {"ad_id": "5", "mine": True}
    assert path_ids("/hits/ad/{ad_id}", {"ad_id": "5"}) == {"ad_id": "5"}
    assert path_ids("/new-ad/{draft_id}", {"draft_id": 3}) == {"draft_id": 3}
    assert path_ids("/purchases/{pid}/{action}", {"pid": 4, "action": "pause"}) == {"purchase_id": 4, "action": "pause"}


@pytest.fixture
def responses():
    queue: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = queue.pop(0)
        return httpx.Response(body) if isinstance(body, int) else httpx.Response(200, json=body)

    client = PublicClient(transport=httpx.MockTransport(handler))
    client.limiter = RateLimiter(0)          # own limiter: a cool-down from another test must not leak in
    public_mod.set_public(client)
    yield queue
    public_mod.set_public(None)


async def test_agent_run_entry_with_counts(responses):
    agent = create_agent(URL, name="Räder")
    responses.extend([load("search_fahrrad_rows3.json"), 500])
    await run_agent(agent.id)
    await run_agent(agent.id)
    ok, failed = _all("agent_run")
    assert ok.title == "Räder" and ok.agent_id == agent.id and ok.ok and ok.payload["rows_found"] > 0
    assert ok.payload["baseline"] is True and ok.duration_ms is not None
    assert failed.ok is False and "500" in failed.error


async def test_failed_job_and_marker_ids():
    async def boom(job):
        job.progress(1, "second ad", "ad:77")
        timeline.record("llm", "inside")
        raise RuntimeError("willhaben said no")

    jobs.submit("myads-delete", "Delete ads", boom, current="ad:76")
    await finish_jobs()
    job = _all("job")[0]
    inside = _all("llm")[0]
    assert job.ok is False and job.error == "willhaben said no" and job.ad_id == "76" and job.extra == {"mine": True}
    assert inside.run_id == job.id and inside.ad_id == "77"       # the job moved on to the next ad


async def test_scheduled_chat_sync_is_a_run(monkeypatch):
    from app import scheduler
    from app.services import chats

    async def fake_sync():
        timeline.record("wh", "GET conversations")

    monkeypatch.setattr(chats, "sync", fake_sync)
    with timeline.context(actor="user", ad_id="leftover"):      # a context the scheduler must not inherit
        await scheduler._chat_sync()
    run, step = _all()
    assert run.kind == "sched" and run.name == "chat-sync" and run.actor == "scheduler" and run.ok
    assert step.run_id == run.id and step.actor == "scheduler" and step.ad_id is None


async def test_push_button_is_an_action(chat):
    res = await ha_actions.handle({"action": ha_actions.action_id("DISCARD", chat)})
    assert res == "discarded"
    a = _all("action")[-1]
    assert a.name == "button.discard" and a.actor == "ha" and a.conversation_id == "c1" and a.payload == {"result": "discarded"}
    with timeline.context(actor="telegram"):
        assert await ha_actions.handle({"action": ha_actions.action_id("DISCARD", chat)}) is None   # already done
    assert len(_all("action")) == 1
