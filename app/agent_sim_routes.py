"""The search agent simulator's pages (/agent-sim, app/services/agent_sim.py): pick an agent, edit a copy of its judging
settings, replay its recent hits or let Claude invent test ads, rate the verdicts, take a better brief or the
settings over. Installed from app/main.py."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import col, select

from app.db import SearchAgent, session
from app.i18n import t
from app.llm.base import LLMError
from app.services import agent_sim, ai_rules, screening, search_agents


def _back(sim_id: int | None = None, msg: str = "", agent_id: int | None = None) -> RedirectResponse:
    q = [f"id={sim_id}"] if sim_id else ([f"agent={agent_id}"] if agent_id else [])
    if msg:
        q.append(f"msg={quote(msg, safe='')}")
    return RedirectResponse("/agent-sim" + (f"?{'&'.join(q)}" if q else ""), status_code=303)


def install(app: FastAPI, templates: Any) -> None:
    from app.main import render

    @app.get("/agent-sim", response_class=HTMLResponse)
    async def agent_sim_page(request: Request, id: int | None = None, agent: int | None = None, msg: str | None = None,
                             changes: bool = False):
        sim = agent_sim.get(id) if id else None
        with session() as db:
            agents = list(db.exec(select(SearchAgent).order_by(col(SearchAgent.name))))
        by_id = {a.id: a for a in agents}
        cur = by_id.get(sim.agent_id if sim else agent) or (agents[0] if agents else None)
        settings = (sim.settings if sim else None) or (agent_sim.settings_of(cur) if cur else {})
        proposal = sim.proposal if sim and sim.proposal else None
        return render(request, "agent_sim.html", msg=msg, sim=sim, agents=agents, agent=cur, settings=settings,
                      runs=agent_sim.runs(), agent_names={a.id: a.name for a in agents},
                      changed=agent_sim.changed_settings(cur, settings) if cur and sim else [],
                      thresholds=screening.thresholds(), issue_labels=screening.issue_labels(),
                      hideable=screening.HIDEABLE, levels=search_agents.level_choices(), deal_label=screening.deal_label,
                      scopes=agent_sim.SCOPES, test_counts=agent_sim.TEST_COUNTS, only_changes=changes,
                      terms=agent_sim.terms_of(cur) if cur else [],
                      brief_diff=ai_rules.diff(proposal["old"], proposal["new"]) if proposal else [],
                      kind_labels=kind_labels())

    @app.post("/agent-sim/run")
    async def agent_sim_run(request: Request):
        """Replay or invented ads, with the settings as edited in the form."""
        form = await request.form()
        try:
            agent_id = int(str(form.get("agent_id") or "0"))
        except ValueError:
            raise HTTPException(400) from None
        kind = "tests" if form.get("kind") == "tests" else "replay"
        settings = agent_sim.parse_settings(form)
        if kind == "tests":
            n = int(str(form.get("tests_n") or "10")) if str(form.get("tests_n") or "").isdigit() else 10
            scope = {"n": n if n in agent_sim.TEST_COUNTS else 10}
        else:
            n = int(str(form.get("n") or "50")) if str(form.get("n") or "").isdigit() else 50
            only = str(form.get("only") or "all")
            scope = {"n": n if n in agent_sim.SCOPES else 50, "only": only if only in ("all", "pushed", "held") else "all"}
        if not settings["screen"] and kind == "tests":
            return _back(msg=t("Test ads need Claude's screening – switch it on above"), agent_id=agent_id)
        try:
            sim = agent_sim.start(agent_id, kind, settings, scope)
        except ValueError as e:
            return _back(msg=str(e), agent_id=agent_id)
        return _back(sim.id)

    @app.post("/agent-sim/{sim_id}/again")
    async def agent_sim_again(sim_id: int):
        """The same run once more (Claude may judge differently; the hits may have changed)."""
        sim = agent_sim.get(sim_id)
        if sim is None:
            raise HTTPException(404)
        new = agent_sim.start(sim.agent_id, sim.kind, sim.settings or {}, sim.scope or {})
        return _back(new.id)

    @app.post("/agent-sim/{sim_id}/rate/{index}")
    async def agent_sim_rate(sim_id: int, index: int, up: str = Form(""), note: str = Form("")):
        agent_sim.rate(sim_id, index, {"1": True, "0": False}.get(up), note)
        return RedirectResponse(f"/agent-sim?id={sim_id}#r{index}", status_code=303)

    @app.post("/agent-sim/{sim_id}/propose")
    async def agent_sim_propose(sim_id: int):
        """Claude's better brief from the mismatches and ratings (a few seconds; shown on the page)."""
        try:
            await agent_sim.propose(sim_id)
        except (LLMError, ValueError) as e:
            return _back(sim_id, t("Claude could not propose a brief: {error}", error=e))
        return RedirectResponse(f"/agent-sim?id={sim_id}#proposal", status_code=303)

    @app.post("/agent-sim/{sim_id}/proposal/{action}")
    async def agent_sim_proposal(sim_id: int, action: str):
        if action not in ("apply", "discard"):
            raise HTTPException(404)
        p = agent_sim.proposal_done(sim_id, "applied" if action == "apply" else "discarded")
        msg = (t("The agent's brief is updated") if action == "apply" else t("Proposal discarded")) if p else ""
        return _back(sim_id, msg)

    @app.post("/agent-sim/{sim_id}/apply")
    async def agent_sim_apply(sim_id: int):
        """The simulated settings become the agent's own."""
        sim = agent_sim.get(sim_id)
        if sim is None:
            raise HTTPException(404)
        agent_sim.apply_settings(sim.agent_id, sim.settings or {})
        return _back(sim_id, t("The agent now judges with these settings"))

    @app.post("/agent-sim/{sim_id}/delete")
    async def agent_sim_delete(sim_id: int):
        sim = agent_sim.get(sim_id)
        agent_sim.delete(sim_id)
        return _back(agent_id=sim.agent_id if sim else None)


def kind_labels() -> dict[str, str]:
    """What an invented ad tests, for its card."""
    return {"bargain": t("bargain"), "fair_price": t("fair price"), "overpriced": t("too expensive"),
            "wrong_product": t("wrong product"), "accessory_only": t("accessory / spare part only"),
            "wrong_variant": t("wrong size or variant"), "defective": t("defective / damaged"),
            "search_or_swap": t("search or swap ad"), "scam_suspect": t("suspected scam"),
            "misses_requirement": t("misses a requirement"), "bundle_only": t("only as a bundle")}
