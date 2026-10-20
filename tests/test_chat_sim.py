"""Chat simulator: answers built like the real automatic ones, nothing sent or stored as a chat."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import Conversation, Draft, MyAd, SimChat, SimProposal, get_setting, session, set_setting
from app.llm.base import LLMResult
from app.services import chat_sim, jobs


class _Backend:
    """Answers by the schema it is asked for: the partner's message, a judge verdict, a proposal or my reply."""
    model = "fake"

    def __init__(self):
        self.reply = {}
        self.calls = []
        self.models = []

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt})
        props = schema.get("properties", {})
        if "done" in props:
            return LLMResult(data={"message": "Was ist der letzte Preis? 5 €?", "done": False}, model=self.model)
        if "passed" in props:
            return LLMResult(data={"passed": "Abholung" in prompt, "reason": "geprüft"}, model=self.model)
        if "findings" in props:
            return LLMResult(data={"findings": ["Zu lang"], "new_text": "Kürzerer System-Prompt.", "summary": "Kürzer."},
                             model=self.model)
        if "target" in props:
            return LLMResult(data={"target": props["target"]["enum"][0], "new_text": "- Nie unter 15 € gehen.",
                                   "summary": "Klare Untergrenze."}, model=self.model)
        if "opening_offer" in props:
            return LLMResult(data={"message": "Hallo, wäre 90 € ok?", "opening_offer": 90}, model=self.model)
        base = {"reply": "Ja, ist noch da. Abholung in Wien ist möglich.", "intent": "availability", "needs_info": [],
                "suggested_price": None, "note": "", "auto_send": True, "auto_reason": "einfache Frage",
                "meetup": None, "alternatives": []}
        if "purchase_state" in props:
            base["purchase_state"] = {"stage": "negotiating", "agreed_price": None, "shipping_cost": None,
                                      "total_price": None, "payment": None, "pickup": None, "answers": [], "reason": ""}
        return LLMResult(data={**base, **self.reply}, model=self.model)


@pytest.fixture
def backend(monkeypatch):
    b = _Backend()
    monkeypatch.setattr(chat_sim, "get_backend", lambda *a, **k: (b.models.append(k.get("model")), b)[1])
    with session() as db:
        db.add(MyAd(ad_id="111", heading="Logitech S315i", status="active", min_price=15,
                    auto_reply_instructions="Netzteil ist dabei."))
        db.commit()

    async def no_public(ad_id):
        return {"price": 20.0, "description": "Lautsprecher mit Netzteil", "location": "Wien"}

    monkeypatch.setattr(chat_sim, "_public_ad", no_public)
    return b


async def _done():
    for _ in range(200):
        if not jobs.busy("sim"):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("simulator job did not finish")


async def test_answer_uses_real_rules_and_checks_nothing_is_stored(backend):
    sim = await chat_sim.create("seller", ad_id="111", persona="lowball")
    assert sim.setup["price"] == 20.0 and sim.setup["title"] == "Logitech S315i"
    assert chat_sim.say(sim.id, "Ist der noch da?")
    await _done()
    turns = chat_sim.get(sim.id).turns
    assert [x["who"] for x in turns] == ["partner", "me"]
    assert turns[1]["auto"] and turns[1]["blockers"] == []
    prompt = backend.calls[-1]["prompt"]
    assert "Netzteil ist dabei." in prompt and "Untergrenze 15 €" in prompt     # the ad's live instructions and floor
    with session() as db:                                                      # never a real chat or draft
        assert db.exec(select(Conversation)).all() == [] and db.exec(select(Draft)).all() == []


async def test_price_below_the_floor_is_held_back(backend):
    backend.reply = {"reply": "Ok, 10 € passt.", "suggested_price": 10, "intent": "price_negotiation"}
    sim = await chat_sim.create("seller", ad_id="111")
    chat_sim.update_setup(sim.id, {"min_price": 12})                         # the test chat's own floor wins
    chat_sim.say(sim.id, "10 €?")
    await _done()
    me = chat_sim.get(sim.id).turns[-1]
    assert not me["auto"] and any("12" in b for b in me["blockers"])
    assert "Untergrenze 12 €" in backend.calls[-1]["prompt"]


async def test_play_lets_claude_be_the_other_side(backend):
    sim = await chat_sim.create("seller", ad_id="111", persona="lowball")
    assert chat_sim.play(sim.id, 2)
    await _done()
    turns = chat_sim.get(sim.id).turns
    assert [x["who"] for x in turns] == ["partner", "me", "partner", "me"] and turns[0]["played"]
    assert any("Schnäppchenjäger" in c["prompt"] for c in backend.calls)


async def test_buyer_chat_opens_and_follows_the_purchase(backend):
    sim = await chat_sim.create("buyer", platform="willhaben")
    chat_sim.update_setup(sim.id, {"title": "Fahrrad", "price": 100, "max_price": 95, "mode": "pickup"})
    assert chat_sim.reply(sim.id)                                              # empty buyer chat: the opener
    await _done()
    assert chat_sim.get(sim.id).turns[0]["opener"]
    backend.reply = {"reply": "Gut, 120 € passt mir.", "suggested_price": 120,
                     "purchase_state": {"stage": "agreed", "agreed_price": 120, "shipping_cost": None, "total_price": None,
                                        "payment": None, "pickup": None, "answers": [], "reason": ""}}
    chat_sim.say(sim.id, "Unter 120 geht nichts.")
    await _done()
    me = chat_sim.get(sim.id).turns[-1]
    assert not me["auto"] and any("95" in b for b in me["blockers"])          # above the maximum
    assert chat_sim.stage_of(chat_sim.get(sim.id).turns)[0] == "agreed"


async def test_other_platform_gets_its_rule(backend):
    sim = await chat_sim.create("buyer", platform="vinted")
    assert sim.setup["mode"] != "paylivery"
    chat_sim.say(sim.id, "Hallo")
    await _done()
    assert "Vinted" in backend.calls[-1]["prompt"]


async def test_scenario_run_checks_expectations(backend):
    sim = await chat_sim.create("seller", ad_id="111")
    chat_sim.say(sim.id, "Noch da?")
    await _done()
    for kind, value in (("auto", None), ("floor", "15"), ("contains", "abholung|versand"), ("not_contains", "@"),
                        ("judge", "schlägt Abholung vor"), ("scam", None)):
        assert chat_sim.add_expectation(sim.id, kind, value)
    assert not chat_sim.add_expectation(sim.id, "floor", "")                   # needs a value
    assert not chat_sim.add_expectation(sim.id, "ceiling", "10")               # buyer only
    assert chat_sim.run_scenarios() == 1
    await _done()
    run = chat_sim.get(sim.id).last_run
    passed = {r["kind"]: r["passed"] for r in run["results"]}
    assert passed == {"auto": True, "floor": True, "contains": True, "not_contains": True, "judge": True, "scam": False}
    assert not run["passed"]


async def test_proposal_is_tried_then_applied(backend):
    set_setting("auto_reply_rules", "- Freundlich bleiben.")
    sim = await chat_sim.create("seller", ad_id="111")
    chat_sim.say(sim.id, "Noch da?")
    await _done()
    chat_sim.add_expectation(sim.id, "auto", None)
    chat_sim.rate(sim.id, 1, "down", "zu förmlich")
    assert chat_sim.can_suggest("seller")
    assert chat_sim.suggest(sim.id)
    await _done()
    [p] = chat_sim.open_proposals()
    assert p.target == "auto_reply_rules" and p.old_text == "- Freundlich bleiben." and ("+", "- Nie unter 15 € gehen.") in chat_sim.diff(p)
    assert "zu förmlich" in backend.calls[-1]["prompt"]
    assert chat_sim.run_scenarios(proposal_id=p.id) == 1
    await _done()
    assert "Nie unter 15 € gehen." in backend.calls[-1]["prompt"]               # the proposal's rules, not the real ones
    assert get_setting("auto_reply_rules") == "- Freundlich bleiben."
    assert chat_sim.get(sim.id).proposal_run["variant"] == f"proposal:{p.id}"
    chat_sim.apply_proposal(p.id)
    assert get_setting("auto_reply_rules") == "- Nie unter 15 € gehen."
    s = chat_sim.get(sim.id)
    assert s.proposal_run is None and s.last_run["variant"] == "current"


async def test_ad_instructions_proposal_only_for_its_ad(backend):
    p = SimProposal(role="seller", target="ad_instructions", ad_id="111", old_text="", new_text="Nur Abholung.")
    sim = await chat_sim.create("seller")                                      # a hand-made ad: another ad
    turns = [{"who": "partner", "text": "Hallo", "at": None}]
    await chat_sim.answer(sim, turns, chat_sim.overrides_of(p))
    assert "Nur Abholung." not in backend.calls[-1]["prompt"]
    own = await chat_sim.create("seller", ad_id="111")
    await chat_sim.answer(own, turns, chat_sim.overrides_of(p))
    assert "Nur Abholung." in backend.calls[-1]["prompt"]


def test_simulator_page(backend):
    from app.main import app

    with TestClient(app) as c:
        r = c.post("/simulator/new", data={"role": "seller", "ad_id": "111", "persona": "scammer"}, follow_redirects=False)
        assert r.status_code == 303 and "/simulator?id=" in r.headers["location"]
        sid = int(r.headers["location"].split("id=")[1].split("&")[0])
        c.post(f"/simulator/{sid}/setup", data={"title": "Betrugstest", "min_price": "18", "persona": "scammer"})
        sim = chat_sim.get(sid)
        assert sim.title == "Betrugstest" and sim.setup["min_price"] == 18.0
        with session() as db:
            row = db.get(SimChat, sid)
            row.turns = [{"who": "partner", "text": "Kurier holt ab", "at": None},
                         {"who": "me", "text": "Nur Abholung bitte.", "auto": False, "blockers": ["Betrugsverdacht"],
                          "intent": "scam_suspect", "reason": "Kurier", "at": None}]
            db.add(row)
            db.commit()
        c.post(f"/simulator/{sid}/expect", data={"kind": "scam"})
        page = c.get(f"/simulator?id={sid}").text
        assert "Betrugstest" in page and "Kurier holt ab" in page and "Betrugsverdacht" in page
        assert "Recognises the scam" in page
        c.post(f"/simulator/{sid}/rate/1", data={"rating": "down", "note": "zu knapp"})
        assert chat_sim.get(sid).turns[1]["rating"] == "down"
        assert "/simulator" in c.get("/chats").text
        c.post(f"/simulator/{sid}/delete")
        assert chat_sim.get(sid) is None
        assert "Pick or start a test chat" in c.get("/simulator").text
        r = c.post("/simulator/new", data={"role": "buyer", "platform": "kleinanzeigen", "persona": "hard_seller"},
                   follow_redirects=False)
        page = c.get(r.headers["location"]).text
        assert "Your maximum incl. shipping" in page and "kleinanzeigen.de" in page and "PayLivery</option>" not in page


async def test_template_fills_in_the_scenario_without_sending(backend):
    sim = await chat_sim.create_from_template("lowball", ad_id="111")
    assert sim.scenario and sim.persona == "lowball" and sim.turns == []
    assert chat_sim.next_script(sim).startswith("Hallo, würde dir 10 €")       # half of the ad's 20 €, in the composer
    assert {"kind": "floor", "value": 15} in sim.expectations                  # the ad's minimum
    assert not jobs.busy("sim") and backend.calls == []                       # nothing sent, nothing run
    chat_sim.say(sim.id, chat_sim.next_script(sim))
    await _done()
    assert chat_sim.next_script(chat_sim.get(sim.id)).startswith("Ok, letztes Angebot 12 €")


async def test_buyer_template_without_purchase_uses_the_sample(backend):
    sim = await chat_sim.create_from_template("price_fixed")
    assert sim.setup["title"] == chat_sim.SAMPLE_BUY["title"] and {"kind": "ceiling", "value": 140.0} in sim.expectations
    chat_sim.say(sim.id, chat_sim.next_script(sim))
    await _done()
    assert chat_sim.run_scenarios([sim.id]) == 1
    await _done()
    run = chat_sim.get(sim.id).last_run
    assert run["turns"][0].get("opener") and [x["who"] for x in run["turns"][1:]] == ["partner", "me"]


def test_all_templates_from_the_page(backend):
    from app.main import app

    with TestClient(app) as c:
        assert "Quick start: ready scenarios" in c.get("/simulator").text
        r = c.post("/simulator/template", data={"role": "seller", "template": "all", "ad_id": "111"},
                   follow_redirects=False)
        assert r.status_code == 303
    seller = [k for k, _l in chat_sim.templates_for("seller")]
    assert len([s for s in chat_sim.all_chats() if s.scenario]) == len(seller)
    first = next(s for s in chat_sim.all_chats() if s.title.startswith("“Still available?”"))
    with TestClient(app) as c:
        assert ">Noch da?</textarea>" in c.get(f"/simulator?id={first.id}").text


async def test_random_ad_fills_the_setup(backend, monkeypatch):
    import random
    from types import SimpleNamespace

    class _Public:
        async def search_keyword(self, word, rows=30):
            return SimpleNamespace(ads=[SimpleNamespace(ad_id="1", price=None), SimpleNamespace(ad_id="2", price=200.0)])

        async def get_detail(self, ad_id):
            assert ad_id == "2"
            return SimpleNamespace(ad_id="2", heading="Sofa grau", price=200.0, description="Bequem", location="Graz",
                                   url="https://www.willhaben.at/iad/2", seller_name="Verkäufer", paylivery_enabled=True,
                                   shipping_price=None)

    monkeypatch.setattr("app.willhaben.public.get_public", lambda: _Public())
    sim = await chat_sim.create_random("buyer", rng=random.Random(1))
    s = sim.setup
    assert s["title"] == "Sofa grau" and s["max_price"] == 200.0 and s["wish_price"] == 170 and s["mode"] == "paylivery"
    assert sim.title == "Random: Sofa grau" and sim.role == "buyer"


def test_old_run_time_renders(backend):
    from app.main import app

    with session() as db:
        db.add(SimChat(title="Alt", role="seller", scenario=True, turns=[], expectations=[],
                       last_run={"at": "2026-10-17T20:00:00+00:00", "passed": True, "results": [], "turns": []}))
        db.commit()
        sid = db.exec(select(SimChat.id)).first()
    with TestClient(app) as c:
        assert c.get(f"/simulator?id={sid}").status_code == 200


async def test_randomize_replaces_the_article(backend, monkeypatch):
    from types import SimpleNamespace

    class _Public:
        async def search_keyword(self, word, rows=30):
            return SimpleNamespace(ads=[SimpleNamespace(ad_id="2", price=80.0)])

        async def get_detail(self, ad_id):
            return SimpleNamespace(heading="Gitarre", price=80.0, description="Akustik", location="Linz",
                                   url="https://www.willhaben.at/iad/2", seller_name="", paylivery_enabled=False,
                                   shipping_price=None)

    monkeypatch.setattr("app.willhaben.public.get_public", lambda: _Public())
    sim = await chat_sim.create_from_template("lowball", ad_id="111")
    chat_sim._save(sim.id, turns=[{"who": "partner", "text": "x", "at": None}])
    s = await chat_sim.randomize(sim.id)
    assert s.setup["title"] == "Gitarre" and s.ad_id is None and s.turns == [] and "script" not in s.setup
    assert s.expectations                                                     # the scenario's checks stay


async def test_custom_models_per_test_chat(backend):
    from app.llm import models as llm_models

    opus, haiku = llm_models.newest("opus"), llm_models.newest("haiku")
    sim = await chat_sim.create("seller", ad_id="111")
    chat_sim.update_setup(sim.id, {}, model=opus, partner_model="haiku")
    chat_sim.update_setup(sim.id, {}, model="no-such-model")                  # unknown: left as it was
    s = chat_sim.get(sim.id)
    assert s.model == opus and s.partner_model == haiku
    chat_sim.play(sim.id, 1)
    await _done()
    assert backend.models[-2:] == [haiku, opus]                                # the other side, then my answer
    chat_sim.update_setup(sim.id, {}, model="")
    assert chat_sim.get(sim.id).model is None


def test_reply_prompt_names_the_ads_handover():
    from app.llm.prompts import reply_prompt

    def p(ad):
        return reply_prompt(role="seller", ad={"title": "X", **ad}, partner=None, messages=[], rules="", examples=[], hint=None)
    assert "Abholung oder Versand mit willhaben PayLivery, Versandkosten 3,49 €" in p({"paylivery": True, "shipping_price": 3.49})
    assert "Übergabe laut Anzeige: nur Abholung" in p({"shipping": "pickup"})
    assert "Übergabe laut Anzeige" not in p({})


async def test_setup_takes_the_handover_and_answers_keep_their_prompt(backend, monkeypatch):
    async def public(ad_id):
        return {"price": 20.0, **chat_sim.delivery_of(type("D", (), {"paylivery_enabled": True, "shipping": "ship",
                                                                      "shipping_price": 4.5})())}
    monkeypatch.setattr(chat_sim, "_public_ad", public)
    sim = await chat_sim.create("seller", ad_id="111")
    assert sim.setup["delivery"] == "paylivery" and sim.setup["ad_shipping"] == 4.5
    chat_sim.say(sim.id, "Verschickst du?")
    await _done()
    me = chat_sim.get(sim.id).turns[-1]
    assert "Versand mit willhaben PayLivery, Versandkosten 4,50 €" in me["prompt"]["user"] and me["prompt"]["system"]
    from app.main import app

    with TestClient(app) as c:
        frag = c.get(f"/simulator/{sim.id}/prompt/1").text
        assert "System prompt" in frag and "Verschickst du?" in frag
        assert "No prompt stored" in c.get(f"/simulator/{sim.id}/prompt/0").text     # written by me
        assert c.get(f"/simulator/{sim.id}/prompt/9").status_code == 404


def test_delete_several_test_chats(backend):
    from app.main import app

    with session() as db:
        for n in range(3):
            db.add(SimChat(title=f"T{n}", turns=[], expectations=[]))
        db.commit()
    ids = [s.id for s in chat_sim.all_chats()]
    with TestClient(app) as c:
        r = c.post("/simulator-delete", data={"ids": [str(i) for i in ids[:2]], "back_to": str(ids[0])},
                   follow_redirects=False)
        assert r.status_code == 303 and f"id={ids[0]}" not in r.headers["location"]
    assert [s.id for s in chat_sim.all_chats()] == ids[2:]


async def test_system_prompt_edit_review_and_reset(backend):
    from app.llm import models as llm_models
    from app.llm.prompts import AUTO_REPLY_SYSTEM
    from app.services import system_prompts, topic

    assert system_prompts.get("auto_reply") == AUTO_REPLY_SYSTEM and not system_prompts.custom("auto_reply")
    system_prompts.save("auto_reply", "Mein eigener System-Prompt für automatische Antworten, bitte sehr knapp.")
    assert system_prompts.custom("auto_reply")
    assert topic.leaks_prompt("eigener System-Prompt für automatische Antworten, bitte sehr knapp")   # leak check knows it
    sim = await chat_sim.create("seller", ad_id="111")
    chat_sim.say(sim.id, "Noch da?")
    await _done()
    assert backend.calls[-1]["system"].startswith("Mein eigener System-Prompt")
    opus = llm_models.newest("opus")
    assert chat_sim.review("auto_reply", "kürzer", opus)
    await _done()
    assert backend.models[-1] == opus and "kürzer" in backend.calls[-1]["prompt"]
    [p] = [x for x in chat_sim.open_proposals() if x.target == "auto_reply_system"]
    assert p.new_text == "Kürzerer System-Prompt." and "• Zu lang" in p.summary
    chat_sim.add_expectation(sim.id, "auto", None)
    chat_sim.run_scenarios(proposal_id=p.id)                                  # tried without changing the real prompt
    await _done()
    assert backend.calls[-1]["system"] == "Kürzerer System-Prompt."
    assert system_prompts.get("auto_reply").startswith("Mein eigener")
    chat_sim.apply_proposal(p.id)
    assert system_prompts.get("auto_reply") == "Kürzerer System-Prompt."
    system_prompts.reset("auto_reply")
    assert system_prompts.get("auto_reply") == AUTO_REPLY_SYSTEM and not system_prompts.custom("auto_reply")
    system_prompts.save("auto_reply", AUTO_REPLY_SYSTEM)                      # the default itself: stays the default
    assert not system_prompts.custom("auto_reply")


def test_system_prompt_routes(backend):
    from app.main import app
    from app.services import system_prompts

    with TestClient(app) as c:
        page = c.get("/simulator").text
        assert "System prompts" in page and "Claude review" in page and 'data-prompt-kind="purchase"' in page
        c.post("/simulator-prompts/sim_partner/save", data={"text": "Spiel die Gegenseite sehr kurz."})
        assert system_prompts.get("sim_partner") == "Spiel die Gegenseite sehr kurz."
        assert "Reset to default" in c.get("/simulator").text
        c.post("/simulator-prompts/purchase/propose", data={"text": "Neuer Kauf-Prompt"})
        assert [p.target for p in chat_sim.open_proposals()] == ["purchase_system"]
        assert system_prompts.custom("purchase") is False
        c.post("/simulator-prompts/sim_partner/reset")
        assert not system_prompts.custom("sim_partner")
        assert c.post("/simulator-prompts/nope/save", data={"text": "x"}).status_code == 404



async def test_rounds_refine_the_system_prompt_step_by_step(monkeypatch):
    """Round 1 is the chat itself; a stronger model's review fills the feedback; a revision plays round 2 with the
    revised system prompt (an open proposal), shown as a tab of the chat, ready to apply."""
    from fastapi.testclient import TestClient

    from app.db import SimChat, session
    from app.llm.base import LLMResult
    from app.main import app
    from app.services import chat_sim

    with session() as db:
        s = SimChat(title="Zu niedrig", role="seller", platform="willhaben", turns=[
            {"who": "partner", "text": "Geht 20?"}, {"who": "me", "text": "Ja, hol es ab.", "auto": True}])
        db.add(s)
        db.commit()
        db.refresh(s)
    seen = []

    async def replay(sim, overrides=None):
        seen.append(overrides)
        return [{"who": "partner", "text": "Geht 20?"}, {"who": "me", "text": "Nein, 65 ist das Minimum.", "auto": False}]

    async def check(sim, turns):
        return []
    monkeypatch.setattr(chat_sim, "replay", replay)
    monkeypatch.setattr(chat_sim, "check", check)
    calls = []

    class Backend:
        async def complete(self, **kw):
            calls.append(kw)
            if "review" in kw["schema"]["properties"]:
                return LLMResult(data={"review": "- Nie unter dem Mindestpreis zusagen."}, model="fake")
            return LLMResult(data={"findings": [], "new_text": "NEUER PROMPT", "summary": "Keine Abholung zusagen."},
                             model="fake")
    models = []
    monkeypatch.setattr(chat_sim, "get_backend", lambda purpose=None, model=None: models.append(model) or Backend())
    started = []
    monkeypatch.setattr(chat_sim.jobs, "submit", lambda key, label, fn, **kw: started.append(fn))
    job = type("J", (), {"progress": lambda *a, **k: None})()

    chat_sim.rate(s.id, 1, "down", "nie Abholung zusagen")             # round 1 = the chat: rated right there
    assert chat_sim.review_round(s.id)
    await started.pop()(job)
    assert "opus" in models[-1] and "App: Ja, hol es ab." in calls[-1]["prompt"] and "nie Abholung" in calls[-1]["prompt"]
    with TestClient(app) as c:
        page = c.get(f"/simulator?id={s.id}").text
    assert "- Nie unter dem Mindestpreis zusagen.</textarea>" in page and "data-rounds" not in page   # no tabs yet
    assert chat_sim.next_round(s.id, "- Nie unter dem Mindestpreis zusagen.")
    await started.pop()(job)
    sim = chat_sim.get(s.id)
    assert [r["n"] for r in sim.iterations] == [1, 2] and sim.iterations[0]["base"]
    assert "nie Abholung zusagen" in calls[-1]["prompt"] and seen == [{"auto_reply_system": "NEUER PROMPT"}]
    with TestClient(app) as c:
        page = c.get(f"/simulator?id={s.id}").text                     # the newest round is shown
        assert "data-rounds" in page and "Nein, 65 ist das Minimum." in page and "Apply this version" in page
        assert "Revise and play round 3" in page and "Claude changed the system prompt:" in page
        r1 = c.get(f"/simulator?id={s.id}&round=1").text
        assert "Ja, hol es ab." in r1 and "Nein, 65 ist das Minimum." not in r1
    await chat_sim._append(s.id, {"who": "partner", "text": "Und 30?"})       # a new message: the rounds start again
    assert chat_sim.get(s.id).iterations is None
