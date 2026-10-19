"""Problems found reviewing pull requests #17-#22: each test fails without its fix."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

from app import db as dbmod
from app.services import ad_batch, ad_creator, jobs
from app.willhaben import insert
from tests.conftest import wait_jobs  # noqa: F401
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)
from tests.test_publish_page import FORM, _batch_of, _draft, _get, wh  # noqa: F401  (wh: pytest fixture)

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def _plan(*codes, selected=0):
    labels = {"HEADPHONES": "Kopfhörer", "MOBILE_SPEAKER": "Mobile Lautsprecher", "DECO": "Lautsprecherboxen Deko"}
    return {"options": [{"path": [{"label": labels[c], "code": c}], "label": labels[c], "fields": []} for c in codes],
            "selected": selected, "location": "1150 Wien", "contact": "Max"}


# ---- 1. #21: a full refresh of the batch page ticked every draft again (unticked ones were published along)
def test_the_batch_page_keeps_rows_unticked(client):  # noqa: F811
    a, b = _draft(status="draft"), _draft(status="draft")
    bid = _batch_of(a, b)
    page = client.get(f"/new-ad/batch/{bid}?unticked={b.id}").text

    def ticked(i):
        return bool(re.search(rf'name="sel" value="{i}" form="bt" checked', page))

    assert ticked(a.id) and not ticked(b.id)
    assert f'data-refresh="/new-ad/batch/{bid}' in page          # the page's own refresh carries the ticks along


# ---- 2. #21: a category that could not be added (willhaben not reachable) left the draft publishable
async def test_a_failed_category_add_keeps_the_draft_from_being_published(wh, monkeypatch):  # noqa: F811
    d = _draft(status="draft", wh_plan=_plan("HEADPHONES"))
    bid = _batch_of(d)

    async def unreachable(draft_id, code):
        raise RuntimeError("willhaben down")

    monkeypatch.setattr(ad_creator, "add_manual_category", unreachable)
    _redraw, unsaved = await ad_batch.save_table(bid, {d.id: {"cat": "DECO"}})
    assert d.id in unsaved
    queued = []
    monkeypatch.setattr(ad_creator, "enqueue_publish", lambda *a, **k: queued.append(a) or True)
    ad_batch.run_action(bid, "publish", [d.id], unsaved=unsaved)
    assert queued == []                                           # not published in its old category


# ---- 3. #21: the category went to the publish job as a list position; a category check meanwhile reorders the list
def test_publish_goes_by_the_category_code(wh, monkeypatch):  # noqa: F811
    d = _draft(status="draft", wh_plan=_plan("HEADPHONES", "MOBILE_SPEAKER", selected=1))
    bid = _batch_of(d)
    calls = []
    monkeypatch.setattr(ad_creator, "enqueue_publish", lambda *a, **k: calls.append((a, k)) or True)
    ad_batch.run_action(bid, "publish", [d.id])
    assert calls and calls[0][1].get("code") == "MOBILE_SPEAKER"
    ad_creator.update_draft(d.id, wh_plan=_plan("MOBILE_SPEAKER", "DECO", "HEADPHONES"))   # a check reordered it
    chosen, *_ = ad_creator._publish_inputs(_get(d.id), 1, {}, None, code="MOBILE_SPEAKER")
    assert chosen["path"][-1]["code"] == "MOBILE_SPEAKER"
    ad_creator.update_draft(d.id, wh_plan=_plan("DECO"))                                    # gone from the list
    import pytest
    with pytest.raises(insert.InsertError):
        ad_creator._publish_inputs(_get(d.id), 0, {}, None, code="MOBILE_SPEAKER")


# ---- 4. #19: an automatic answer waited up to an hour for the hourly budget and then went out unchecked
async def test_an_automatic_answer_does_not_wait_for_the_hourly_budget(monkeypatch):
    from app.platforms import ratelimit
    from app.services import chats
    seen = []

    async def send(draft_id, text):
        seen.append(ratelimit.waits())
        raise chats.SendFailed("budget")

    monkeypatch.setattr(chats, "send_draft", send)
    with dbmod.session() as db:
        d = dbmod.Draft(conversation_id="c1", text="Ja, ist noch da.", status="draft")
        db.add(d)
        db.commit()
        db.refresh(d)
    conv = SimpleNamespace(partner_name="Max", ad_id="1")
    await chats._send_auto(conv, d, "c1", "Titel")
    assert seen == [False]                       # fails fast; the next sync checks again before it tries anew


# ---- 5. #19: an irreversible job (send, publish, buy) waited for the budget while running, beyond any take-back
async def test_irreversible_jobs_do_not_wait_for_the_hourly_budget(monkeypatch):
    from app.platforms import ratelimit
    monkeypatch.setattr(jobs, "grace_s", lambda: 0.0)
    seen = {}

    async def work(job):
        seen[job.label] = ratelimit.waits()

    a = jobs.submit("x", "send", work, grace=True)
    b = jobs.submit("y", "refresh", work, lane="claude")
    await asyncio.gather(a.task, b.task)
    assert seen == {"send": False, "refresh": True}


# ---- 6. #22: "Undo" in the last moment: the job had started, the notice vanished as if it had been taken back
def test_undo_says_when_it_came_too_late():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    cancel = re.search(r"async cancel\(id\) \{(.*?)\n    \},", base, re.S).group(1)
    assert ".json()" in cancel and "ok" in cancel and "tooLate" in cancel      # the answer is read and told
    assert "grace_s * 1000 + 1500" not in base                               # no Undo after the period ended


# ---- 7. #17: the style examples filled up with my newest messages without the contact-data filter
def test_style_examples_never_carry_contact_data():
    from app.services import style
    from app.services.chats import _style_examples
    now = dbmod.utcnow()
    with dbmod.session() as db:
        for i, text in enumerate(["Abholung Hauptstraße 5, 1100 Wien möglich", "ruf mich an 0664 1234567",
                                  "passt, bis morgen dann", "gerne, schick ich dir"]):
            db.add(dbmod.Message(conversation_id=f"c{i}", ext_id=f"m{i}", direction="out", text=text,
                                 created_at=now - __import__("datetime").timedelta(minutes=i)))
        db.commit()
    got = _style_examples(8)
    assert got == ["passt, bis morgen dann", "gerne, schick ich dir"]
    assert not any(style.has_contact_data(e) for e in style.examples())


# ---- 8. #17: what Claude itself wrote to other buyers came back as "facts about the item" for the next one
def test_others_said_leaves_out_claudes_own_answers():
    from app.services.chats import _others_said
    now = dbmod.utcnow()
    with dbmod.session() as db:
        for cid in ("c1", "c2"):
            db.add(dbmod.Conversation(id=cid, ad_id="111", role="seller", ad_title="Karte"))
        db.add(dbmod.Message(conversation_id="c2", ext_id="m1", direction="out", text="ja funktioniert einwandfrei",
                             created_at=now))
        db.add(dbmod.Message(conversation_id="c2", ext_id="m2", direction="out", text="Akku hält sicher 10 Stunden",
                             created_at=now))
        db.add(dbmod.Draft(conversation_id="c2", text="Akku hält sicher 10 Stunden", status="sent"))
        db.commit()
    assert _others_said("c1", "111") == ["ja funktioniert einwandfrei"]


# ---- 9. #22: "Clear" in the hits bulk bar lost its confirmation (hx-confirm on a button of a boosted form)
def test_clearing_hits_asks_first():
    hits = (TEMPLATES / "hits.html").read_text(encoding="utf-8")
    button = re.search(r'<button name="action" value="clear"[^>]*>', hits).group(0)
    assert "onclick=" in button and "confirm(" in button


# ---- 10. #18: an ambiguous category hint picked the first matching leaf
def test_an_ambiguous_category_hint_picks_nothing():
    tree = {"categoryNode": {"label": "Marktplatz", "code": "ROOT", "children": [
        {"label": "Audio", "code": "AUDIO", "children": [{"label": "Zubehör", "code": "AUDIO_ACC"}]},
        {"label": "Foto", "code": "FOTO", "children": [{"label": "Zubehör", "code": "FOTO_ACC"}]},
    ]}, "attributes": []}
    kw = {"condition": None, "attributes": None}
    assert insert.hint_option({"categoryTree": tree}, "Zubehör", **kw) is None
    assert insert.hint_option({"categoryTree": tree}, "Foto > Zubehör", **kw)["path"][-1]["code"] == "FOTO_ACC"
    assert insert.hint_option(FORM, "Audio > Mobile Lautsprecher", **kw)["path"][-1]["code"] == "MOBILE_SPEAKER"
