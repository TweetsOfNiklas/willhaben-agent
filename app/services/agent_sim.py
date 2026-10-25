"""The search agent simulator: how would an agent judge with other settings? Nothing is stored on the hits, nothing is
pushed, no Geizhals search of Claude's own runs.

- replay: the agent's recent hits judged again with an edited copy of its judging settings (Claude's brief, the
  threshold, the hidden reasons, the Geizhals rule and check, the pickup distance) – the rules without Claude, the
  screening through the same call as the real one (screening.ask) – next to what really happened.
- tests: Claude invents tricky ads for the agent (wrong product, accessory, wrong variant, defective, search/swap,
  scam signs, bargains, overpriced), each with the verdict it should get; the screening judges them; mismatches are
  marked, the owner rates each verdict, and Claude may propose a better brief from that.

Runs are kept (AgentSim) to reopen and compare; they run as background jobs (lane "sim"), the page asks again until
they are done. Claude runs with the screening's model (purpose "screen"), as the agent would.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel, col, select

from app.db import EventLog, Hit, SearchAgent, session, utcnow
from app.i18n import LANGS, background_lang, t
from app.llm.base import LLMError, get_backend
from app.services import auto_buy, screening

log = logging.getLogger(__name__)

SCOPES = (30, 50, 100)              # how many recent hits a replay judges
TEST_COUNTS = (5, 10, 15)           # how many ads Claude invents
SETTING_KEYS = ("screen", "screen_brief", "screen_min", "screen_hide", "geizhals_pct", "geizhals_level",
                "geizhals_check", "max_km", "auto_buy", "auto_buy_max")
# what an invented ad is meant to test (the expected verdict follows from it)
TEST_KINDS = ("bargain", "fair_price", "overpriced", "wrong_product", "accessory_only", "wrong_variant", "defective",
              "search_or_swap", "scam_suspect", "misses_requirement", "bundle_only")


class AgentSim(SQLModel, table=True):
    """One run of the search agent simulator (see the module doc)."""
    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(index=True)
    kind: str = "replay"                   # replay | tests
    settings: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # the judging settings simulated
    scope: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))      # replay: {n, only}; tests: {n}
    status: str = "running"                # running | done | error
    error: str | None = None
    results: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    summary: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    ratings: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))    # {index: {up, note}}
    proposal: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # {old, new, summary, status}
    created_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: datetime | None = None


# ------------------------------------------------------------------ settings
def settings_of(agent: SearchAgent) -> dict[str, Any]:
    """The agent's judging settings as the simulator edits them (screen_hide as a list, None = the defaults)."""
    hide = agent.screen_hide
    return {"screen": bool(agent.screen), "screen_brief": agent.screen_brief or "", "screen_min": agent.screen_min or "match",
            "screen_hide": sorted(screening.hide_set(agent)) if hide is not None else sorted(screening.DEFAULT_HIDE),
            "geizhals_pct": agent.geizhals_pct, "geizhals_level": agent.geizhals_level,
            "geizhals_check": bool(agent.geizhals_check), "max_km": agent.max_km,
            "auto_buy": bool(agent.auto_buy), "auto_buy_max": agent.auto_buy_max}


def parse_settings(form: Any) -> dict[str, Any]:
    """The settings form (agent_sim.html): checkboxes are "on" when ticked, numbers may be empty."""
    def num(key: str) -> int | None:
        raw = str(form.get(key) or "").strip()
        return int(raw) if raw.isdigit() and int(raw) > 0 else None
    def money(key: str) -> float | None:
        raw = str(form.get(key) or "").strip().replace("€", "").replace(",", ".")
        try:
            return round(float(raw), 2) if raw and float(raw) > 0 else None
        except ValueError:
            return None
    rule = str(form.get("gh_rule") or "")
    return {"screen": bool(form.get("screen")), "screen_brief": str(form.get("screen_brief") or "").strip(),
            "screen_min": str(form.get("screen_min") or "match") if str(form.get("screen_min") or "") in screening.MIN_RANK
            else "match",
            "screen_hide": sorted(h for h in form.getlist("screen_hide") if h in screening.HIDEABLE),
            "geizhals_pct": num("geizhals_pct") if rule == "pct" else None,
            "geizhals_level": rule if rule and rule != "pct" else None,
            "geizhals_check": bool(form.get("geizhals_check")), "max_km": num("max_km"),
            "auto_buy": bool(form.get("auto_buy")), "auto_buy_max": money("auto_buy_max")}


def as_agent(agent: SearchAgent, settings: dict[str, Any]) -> SearchAgent:
    """A copy of the agent with the simulated settings, never added to a session."""
    copy = SearchAgent.model_validate(agent.model_dump())
    for k in SETTING_KEYS:
        if k in settings:
            setattr(copy, k, settings[k])
    copy.screen_hide = ",".join(settings.get("screen_hide") or []) or "-"
    return copy


def changed_settings(agent: SearchAgent, settings: dict[str, Any]) -> list[str]:
    """The keys the simulated settings change compared with the agent as it is now."""
    now = settings_of(agent)
    return [k for k in SETTING_KEYS if (now.get(k) or None) != (settings.get(k) or None)]


def apply_settings(agent_id: int, settings: dict[str, Any]) -> None:
    """The simulated settings become the agent's own."""
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
        if agent is None:
            return
        for k in SETTING_KEYS:
            if k in settings:
                setattr(agent, k, settings[k])
        agent.screen_hide = ",".join(settings.get("screen_hide") or []) or "-"
        db.add(agent)
        db.commit()


# ------------------------------------------------------------------ runs
def runs(limit: int = 40) -> list[AgentSim]:
    with session() as db:
        return list(db.exec(select(AgentSim).order_by(col(AgentSim.id).desc()).limit(limit)))


def get(sim_id: int) -> AgentSim | None:
    with session() as db:
        return db.get(AgentSim, sim_id)


def delete(sim_id: int) -> None:
    with session() as db:
        row = db.get(AgentSim, sim_id)
        if row is not None:
            db.delete(row)
            db.commit()


def _save(sim_id: int, **fields: Any) -> None:
    with session() as db:
        row = db.get(AgentSim, sim_id)
        if row is None:
            return
        for k, v in fields.items():
            setattr(row, k, v)
        db.add(row)
        db.commit()


def start(agent_id: int, kind: str, settings: dict[str, Any], scope: dict[str, Any]) -> AgentSim:
    """A new run, judged in the background (jobs lane "sim"); the page shows it while it runs."""
    from app.services import jobs

    with session() as db:
        agent = db.get(SearchAgent, agent_id)
        if agent is None:
            raise ValueError(t("Unknown search agent"))
        row = AgentSim(agent_id=agent_id, kind=kind, settings=settings, scope=scope)
        db.add(row)
        db.commit()
        db.refresh(row)
        name = agent.name

    async def run(job: Any) -> str | None:
        try:
            if kind == "tests":
                results = await _tests(agent_id, settings, int(scope.get("n") or 10))
                summary = _tests_summary(results)
            else:
                results = await _replay(agent_id, settings, int(scope.get("n") or 50), str(scope.get("only") or "all"))
                summary = _replay_summary(results)
        except (LLMError, ValueError) as e:
            _save(row.id, status="error", error=str(e), finished_at=utcnow())
            return t("Agent simulator failed: {error}", error=e)
        _save(row.id, status="done", results=results, summary=summary, finished_at=utcnow())
        return t("{n} judged", n=len(results))

    jobs.submit("agentsim", t("Agent simulator: {name}", name=name), run, lane="sim", quiet=True,
                current=f"agentsim:{row.id}", link=f"/agent-sim?id={row.id}")
    return row


def _copy(h: Hit) -> Hit:
    return Hit.model_validate(h.model_dump())


def _verdict(h: Hit) -> dict[str, Any]:
    return {"relevant": h.screen_relevant, "deal": h.screen_deal, "issues": list(h.screen_issues or []),
            "reason": h.screen_reason}


async def _judge(agent: SearchAgent, hits: list[Hit], check_geizhals: bool) -> dict[int, dict[str, Any]]:
    """The dry screening of `hits` (copies, changed in place: their verdicts and, with the check, their Geizhals
    product – only one of the lookup's own results, no search of Claude's). {index: product note}."""
    notes: dict[int, dict[str, Any]] = {}
    for start_i in range(0, len(hits), screening.MAX_PER_CALL):
        part = hits[start_i:start_i + screening.MAX_PER_CALL]
        check = list(range(1, len(part) + 1)) if check_geizhals else []
        stats = screening._stats(agent.id, {h.id for h in part if h.id}) if agent.id else None
        by_nr, cands = await screening.ask(agent, part, check, stats)
        for nr, h in enumerate(part, 1):
            r = by_nr.get(nr)
            if r is None:
                continue
            for k, v in screening.verdict_fields(r, h).items():
                setattr(h, k, v)
            if nr in check:
                gh = dict(h.geizhals or {})
                options = cands.get(nr) or []
                pick = r.get("geizhals_pick")
                if r.get("geizhals_ok"):
                    note = {"product": "kept", "name": gh.get("name")}
                elif isinstance(pick, int) and 1 <= pick <= len(options):
                    c = options[pick - 1]
                    h.geizhals = {"name": c["name"], "url": c["url"], **({"price_from": c["price"]} if c.get("price") else {}),
                                  "by": "claude", "checked": True}
                    note = {"product": "picked", "name": c["name"]}
                else:        # the real check would search Geizhals itself; here the product's price is not used
                    h.geizhals = {}
                    note = {"product": "search", "name": (r.get("product") or "").strip() or None}
                notes[start_i + nr - 1] = note
    return notes


async def _replay(agent_id: int, settings: dict[str, Any], n: int, only: str) -> list[dict[str, Any]]:
    from app.services import search_agents

    with session() as db:
        agent = db.get(SearchAgent, agent_id)
        q = select(Hit).where(Hit.agent_id == agent_id, Hit.state != search_agents.CLEARED)
        if only == "held":
            q = q.where(col(Hit.state).in_(("filtered", search_agents.PENDING)))
        rows = list(db.exec(q.order_by(col(Hit.first_seen_at).desc()).limit(500 if only == "pushed" else n)))
        refs = {r.ad_id for r in rows} | {f"{r.platform}:{r.ad_id}" for r in rows if r.platform}
        pushed = set(db.exec(select(EventLog.ad_id).where(col(EventLog.ad_id).in_(refs or {"-"}),
                                                          col(EventLog.quiet).is_not(True))).all())
    if agent is None:
        raise ValueError(t("Unknown search agent"))
    was = lambda h: h.ad_id in pushed or f"{h.platform}:{h.ad_id}" in pushed   # noqa: E731
    if only == "pushed":
        rows = [h for h in rows if was(h)][:n]
    sim_agent = as_agent(agent, settings)
    copies = [_copy(h) for h in rows]
    for c in copies:                         # judged anew: the old verdict must not leak into the rules
        c.screen_relevant = c.screen_deal = c.screen_reason = None
        c.screen_issues = None
    notes: dict[int, dict[str, Any]] = {}
    if sim_agent.screen:
        notes = await _judge(sim_agent, copies, bool(sim_agent.geizhals_check))
    out = []
    for i, (h, c) in enumerate(zip(rows, copies, strict=True)):
        real_push = h.state not in ("filtered", search_agents.PENDING)
        sim_push = search_agents.passes(sim_agent, c)
        gh_real, gh_sim = (h.geizhals or {}).get("price_from"), (c.geizhals or {}).get("price_from")
        buy_no = auto_buy.why_not(sim_agent, c) if sim_push else t("no hit")
        out.append({"hit_id": h.id, "ad_id": h.ad_id, "platform": h.platform or "willhaben", "title": h.heading,
                    "price": h.price, "price_display": h.price_display, "image": h.image_url, "url": h.url,
                    "state": h.state, "real": {"push": real_push, "pushed": was(h), **_verdict(h), "geizhals": gh_real},
                    "sim": {"push": sim_push, "reasons": search_agents.held_back_reasons(sim_agent, c), **_verdict(c),
                            "geizhals": gh_sim, "product": notes.get(i),
                            "buy": sim_agent.auto_buy and buy_no is None, "buy_total": auto_buy.estimate(c),
                            "buy_why": buy_no},
                    "changed": real_push != sim_push})
    return out


def _replay_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"n": len(results), "push": sum(1 for r in results if r["sim"]["push"]),
            "held": sum(1 for r in results if not r["sim"]["push"]),
            "real_push": sum(1 for r in results if r["real"]["push"]),
            "now_push": sum(1 for r in results if r["changed"] and r["sim"]["push"]),
            "now_held": sum(1 for r in results if r["changed"] and not r["sim"]["push"]),
            "buy": sum(1 for r in results if r["sim"].get("buy"))}


# ------------------------------------------------------------------ invented ads
ADS_SYSTEM = """\
Du schreibst Test-Kleinanzeigen für eine Privatperson, die prüfen will, ob die automatische Prüfung ihres Suchagenten
richtig urteilt. Jede Anzeige soll realistisch klingen (Titel und Text wie echte Privatanzeigen auf willhaben.at, mit
Tippfehlern und Umgangssprache, wo es passt) und genau eine Schwierigkeit testen.

- kind: was die Anzeige testet, aus der festen Liste: bargain (das Gesuchte, deutlich unter dem üblichen Preis),
  fair_price (das Gesuchte zum üblichen Preis), overpriced (das Gesuchte, zu teuer), wrong_product (ähnlich klingendes
  anderes Produkt), accessory_only (nur Zubehör oder Ersatzteil), wrong_variant (andere Größe, Speicher, Generation),
  defective (defekt oder beschädigt), search_or_swap ("Suche …" oder Tauschanzeige), scam_suspect (Betrugsanzeichen,
  z. B. Vorauskasse, WhatsApp-Wechsel, Text und beschriebenes Foto passen nicht zusammen), misses_requirement (verfehlt
  eine ausdrückliche Vorgabe der Beschreibung), bundle_only (nur im Paket mit anderem). Verteile die Arten; mindestens
  zwei Anzeigen sollen echte Treffer sein (bargain oder fair_price), die übrigen knifflig, nicht plump.
- Preis in Euro als Zahl; nutze die genannten üblichen Preise, damit "Schnäppchen" und "zu teuer" stimmen.
- expected_relevant, expected_deal (great, good, fair, overpriced, unknown), expected_issues (aus: wrong_product,
  accessory_only, wrong_variant, misses_requirement, defective, search_or_swap, incomplete, bundle_only, too_far,
  scam_suspect, too_expensive, price_unclear): das Urteil, das eine sorgfältige Prüfung nach der Beschreibung der
  Person fällen sollte. why: ein kurzer Satz, warum.
- Keine echten Namen, Telefonnummern, Adressen oder Links. Orte nur als Bezirk oder Stadt.
"""

ADS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ads": {"type": "array", "items": {
        "type": "object",
        "properties": {"kind": {"type": "string", "enum": list(TEST_KINDS)}, "title": {"type": "string"},
                       "price": {"type": ["number", "null"]}, "description": {"type": "string"},
                       "condition": {"type": "string"}, "location": {"type": "string"}, "private": {"type": "boolean"},
                       "shipping": {"type": "boolean"},
                       "expected_relevant": {"type": "boolean"},
                       "expected_deal": {"type": "string", "enum": ["great", "good", "fair", "overpriced", "unknown"]},
                       "expected_issues": {"type": "array", "items": {"type": "string", "enum": list(screening.ISSUES)}},
                       "why": {"type": "string"}},
        "required": ["kind", "title", "price", "description", "condition", "location", "private", "shipping",
                     "expected_relevant", "expected_deal", "expected_issues", "why"],
        "additionalProperties": False}}},
    "required": ["ads"],
    "additionalProperties": False,
}


def terms_of(agent: SearchAgent) -> list[str]:
    """The agent's search terms: its keyword searches, else the keyword of its search link."""
    from urllib.parse import parse_qs, urlsplit

    terms = [str(q.get("keyword")) for q in agent.queries or [] if q.get("keyword")]
    if not terms:
        q = parse_qs(urlsplit(agent.source_url or "").query)
        terms = [v for k in ("keyword", "q", "search_text", "query", "_nkw") for v in q.get(k, [])][:1]
    return list(dict.fromkeys(terms))


def _ads_prompt(agent: SearchAgent, settings: dict[str, Any], n: int) -> str:
    with session() as db:
        prices = [p for p in db.exec(select(Hit.price).where(Hit.agent_id == agent.id, Hit.screen_relevant == True)  # noqa: E712
                                     .order_by(col(Hit.id).desc()).limit(80)) if p]
        titles = list(db.exec(select(Hit.heading).where(Hit.agent_id == agent.id).order_by(col(Hit.id).desc()).limit(12)))
    stats = screening.price_stats(prices) if prices else None
    lines = [f"Suchagent: {agent.name}", f"Suchbegriffe: {', '.join(terms_of(agent)) or agent.name}", "",
             "Was die Person genau sucht:", settings.get("screen_brief") or "(keine Beschreibung)"]
    if stats:
        lines += ["", f"Übliche Preise passender Treffer ({stats['count']}): Median {stats['median']:.0f} €, "
                      f"mittlere Hälfte {stats['p25']:.0f}–{stats['p75']:.0f} €"]
    if titles:
        lines += ["", "Titel echter Treffer (nur als Stilvorlage, nicht kopieren):", *[f"- {x}" for x in titles]]
    lines += ["", f"Sprache: {LANGS.get(background_lang(), 'Deutsch')}", f"Aufgabe: Schreibe {n} Test-Anzeigen."]
    return "\n".join(lines)


def _ad_hit(agent: SearchAgent, nr: int, ad: dict[str, Any]) -> Hit:
    """An invented ad as a hit the screening judges (never stored)."""
    price = ad.get("price")
    return Hit(agent_id=agent.id or 0, ad_id=f"test-{nr}", heading=str(ad.get("title") or "")[:200], url="",
               price=float(price) if isinstance(price, (int, float)) else None, body=str(ad.get("description") or ""),
               location=str(ad.get("location") or "") or None, is_private=bool(ad.get("private", True)),
               item_condition_label=str(ad.get("condition") or "") or None, shipping="ship" if ad.get("shipping") else "pickup",
               platform="willhaben")


async def _tests(agent_id: int, settings: dict[str, Any], n: int) -> list[dict[str, Any]]:
    from app.services import search_agents

    with session() as db:
        agent = db.get(SearchAgent, agent_id)
    if agent is None:
        raise ValueError(t("Unknown search agent"))
    sim_agent = as_agent(agent, {**settings, "screen": True})
    out = await get_backend("screen").complete(system=ADS_SYSTEM, prompt=_ads_prompt(agent, settings, n), schema=ADS_SCHEMA)
    ads = [a for a in out.data.get("ads") or [] if isinstance(a, dict) and a.get("title")][:max(n, 1)]
    if not ads:
        raise ValueError(t("Claude wrote no test ads"))
    hits = [_ad_hit(sim_agent, i, a) for i, a in enumerate(ads, 1)]
    await _judge(sim_agent, hits, False)
    results = []
    for a, h in zip(ads, hits, strict=True):
        expected = {"relevant": bool(a.get("expected_relevant")), "deal": a.get("expected_deal") or "unknown",
                    "issues": [i for i in a.get("expected_issues") or [] if i in screening.ISSUES]}
        exp_hit = _copy(h)
        exp_hit.screen_relevant, exp_hit.screen_deal = expected["relevant"], expected["deal"]
        exp_hit.screen_issues = expected["issues"]
        expected["push"] = screening.passes(sim_agent, exp_hit)
        actual = {**_verdict(h), "push": search_agents.passes(sim_agent, h)}
        no = auto_buy.why_not(sim_agent, h) if actual["push"] else t("no hit")
        actual.update(buy=bool(sim_agent.auto_buy) and no is None, buy_total=auto_buy.estimate(h), buy_why=no)
        results.append({"kind": a.get("kind"), "title": h.heading, "price": h.price, "description": h.body,
                        "condition": h.item_condition_label, "location": h.location, "private": h.is_private,
                        "shipping": bool(a.get("shipping")), "why": a.get("why"), "expected": expected, "actual": actual,
                        "ok": _agrees(expected, actual)})
    return results


def _agrees(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """The verdict counts as right when the item decision and the push are the same and, for the right item, the
    price verdict is at most one step off."""
    if bool(actual.get("relevant")) != expected["relevant"] or bool(actual.get("push")) != bool(expected.get("push")):
        return False
    if expected["relevant"]:
        rank = screening.DEAL_RANK
        return abs(rank.get(actual.get("deal") or "unknown", 1) - rank.get(expected["deal"], 1)) <= 1
    return True


def _tests_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"n": len(results), "ok": sum(1 for r in results if r["ok"]), "wrong": sum(1 for r in results if not r["ok"])}


def rate(sim_id: int, index: int, up: bool | None, note: str = "") -> None:
    with session() as db:
        row = db.get(AgentSim, sim_id)
        if row is None or not row.results or not 0 <= index < len(row.results):
            return
        ratings = dict(row.ratings or {})
        ratings[str(index)] = {"up": up, "note": note.strip()[:500]}
        row.ratings = ratings
        db.add(row)
        db.commit()


# ------------------------------------------------------------------ a better brief
BRIEF_SYSTEM = """\
Du verbesserst die Beschreibung eines Suchagenten ("Was die Person genau sucht"), nach der eine automatische Prüfung
neue Kleinanzeigen beurteilt. Du bekommst die jetzige Beschreibung und Testergebnisse: Anzeigen, bei denen die
Prüfung anders geurteilt hat als erwartet oder die die Person mit 👎 bewertet hat (mit ihrer Notiz), und solche, die
gepasst haben.

- brief: die überarbeitete Beschreibung. Behalte alles, was die Person schon festgelegt hat (Preisgrenzen,
  Pflicht-Eigenschaften, Ausschlüsse) wörtlich oder sinngleich; ergänze nur, was die Fehlurteile verhindert hätte
  (z. B. welche Varianten passen, was als Zubehör gilt, ob defekte Geräte in Frage kommen). Kurz und klar, in der Sprache
  der jetzigen Beschreibung, höchstens 600 Zeichen. Keine Regeln über das Ausgabeformat der Prüfung.
- summary: in einem Satz (max. 200 Zeichen), was du geändert hast und warum.
- Ändere nichts, wofür die Tests keinen Anlass geben; passt alles, gib die Beschreibung unverändert zurück.
"""

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"brief": {"type": "string"}, "summary": {"type": "string"}},
    "required": ["brief", "summary"],
    "additionalProperties": False,
}


def _brief_prompt(agent: SearchAgent, sim: AgentSim) -> str:
    ratings = sim.ratings or {}
    lines = [f"Suchagent: {agent.name}", "", "Jetzige Beschreibung:", (sim.settings or {}).get("screen_brief") or "(leer)", ""]
    for i, r in enumerate(sim.results or []):
        rated = ratings.get(str(i)) or {}
        if sim.kind == "tests":
            bad = not r.get("ok") or rated.get("up") is False
            exp, act = r["expected"], r["actual"]
            lines.append(f"[{'FEHLER' if bad else 'ok'}] {r['title']} · {r.get('price') or '?'} € · Art: {r.get('kind')}")
            lines.append(f"    erwartet: {'passt' if exp['relevant'] else 'passt nicht'}, {exp['deal']}, {', '.join(exp['issues']) or '-'}"
                         f" · Prüfung: {'passt' if act.get('relevant') else 'passt nicht'}, {act.get('deal')}, "
                         f"{', '.join(act.get('issues') or []) or '-'} – {act.get('reason') or ''}")
        else:
            if not r.get("changed") and rated.get("up") is not False:
                continue
            lines.append(f"[{'geändert' if r.get('changed') else 'bewertet'}] {r['title']} · {r.get('price') or '?'} € · "
                         f"echt: {'gemeldet' if r['real']['push'] else 'zurückgehalten'}, simuliert: "
                         f"{'gemeldet' if r['sim']['push'] else 'zurückgehalten'} – {r['sim'].get('reason') or ''}")
        if rated.get("up") is not None:
            lines.append(f"    Bewertung der Person: {'👍' if rated['up'] else '👎'}{(' – ' + rated['note']) if rated.get('note') else ''}")
    lines += ["", "Aufgabe: Schlage die verbesserte Beschreibung vor."]
    return "\n".join(lines)


async def propose(sim_id: int) -> dict[str, Any]:
    """Claude's revised brief from this run's mismatches and ratings: {old, new, summary, status: open}."""
    sim = get(sim_id)
    if sim is None or sim.status != "done":
        raise ValueError(t("The run is not finished yet"))
    with session() as db:
        agent = db.get(SearchAgent, sim.agent_id)
    if agent is None:
        raise ValueError(t("Unknown search agent"))
    out = await get_backend("screen").complete(system=BRIEF_SYSTEM, prompt=_brief_prompt(agent, sim), schema=BRIEF_SCHEMA)
    proposal = {"old": (sim.settings or {}).get("screen_brief") or "", "new": (out.data.get("brief") or "").strip()[:1500],
                "summary": (out.data.get("summary") or "").strip()[:300], "status": "open"}
    _save(sim_id, proposal=proposal)
    return proposal


def proposal_done(sim_id: int, status: str) -> dict[str, Any] | None:
    """The proposal applied (the agent's brief and this run's settings take it) or discarded."""
    sim = get(sim_id)
    if sim is None or not sim.proposal or sim.proposal.get("status") != "open":
        return None
    p = {**sim.proposal, "status": status}
    _save(sim_id, proposal=p)
    if status == "applied":
        with session() as db:
            agent = db.get(SearchAgent, sim.agent_id)
            if agent is not None:
                agent.screen_brief = p["new"]
                db.add(agent)
                db.commit()
    return p
