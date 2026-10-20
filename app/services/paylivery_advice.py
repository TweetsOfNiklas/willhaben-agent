"""PayLivery in the answer draft: when a chat has an open PayLivery offer (a buyer's offer in a seller's chat, a seller's
counter offer in a buyer's chat), Claude's draft also suggests what to do with it on willhaben, and the chat's
composer shows that action next to the text.

- prompt_block()/schema(): what the draft call gets on top (compose in app/services/chats.py)
- clean(): the suggestion held to the numbers (never accept below the ad's minimum price, a counter offer between
  the buyer's offer, the minimum price and the asking price)
- on_stage(): an offer came in as a willhaben notice, without a chat message: a draft is written for it

Nothing runs by itself: the owner presses the button (or "Send + counter offer").
"""
from __future__ import annotations

from typing import Any

from app.db import Conversation, MyAd
from app.i18n import t

ACTIONS = ("none", "accept", "counter", "decline")
AUTO = True              # the stage hook writes a draft by itself (tests switch it off)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Was auf willhaben mit dem offenen PayLivery-Vorschlag passieren soll",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "amount": {"type": ["number", "null"], "description": "Nur bei counter: der Betrag in Euro"},
    },
    "required": ["action", "amount"],
    "additionalProperties": False,
}


def pending(conv: Conversation | None) -> dict[str, Any] | None:
    """{"role", "offer"} while a PayLivery offer waits for this side's answer, else None."""
    st = (conv.paylivery or {}) if conv else {}
    want = {"seller": "offer_received", "buyer": "counter_received"}.get((conv.role if conv else None) or "")
    if not want or st.get("stage") != want or not st.get("offer"):
        return None
    return {"role": conv.role, "offer": float(st["offer"])}


def _eur(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f} €".replace(".", ",")


def prompt_block(p: dict[str, Any], asking: float | None, floor: float | None) -> str:
    if p["role"] == "seller":
        return (f"Offene PayLivery-Anfrage des Käufers auf willhaben: {_eur(p['offer'])} (Angebotspreis {_eur(asking)}"
                + (f", Mindestpreis {_eur(floor)}" if floor else "") + ").\n"
                "Schlag in paylivery_action vor, was der Verkäufer auf willhaben damit tun soll: accept (annehmen), "
                "counter (Gegenangebot, amount = Betrag: zwischen Anfrage und Angebotspreis, eher näher am "
                "Angebotspreis, wenn die Anfrage weit darunter liegt; nie unter dem Mindestpreis) oder decline. "
                "Die Chat-Antwort passt dazu: bei einem Gegenangebot nennt sie den Betrag und sagt, dass es über "
                "PayLivery kommt.")
    return (f"Offenes PayLivery-Gegenangebot des Verkäufers auf willhaben: {_eur(p['offer'])}.\n"
            "Schlag in paylivery_action vor: accept (annehmen, danach wird bezahlt), decline oder none. Die "
            "Chat-Antwort passt dazu.")


def schema(base: dict[str, Any]) -> dict[str, Any]:
    return {**base, "properties": {**base["properties"], "paylivery_action": SCHEMA},
            "required": [*base["required"], "paylivery_action"]}


def clean(raw: Any, p: dict[str, Any] | None, asking: float | None, floor: float | None) -> dict[str, Any] | None:
    """The suggestion as stored on the draft ({"action", "amount", "offer"}), or None for none or nothing open."""
    if not p or not isinstance(raw, dict) or raw.get("action") not in ACTIONS[1:]:
        return None
    offer, action = p["offer"], raw["action"]
    amount = raw.get("amount") if isinstance(raw.get("amount"), (int, float)) else None
    if p["role"] == "buyer":
        return {"action": action, "amount": None, "offer": offer} if action in ("accept", "decline") else None
    if action == "accept" and floor and offer < floor:
        action, amount = "counter", None
    if action == "counter":
        amount = amount or (round((offer + asking) / 2) if asking else None)
        if amount is not None:
            if floor:
                amount = max(amount, floor)
            if asking:
                amount = min(amount, asking)
            amount = round(amount * 2) / 2
        if amount is None or amount <= offer:             # nothing to counter: the offer reaches the price
            action, amount = ("accept" if asking and offer >= asking - 0.005 else "decline"), None
    return {"action": action, "amount": amount if action == "counter" else None, "offer": offer}


def for_draft(conv: Conversation, ad: MyAd | None) -> tuple[dict[str, Any] | None, float | None, float | None]:
    """(pending offer, asking price, minimum price) of a chat, for compose and clean."""
    from app.services import paylivery
    asking = paylivery.parse_amount(str((conv.ad_context or {}).get("price") or ""))
    return pending(conv), asking, (ad.min_price if ad and conv.role == "seller" else None)


def view(d: Any, conv: Conversation) -> dict[str, Any] | None:
    """The composer's line: only while the draft's suggestion belongs to the offer still open."""
    a = getattr(d, "paylivery", None) or {}
    p = pending(conv)
    if not a or not p or a.get("offer") != p["offer"]:
        return None
    label = {"accept": t("Accept offer"), "counter": t("Counter offer"), "decline": t("Decline offer")}[a["action"]]
    return {**a, "label": label, "input": a["action"] == "counter"}


async def on_stage(conv: Conversation, stage: str, state: dict[str, Any]) -> None:
    """Stage hook: an offer reached this side as a willhaben notice (no chat message): a draft is written for it."""
    if not AUTO or not pending(conv):
        return
    from app.services import chats
    chats.enqueue_draft(conv.id)
