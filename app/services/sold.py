""""Sold" / "Reserved" workflow for an own ad (page /sold/{ad_id}, reached from the chat page and My ads).

Three independent steps, each one optional and each one only on an explicit click (+ confirm in the browser):
  1. willhaben: mark the ad as sold or reserved (app/willhaben/sales.py; needs live verification), undo "reserved"
  2. the other buyers: polite "already sold" / "reserved" reply drafts for the other conversations about the ad,
     sent only via chats.send_draft after "Send selected" (+ confirm)
  3. here: automatic answers off for the ad (instructions kept) and the local sale status set
     (MyAd.sale_status; a sold ad never gets automatic answers again, see chats._auto_ad)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlmodel import col, select

from app.db import Conversation, Draft, Message, MyAd, session, utcnow
from app.i18n import t
from app.services import chats
from app.willhaben import sales
from app.willhaben.session import get_session

log = logging.getLogger(__name__)

KINDS = ("sold", "reserved")
NOTICE_INTENTS = {"sold": "sold_notice", "reserved": "reserved_notice"}

# German on purpose: these texts go to buyers (editable before sending)
NOTICE_TEXT = {
    "sold": "Hallo{name}, danke für dein Interesse! Der Artikel ist leider schon verkauft. Liebe Grüße",
    "reserved": ("Hallo{name}, danke für dein Interesse! Der Artikel ist gerade reserviert. "
                 "Falls es nicht klappt, melde ich mich bei dir. Liebe Grüße"),
}
# German on purpose: hint for Claude (chats.draft_reply) when the owner wants a written-out answer
NOTICE_HINT = {
    "sold": ("Der Artikel ist bereits an jemand anderen verkauft. Sag kurz und freundlich ab und bedank dich für das "
             "Interesse. Keine Fragen stellen, nichts anderes anbieten."),
    "reserved": ("Der Artikel ist gerade für jemand anderen reserviert. Sag das kurz und freundlich und biete an, "
                 "dich zu melden, falls es nicht klappt. Keine Fragen stellen."),
}


def kind_of(value: str | None) -> str:
    return value if value in KINDS else "sold"


def notice_text(kind: str, partner: str | None) -> str:
    name = (partner or "").strip()
    return NOTICE_TEXT[kind_of(kind)].format(name=f" {name}" if name else "")


def ad_row(ad_id: str) -> MyAd | None:
    with session() as db:
        return db.get(MyAd, ad_id)


def seller_conversations(ad_id: str) -> list[Conversation]:
    with session() as db:
        return list(db.exec(select(Conversation).where(Conversation.ad_id == ad_id, Conversation.role == "seller")
                            .order_by(col(Conversation.last_message_at).desc().nulls_last())))


def title_of(ad_id: str) -> str:
    ad = ad_row(ad_id)
    if ad and ad.heading:
        return ad.heading
    conv = next(iter(seller_conversations(ad_id)), None)
    return (conv.ad_title if conv else None) or ad_id


@dataclass
class Other:
    conv: Conversation
    last_in: str | None          # the buyer's latest message
    informed: bool               # a sold/reserved notice was already sent in this chat
    draft: Draft | None          # open notice draft, if any
    sending: Draft | None = None # notice queued / being sent right now


def others(ad_id: str, buyer: str | None) -> list[Other]:
    """The other buyers' conversations about this ad (everyone except the buyer it was sold/reserved to)."""
    out = []
    with session() as db:
        for conv in seller_conversations(ad_id):
            if conv.id == buyer:
                continue
            msgs = list(db.exec(select(Message).where(Message.conversation_id == conv.id, Message.direction == "in")
                                .order_by(col(Message.created_at).desc().nulls_last()).limit(1)))
            notices = list(db.exec(select(Draft).where(Draft.conversation_id == conv.id,
                                                       col(Draft.intent).in_(list(NOTICE_INTENTS.values())))
                                   .order_by(col(Draft.created_at).desc())))
            out.append(Other(conv=conv, last_in=msgs[0].text if msgs else conv.last_message_text,
                             informed=any(d.status == "sent" for d in notices),
                             draft=next((d for d in notices if d.status == "draft"), None),
                             sending=next((d for d in notices if d.status == "sending"), None)))
    return out


# ------------------------------------------------------------------ step 1: willhaben (writes, after confirm)
async def on_willhaben(ad_id: str, kind: str, *, undo: bool = False, reason_id: str | None = None) -> str:
    """Mark sold / reserved (or remove "reserved") on willhaben. Raises sales.ReasonRequired, NotOffered, ..."""
    s = get_session()
    if kind_of(kind) == "sold":
        if undo:
            raise sales.NotOffered(t('willhaben cannot undo "sold".'))
        await sales.mark_sold(s, ad_id, reason_id)
        status, msg = "sold", t("Marked as sold on willhaben")
    else:
        await sales.mark_reserved(s, ad_id, on=not undo)
        status, msg = ("active", t("Reservation removed on willhaben")) if undo else ("reserved", t("Marked as reserved on willhaben"))
    with session() as db:
        row = db.get(MyAd, ad_id) or MyAd(ad_id=ad_id, heading=title_of(ad_id))
        row.status, row.last_action, row.last_error = status, msg, None
        if status == "sold":
            row.auto_renew = False    # sold: auto-renew must never reactivate or republish it
        db.add(row)
        db.commit()
    return msg


def enqueue_willhaben(ad_id: str, kind: str, *, undo: bool = False, reason_id: str | None = None) -> bool:
    """Step 1 as a background job (lane ads). When willhaben asks for a sold reason first, nothing is written: the
    reasons wait on the ad (pending_payload) and the page offers them; the next click queues it again.
    False: the ad already waits for another action."""
    from app.services import jobs

    kind = kind_of(kind)
    action = "sold" if kind == "sold" else ("unreserve" if undo else "reserved")
    with session() as db:
        row = db.get(MyAd, ad_id) or MyAd(ad_id=ad_id, heading=title_of(ad_id))
        if row.pending_action:
            return False
        unsaved = (row.pending_payload or {}).get("unsaved")      # an edit text waiting to be saved again stays
        row.pending_action, row.pending_payload, row.last_error = action, ({"unsaved": unsaved} if unsaved else None), None
        db.add(row)
        db.commit()

    def unmark(error: str | None = None, payload: dict[str, Any] | None = None) -> None:
        with session() as db:
            r = db.get(MyAd, ad_id)
            unsaved = (r.pending_payload or {}).get("unsaved")
            r.pending_action, r.pending_payload = None, ({**(payload or {}), "unsaved": unsaved} if unsaved else payload)
            if error is not None:
                r.last_error = error
            db.add(r)
            db.commit()

    async def run(job: jobs.Job) -> str:
        try:
            msg = await on_willhaben(ad_id, kind, undo=undo, reason_id=reason_id)
        except sales.ReasonRequired as e:     # nothing written: the user picks willhaben's reason, then confirms again
            unmark(t("willhaben asks why the ad is sold. Pick a reason."),
                   {"reasons": [{"id": r.id, "label": r.label} for r in e.reasons]})
            return t("willhaben asks why the ad is sold. Pick a reason.")
        except Exception as e:
            unmark(t("Not changed on willhaben: {error}", error=e))
            raise
        unmark()
        return msg

    label = {"sold": t("Mark „{title}“ as sold on willhaben", title=title_of(ad_id)[:60]),
             "reserved": t("Mark „{title}“ as reserved on willhaben", title=title_of(ad_id)[:60]),
             "unreserve": t("Remove the reservation of „{title}“ on willhaben", title=title_of(ad_id)[:60])}[action]
    jobs.submit(f"sold-{action}", label, run, current=f"ad:{ad_id}")
    return True


def pending_reasons(ad_id: str) -> list[sales.SoldReason] | None:
    """Sold reasons a background step left behind (willhaben asked for one), for the reason picker."""
    with session() as db:
        row = db.get(MyAd, ad_id)
    raw = ((row.pending_payload or {}) if row else {}).get("reasons")
    return [sales.SoldReason(id=str(r["id"]), label=str(r["label"])) for r in raw] if raw else None


# ------------------------------------------------------------------ step 2: notices to the other buyers
async def prepare_notices(ad_id: str, kind: str, conversation_ids: list[str], *, buyer: str | None = None,
                          use_claude: bool = False) -> tuple[int, list[str]]:
    """One notice draft per chosen conversation (replacing an unsent one). Nothing is sent here."""
    kind = kind_of(kind)
    allowed = {o.conv.id: o for o in others(ad_id, buyer)}
    made, errors = 0, []
    for cid in conversation_ids:
        o = allowed.get(cid)
        if o is None:
            continue                     # not a conversation about this ad (or the buyer's own)
        if o.draft is not None:
            chats.discard_draft(o.draft.id)
        if use_claude:
            try:
                d = await chats.draft_reply(cid, hint=NOTICE_HINT[kind])
            except Exception as e:  # noqa: BLE001 - one failed draft must not stop the others
                errors.append(f"{o.conv.partner_name or cid}: {e}")
                continue
            with session() as db:
                row = db.get(Draft, d.id)
                row.intent = NOTICE_INTENTS[kind]
                db.add(row)
                db.commit()
        else:
            with session() as db:
                db.add(Draft(conversation_id=cid, text=notice_text(kind, o.conv.partner_name),
                             intent=NOTICE_INTENTS[kind], model="template"))
                db.commit()
        made += 1
    return made, errors


def enqueue_notice_drafts(ad_id: str, kind: str, conversation_ids: list[str], *, buyer: str | None = None) -> int:
    """"Prepare drafts with Claude" as a background job (lane claude, 10–60 s per chat): the chats show "Claude is
    writing…" until their draft is there. Nothing is sent. Returns how many chats were queued."""
    from app.services import jobs

    allowed = {o.conv.id for o in others(ad_id, buyer)}
    picked = [c for c in dict.fromkeys(conversation_ids) if c in allowed]
    with session() as db:
        picked = [c for c in picked if not (db.get(Conversation, c) or Conversation(id=c)).drafting]
    for cid in picked:
        chats._set_drafting(cid, True)
    if not picked:
        return 0

    async def run(job: jobs.Job) -> str:
        made, errors = 0, []
        try:
            for i, cid in enumerate(picked):
                job.progress(i, t("Draft {i} of {n}", i=i + 1, n=len(picked)), f"drafting:{cid}")
                m, err = await prepare_notices(ad_id, kind, [cid], buyer=buyer, use_claude=True)
                made += m
                errors += err
                chats._set_drafting(cid, False, err[0] if err else None)
        finally:
            for cid in picked:
                with session() as db:
                    c = db.get(Conversation, cid)
                    still = bool(c and c.drafting)
                if still:
                    chats._set_drafting(cid, False)
        job.problems = len(errors)
        return t("{n} drafts prepared – nothing sent yet", n=made)

    jobs.submit("notice-drafts", t("Claude writes {n} notices", n=len(picked)), run, lane="claude", total=len(picked))
    return len(picked)


def send_notices(ad_id: str, items: list[tuple[int, str]]) -> int:
    """Queue the chosen notice drafts on the normal send path (chats.enqueue_send): one background job per chat,
    sent one after another; a failure goes back to that draft with its reason. Returns how many were queued."""
    convs = {c.id for c in seller_conversations(ad_id)}
    queued = 0
    for draft_id, text in items:
        with session() as db:
            d = db.get(Draft, draft_id)
        if d is None or d.status != "draft" or d.conversation_id not in convs or d.intent not in NOTICE_INTENTS.values():
            continue
        queued += chats.enqueue_send(draft_id, text)
    return queued


def discard_notices(ad_id: str, draft_ids: list[int]) -> int:
    convs = {c.id for c in seller_conversations(ad_id)}
    n = 0
    for draft_id in draft_ids:
        with session() as db:
            d = db.get(Draft, draft_id)
        if d and d.status == "draft" and d.conversation_id in convs and d.intent in NOTICE_INTENTS.values():
            chats.discard_draft(draft_id)
            n += 1
    return n


# ------------------------------------------------------------------ step 3: local state
def finish(ad_id: str, kind: str, buyer: str | None, *, auto_off: bool = True) -> MyAd:
    """Automatic answers off (instructions kept) and the sale status on the ad."""
    kind = kind_of(kind)
    with session() as db:
        row = db.get(MyAd, ad_id) or MyAd(ad_id=ad_id, heading=title_of(ad_id))
        if auto_off:
            row.auto_reply = False
        row.sale_status, row.sale_conversation_id, row.sale_at = kind, buyer or None, utcnow()
        if kind == "sold":
            row.auto_renew = False    # sold: auto-renew must never reactivate or republish it
        row.last_action = t("Sold") if kind == "sold" else t("Reserved")
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def reset(ad_id: str) -> None:
    """The deal fell through: forget the local sale status (willhaben is not touched)."""
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.sale_status = row.sale_conversation_id = row.sale_at = None
            db.add(row)
            db.commit()


def state_for(conv: Conversation) -> dict[str, Any] | None:
    """Sale info for the chat page: kind, and whether this chat is the buyer's."""
    if not conv.ad_id:
        return None
    ad = ad_row(conv.ad_id)
    if ad is None or not (ad.sale_status or ad.status in KINDS):
        return None
    kind = ad.sale_status or ad.status
    return {"kind": kind, "buyer": ad.sale_conversation_id, "is_buyer": ad.sale_conversation_id == conv.id}
