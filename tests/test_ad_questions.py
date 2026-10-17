"""Ad texts never carry a "(bitte prüfen)": what Claude cannot tell becomes questions on the draft page (and in the
My-ads editor), answered with option chips or own text and worked in by one revision."""
import json

from app import db as dbmod
from app.llm.prompts import AD_SCHEMA, AD_SYSTEM
from app.services import ad_creator
from tests.conftest import wait_jobs
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)

QUESTIONS = [{"id": "ir", "text": "Wie weit reicht die Nachtsicht?", "options": ["ca. 30 m", "ca. 50 m"]},
             {"id": "box", "text": "Ist das Montagezubehör dabei?", "options": ["Ja", "Nein"]},
             {"id": "bad", "text": "Nur eine Option", "options": ["x"]}]                 # dropped: < 2 options


def _get(draft_id):
    with dbmod.session() as db:
        return db.get(dbmod.AdDraft, draft_id)


def test_prompt_and_schema_ask_instead_of_marking():
    assert "questions" in AD_SCHEMA["required"] and 'Schreib nie "(bitte prüfen)"' in AD_SYSTEM
    assert ad_creator.clean_text("Reichweite ca. 30 m (bitte prüfen). Gut [prüfen]") == "Reichweite ca. 30 m. Gut"
    assert "(bitte prüfen)" not in ad_creator.RECONCILE_INSTRUCTION


async def test_questions_on_the_draft_and_answers_worked_in(client):  # noqa: F811
    client.fake.ad_extra = {"questions": QUESTIONS, "description": "Dome-Kamera, IR ca. 30 m (bitte prüfen)."}
    d = await ad_creator.create_draft("Kamera", [])
    assert [q["id"] for q in d.questions] == ["ir", "box"] and "(bitte prüfen)" not in d.description
    page = client.get(f"/new-ad/{d.id}").text
    assert "data-ad-questions" in page and 'name="q_ir" value="ca. 30 m"' in page and 'name="t_box"' in page

    r = client.post(f"/new-ad/{d.id}/answers", data={}, follow_redirects=False)
    assert "msg=" in r.headers["location"]                                   # nothing answered
    client.fake.ad_extra = {"questions": []}
    client.post(f"/new-ad/{d.id}/answers", data={"q_ir": "ca. 30 m", "q_box": "Ja", "t_box": "Ja, Schrauben und Dübel"})
    wait_jobs()
    prompt = client.fake.calls[-1]["prompt"]
    assert "Wie weit reicht die Nachtsicht? → ca. 30 m" in prompt and "→ Ja, Schrauben und Dübel" in prompt
    after = _get(d.id)
    assert after.questions is None and after.revisions[-1]["instruction"] == "Your answers"

    ad_creator.update_draft(d.id, questions=QUESTIONS[:1])
    client.post(f"/new-ad/{d.id}/answers", data={"skip": "1"})
    assert _get(d.id).questions is None


def test_my_ads_editor_shows_and_sends_the_answers(client, monkeypatch):  # noqa: F811
    from app.services import my_ads

    with dbmod.session() as db:
        db.add(dbmod.MyAd(ad_id="77", heading="Kamera", url="https://www.willhaben.at/iad/x-77"))
        db.commit()
    asked = []

    async def ai_revise(ad_id, heading, description, price, instruction):
        asked.append(instruction)
        return {"heading": heading, "description": description, "price_estimate": None, "note": "",
                "questions": QUESTIONS[:1] if len(asked) == 1 else []}
    monkeypatch.setattr(my_ads, "ai_revise", ai_revise)
    page = client.post("/myads/77/edit/ai", data={"heading": "Kamera", "description": "Text", "instruction": "kürzer"}).text
    assert "data-ad-questions" in page and 'name="q_ir"' in page
    client.post("/myads/77/edit/ai", data={"heading": "Kamera", "description": "Text", "instruction": "",
                                           "questions": json.dumps(QUESTIONS[:1]), "q_ir": "ca. 50 m"})
    assert "Wie weit reicht die Nachtsicht? → ca. 50 m" in asked[-1]
