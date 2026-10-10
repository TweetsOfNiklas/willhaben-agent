import time

from sqlmodel import select

from app import db as dbmod
from app.llm.base import LLMResult
from app.services import ad_batch
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)
from tests.conftest import wait_jobs  # noqa: E402

SORTED = {
    "groups": [
        {"photos": [2, 1], "product": "Bosch GSR 18V-55", "product_query": "Bosch GSR 18V-55",
         "observations": "Akkuschrauber mit Koffer, Kratzer am Griff", "confidence": "hoch", "question": ""},
        {"photos": [3], "product": "Holzstuhl braun", "product_query": "", "observations": "",
         "confidence": "niedrig", "question": "Gehört Foto 4 auch dazu?"},
        {"photos": [3, 99], "product": "Doppelt", "product_query": "", "observations": "",
         "confidence": "mittel", "question": ""},          # photo 3 is taken, 99 does not exist -> dropped
    ],
    "unassigned": [4, 5],
    "note": "Foto 5 ist unscharf.",
}


def _wait(batch_id, *states, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        b = ad_batch.get(batch_id)
        if b.status in states:
            return b
        time.sleep(0.02)
    raise AssertionError(f"batch stuck in {ad_batch.get(batch_id).status}")


def _sorting_backend(client, monkeypatch, result=SORTED):  # noqa: F811
    fake = client.fake
    orig = fake.complete

    async def complete(**kw):
        if "groups" in kw["schema"]["properties"]:
            fake.calls.append({**kw, "images": kw.get("images") or []})
            return LLMResult(data=result, model="fake-opus")
        return await orig(**kw)

    monkeypatch.setattr(fake, "complete", complete)
    monkeypatch.setattr("app.services.ad_batch.get_backend", lambda *a: fake)
    return fake


def test_batch_sort_review_and_create(client, monkeypatch):  # noqa: F811
    fake = _sorting_backend(client, monkeypatch)
    photos = [("photos", (f"IMG_{i}.jpg", b"jpeg-bytes-%d" % i, "image/jpeg")) for i in range(5)]
    photos.append(("photos", ("notes.txt", b"x", "text/plain")))          # skipped
    r = client.post("/new-ad/batch", data={"notes": "Abholung Wien 15"}, files=photos, follow_redirects=False)
    assert r.status_code == 303 and "/new-ad/batch/" in r.headers["location"] and "skipped" in r.headers["location"]
    batch_id = int(r.headers["location"].split("/")[-1].split("?")[0])

    b = _wait(batch_id, "review")
    assert len(b.image_paths) == 5 and len(fake.calls[-1]["images"]) == 5
    assert "Abholung Wien 15" in fake.calls[-1]["prompt"]
    assert [g["photos"] for g in b.groups] == [[1, 0], [2]]              # 1-based -> index, duplicate dropped
    assert ad_batch.unassigned(b) == [3, 4] and b.note == "Foto 5 ist unscharf."
    page = client.get(f"/new-ad/batch/{batch_id}").text
    assert "Bosch GSR 18V-55" in page and "Gehört Foto 4 auch dazu?" in page and "do not use" in page

    # user: photo 0 becomes the cover, photo 3 is a new article, chair is skipped, comment on the drill
    form = [("img_0", "g1"), ("img_1", "g1"), ("product_g1", "Bosch GSR 18V-55 solo"), ("comment_g1", "Akku fehlt"),
            ("include_g1", "1"), ("img_2", "g2"), ("product_g2", "Holzstuhl"), ("img_3", "new"), ("img_4", ""),
            ("action", "save")]
    client.post(f"/new-ad/batch/{batch_id}", data=dict(form))
    b = ad_batch.get(batch_id)
    assert [(g["id"], g["photos"], g["include"]) for g in b.groups] == [("g1", [0, 1], True), ("g2", [2], False),
                                                                       ("g3", [3], True)]
    assert b.groups[0]["comment"] == "Akku fehlt" and b.groups[0]["product"] == "Bosch GSR 18V-55 solo"

    # re-sort: Claude sees the corrections and the hint; the comment survives
    client.post(f"/new-ad/batch/{batch_id}", data=dict([*form[:-1], ("include_g3", "1"), ("hint", "Foto 4 ist ein Kabel"),
                                                        ("action", "resort")]))
    b = _wait(batch_id, "review")
    prompt = fake.calls[-1]["prompt"]
    assert "Kommentar der Person: Akku fehlt" in prompt and "Foto 4 ist ein Kabel" in prompt
    assert b.groups[0]["comment"] == "Akku fehlt" and b.groups[1]["include"] is False  # carried by photo overlap

    client.post(f"/new-ad/batch/{batch_id}", data=dict([*form[:-1], ("include_g3", "1"), ("action", "create")]))
    b = _wait(batch_id, "review")
    with dbmod.session() as db:
        drafts = list(db.exec(select(dbmod.AdDraft).order_by(dbmod.AdDraft.id)))
    assert len(drafts) == 2 and all(d.batch_id == batch_id for d in drafts)
    assert "Akku fehlt" in drafts[0].input_text and "Artikel: Bosch GSR 18V-55 solo" in drafts[0].input_text
    assert "Gilt für alle meine Artikel: Abholung Wien 15" in drafts[0].input_text
    assert len(drafts[0].image_paths) == 2 and len(drafts[1].image_paths) == 1
    assert [g.get("draft_id") for g in b.groups] == [drafts[0].id, None, drafts[1].id]
    page = client.get(f"/new-ad/batch/{batch_id}").text
    assert f'href="/new-ad/{drafts[0].id}"' in page and "KTM Kinderrad 20 Zoll blau" in page
    assert f'href="/new-ad/batch/{batch_id}"' in client.get(f"/new-ad/{drafts[0].id}").text

    # drafts are locked: a later save cannot move their photos, a re-sort only gets the rest
    client.post(f"/new-ad/batch/{batch_id}", data={"img_0": "g2", "action": "save"})
    assert ad_batch.get(batch_id).groups[0]["photos"] == [0, 1]
    client.post(f"/new-ad/batch/{batch_id}", data={"img_2": "g2", "action": "resort"})
    b = _wait(batch_id, "review")
    assert len(fake.calls[-1]["images"]) == 2                                    # photos 2 and 4
    assert b.groups[0]["draft_id"] == drafts[0].id and b.groups[1]["draft_id"] == drafts[1].id

    client.post(f"/new-ad/batch/{batch_id}/delete")
    assert ad_batch.get(batch_id) is None
    assert client.get(f"/new-ad/{drafts[0].id}").status_code == 200


def test_batch_without_photos_and_recover(client):  # noqa: F811
    r = client.post("/new-ad/batch", data={"notes": "x"}, follow_redirects=False)
    assert r.headers["location"].startswith("/new-ad?msg=")
    b = ad_batch.new_batch("")
    ad_batch._update(b.id, status="sorting")
    ad_batch.recover()
    assert ad_batch.get(b.id).status == "error"


def test_batch_inline_review_and_publish(client, monkeypatch):  # noqa: F811
    _sorting_backend(client, monkeypatch)
    photos = [("photos", (f"IMG_{i}.jpg", b"jpeg-bytes-%d" % i, "image/jpeg")) for i in range(3)]
    r = client.post("/new-ad/batch", files=photos, follow_redirects=False)
    batch_id = int(r.headers["location"].split("/")[-1].split("?")[0])
    _wait(batch_id, "review")
    client.post(f"/new-ad/batch/{batch_id}", data={"img_0": "g1", "img_1": "g1", "include_g1": "1",
                                                   "img_2": "g2", "include_g2": "1", "action": "create"})
    _wait(batch_id, "review")
    assert client.get(f"/new-ad/batch/{batch_id}/progress").json() == {"status": "review", "drafts": 2}
    with dbmod.session() as db:
        d1, d2 = db.exec(select(dbmod.AdDraft).order_by(dbmod.AdDraft.id)).all()

    page = client.get(f"/new-ad/batch/{batch_id}").text
    assert "Review &amp; publish" in page and page.count('class="bdraft ') == 2
    assert f'hx-post="/ad-drafts/{d1.id}/inline"' in page and "0</strong> of 2 published" in page

    card = client.post(f"/ad-drafts/{d1.id}/inline", data={"title": "Bohrschrauber", "description": "Top\r\nZustand",
                                                          "price": "45,50"}).text
    assert f'id="draft-{d1.id}"' in card and "saved" in card and "<html" not in card
    with dbmod.session() as db:
        d = db.get(dbmod.AdDraft, d1.id)
    assert (d.title, d.description, d.price_suggestion) == ("Bohrschrauber", "Top\nZustand", 45.5)

    # refine saves unsaved edits first and goes through the normal refine (undo-able)
    client.post(f"/ad-drafts/{d1.id}/inline/refine", data={"instruction": "kürzer", "title": "Neu", "description": "x"})
    wait_jobs()
    assert "Änderungswünsche: kürzer" in client.fake.calls[-1]["prompt"] and "- Titel: Neu" in client.fake.calls[-1]["prompt"]
    with dbmod.session() as db:
        assert db.get(dbmod.AdDraft, d1.id).revisions[-1]["title"] == "Neu"

    card = client.post(f"/ad-drafts/{d1.id}/inline/status", data={"status": "published"}).text
    assert "published" in card and "Not published after all" in card
    client.post(f"/ad-drafts/{d2.id}/inline/status", data={"status": "discarded"})
    assert "1</strong> of 2 published, 1 discarded" in client.get(f"/new-ad/batch/{batch_id}").text
    assert client.post(f"/ad-drafts/{d2.id}/inline/status", data={"status": "bogus"}).status_code == 400

    import io
    import zipfile
    r = client.get(f"/ad-drafts/{d1.id}/photos.zip")
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert r.headers["content-type"] == "application/zip" and len(names) == 2 and names[0].endswith("-01.jpg")


def test_batch_card_renders_willhaben_plan(client):  # noqa: F811
    """A prepared draft shows category options and required fields (regression: f.values was dict.values)."""
    from app.services import ad_creator

    with dbmod.session() as db:
        d = dbmod.AdDraft(input_text="x", title="Logitech S125i", description="Gut.", price_suggestion=12)
        db.add(d)
        db.commit()
        db.refresh(d)
    ad_creator.update_draft(d.id, wh_plan={"location": "1150 Wien", "contact": "", "options": [
        {"path": [{"label": "Mobile Lautsprecher", "code": "MOBILE_SPEAKER"}], "label": "Audio › Mobile Lautsprecher",
         "fields": [{"code": "Zustand", "label": "Zustand", "values": [["neu", "Neu"], ["gebraucht", "Gebraucht"]],
                     "selected": "gebraucht"}]}]})
    from app.main import _draft_card
    from starlette.requests import Request

    html = _draft_card(Request({"type": "http", "headers": [], "method": "GET", "path": "/"}),
                       ad_creator.update_draft(d.id)).body.decode()
    assert 'name="o0_Zustand"' in html and '<option value="gebraucht" selected>Gebraucht</option>' in html
    assert "Audio › Mobile Lautsprecher" in html and "/inline/publish" in html


def test_category_queries_order():
    from app.services.ad_creator import category_queries

    q = category_queries("Logitech Rechargeable Speaker S715i, schwarz, iPod-Dock", "Logitech S715i", "logitech s715i",
                         user_query="Lautsprecher")
    assert q == ["Lautsprecher", "Logitech Rechargeable Speaker S715i, schwarz, iPod-Dock",
                 "Logitech Rechargeable Speaker S715i", "Logitech Rechargeable Speaker", "Logitech S715i"]
    assert category_queries("Rad", None, "rad") == ["Rad"]
