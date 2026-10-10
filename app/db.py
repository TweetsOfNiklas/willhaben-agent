"""SQLite persistence (SQLModel)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine, select

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SearchAgent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    source_url: str
    webapi_url: str
    interval_s: int = 300
    enabled: bool = True
    notify: bool = True
    notify_price_drops: bool = True
    baseline_done: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    last_run_at: datetime | None = None
    last_ok_at: datetime | None = None
    last_error: str | None = None
    last_rows_found: int | None = None
    screen: bool | None = None          # let Claude check new hits before they push
    screen_brief: str | None = None     # what exactly the owner is looking for (for the screening)
    screen_min: str | None = None       # match | fair | good: lowest result that still pushes
    screen_hide: str | None = None      # comma list of screening issues that keep a hit from pushing; None = defaults, "-" = none
    # created from a product search: several keyword searches per check (source_url/webapi_url mirror the first)
    # [{keyword, webapi_url, source_url, price_from, price_to, category_path, why}]
    queries: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    plan: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # {max_price, must_have, nice_to_have, exclude, summary}
    search_id: int | None = None        # origin ProductSearch


class Hit(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("agent_id", "ad_id"),)
    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="searchagent.id", index=True)
    ad_id: str = Field(index=True)
    heading: str
    body: str | None = None
    price: float | None = None
    price_display: str | None = None
    old_price: float | None = None
    location: str | None = None
    postcode: str | None = None
    published_at: datetime | None = None
    end_date: datetime | None = None
    url: str
    image_url: str | None = None
    is_private: bool | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    state: str = "new"  # new | seen | saved | ignored
    price_dropped_at: datetime | None = None
    bumped_count: int = 0
    screen_relevant: bool | None = None  # Claude: the item searched for? None = not screened
    screen_deal: str | None = None       # great | good | fair | overpriced | unknown
    screen_reason: str | None = None
    screen_issues: list[str] | None = Field(default=None, sa_column=Column(JSON))  # predefined reasons (screening.ISSUES)
    screen_at: datetime | None = None
    scam_flags: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # app/services/scam.py


class Conversation(SQLModel, table=True):
    id: str = Field(primary_key=True)
    ad_id: str | None = Field(default=None, index=True)
    ad_title: str | None = None
    partner_name: str | None = None
    unread_count: int = 0
    last_message_text: str | None = None
    last_message_at: datetime | None = None
    role: str | None = None  # seller | buyer
    ad_context: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    synced_at: datetime | None = None
    messages_synced_at: datetime | None = None
    # appointment agreed in this chat: {start (local ISO), minutes, location, summary, event_id?, event_link?}
    meetup: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # PayLivery process of this chat (app/services/paylivery.py): role, stage, amounts, deadline, history, applied notices
    paylivery: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # hidden from the chat list (only here, not on willhaben) until the other person writes again
    hidden_at: datetime | None = None
    removed: bool | None = None    # also hidden, and its messages/drafts deleted here; the row stays so sync skips it


class Message(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("conversation_id", "ext_id"),)
    id: int | None = Field(default=None, primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    ext_id: str
    direction: str  # in | out | unknown
    text: str
    created_at: datetime | None = None
    kind: str | None = None  # system | integration: willhaben's own notice (see app/services/paylivery.py); None = normal
    params: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # willhaben's structured `parameters`


class Draft(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    text: str
    intent: str | None = None
    needs_info: list[str] | None = Field(default=None, sa_column=Column(JSON))
    suggested_price: float | None = None
    model: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    status: str = "draft"  # draft | pending (auto reply "typing") | sending (queued send job) | sent | discarded
    sent_at: datetime | None = None
    error: str | None = None
    auto: bool | None = None          # created by the auto-reply job
    auto_reason: str | None = None    # why it was / was not sent automatically
    feedback: str | None = None       # up | down, from Telegram buttons
    feedback_note: str | None = None  # what was wrong (goes into later automatic answers)
    meetup: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # appointment this answer fixes
    purchase: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # buyer side: Claude's purchase_state


class MyAd(SQLModel, table=True):
    ad_id: str = Field(primary_key=True)
    heading: str | None = None
    url: str | None = None
    image_url: str | None = None
    status: str = "unknown"  # active | expired | sold | reserved | deleted (gone from the account) | label | unknown
    published_at: datetime | None = None
    end_date: datetime | None = None
    auto_renew: bool = False
    notified_expiry_for: datetime | None = None
    last_checked_at: datetime | None = None
    last_action: str | None = None
    last_error: str | None = None
    auto_reply: bool | None = None                 # answer buyers of this ad automatically
    auto_reply_instructions: str | None = None     # ad-specific facts/limits for those answers
    min_price: float | None = None                 # floor for automatic answers: never agree below this (€)
    replaced_by: str | None = None    # republished (auto-renew) under this new ad id; old chats still point here
    parcel: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # confirmed size/weight for shipping
    hidden: bool | None = None        # hidden from the list by the user; the import keeps it hidden
    sale_status: str | None = None                 # sold | reserved, set by the sold workflow (/sold/{ad_id}); sticky locally
    sale_conversation_id: str | None = None        # the buyer's conversation
    sale_at: datetime | None = None
    pending_action: str | None = None              # activate | deactivate | delete: queued as a background job


class Shipment(SQLModel, table=True):
    """A parcel to a buyer: address from the chat, size/weight, chosen carrier option, label."""
    id: int | None = Field(default=None, primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    ad_id: str | None = None
    # draft (address/parcel being prepared, not paid) | awaiting (paid, needs the user's go) | buying (label is being
    # bought in the browser) | labelled | printed | sent | failed (buying/printing broke, see error) | discarded
    status: str = "draft"
    recipient: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # name, street, house_no, extra, zip, city, phone, email
    parcel: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))     # length/width/height cm, weight_kg, contents, source
    option: str | None = None        # tariff key, e.g. "gls:S:shop"
    price: float | None = None       # published tariff price of the option
    label_path: str | None = None    # file in DATA_DIR/labels
    tracking: str | None = None
    note: str | None = None          # Claude's remark (missing data, oddities)
    # --- payment (the label is only bought once the buyer has paid) ---
    agreed_price: float | None = None          # price agreed in the chat (Claude reads it with the address)
    paid_at: datetime | None = None
    paid_via: str | None = None                # manual | paylivery | email | bank
    paid_amount: float | None = None           # amount seen by an automatic source (None for manual)
    payment_claimed_at: datetime | None = None # the buyer wrote "I have paid" (a hint, never a status)
    # --- approval and purchase ---
    approval: str | None = None                # pending | approved | auto | declined (buy_only: older rows)
    approved_at: datetime | None = None
    buy_log: list[Any] | None = Field(default=None, sa_column=Column(JSON))   # checkout steps [{step, url, action, note}]
    paid_price: float | None = None            # what the carrier's checkout actually charged
    error: str | None = None                   # last failure of buying/printing (status failed) or what the user must do
    # --- printing ---
    print_job: str | None = None               # printer job id/uri
    printed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AdDraft(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    input_text: str
    image_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    title: str | None = None
    description: str | None = None
    category_hint: str | None = None
    condition: str | None = None
    attributes: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    search_keywords: str | None = None
    price_suggestion: float | None = None
    price_stats: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    comparables: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    model: str | None = None
    product_query: str | None = None   # exact product name Claude recognised (for Geizhals)
    geizhals: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))       # match + specs + candidates
    revisions: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # earlier versions, newest last
    wh_plan: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # willhaben category/fields to confirm
    wh_ad_id: str | None = None        # willhaben ad id once the insertion form created it (draft or live)
    status: str = "draft"  # draft | published | discarded
    error: str | None = None
    batch_id: int | None = None   # AdBatch this draft came from
    publishing: bool | None = None     # queued/running as a background publish job
    created_at: datetime = Field(default_factory=utcnow)


class AdBatch(SQLModel, table=True):
    """Photo dump of many articles: Claude sorts the photos into products, the user corrects, then one AdDraft per group."""
    id: int | None = Field(default=None, primary_key=True)
    notes: str = ""                     # applies to every article (pickup, origin, ...)
    image_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # [{id, photos: [image index], product, product_query, observations, confidence, question, comment, include,
    #   draft_id, error}]; images in no group are unused
    groups: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = "sorting"  # sorting | review | creating | error
    error: str | None = None
    note: str | None = None             # Claude's remark on the sorting
    model: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class DealCheck(SQLModel, table=True):
    """Price check of one ad (Produktsuche tab / ad viewer): last analysis and Claude's verdict, one row per ad."""
    ad_id: str = Field(primary_key=True)
    title: str | None = None
    price: float | None = None
    price_display: str | None = None
    image_url: str | None = None
    url: str | None = None
    query: str | None = None
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # deals.analyze() output
    claude: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # {deal, reason, model, at}
    checked_at: datetime | None = Field(default_factory=utcnow, index=True)


class ProductSearch(SQLModel, table=True):
    """Claude product search (Produktsuche tab): description -> planned willhaben searches -> ranked offers."""
    id: int | None = Field(default=None, primary_key=True)
    description: str = ""
    settings: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # {depth, unusual, categories, sort, photos, ask}
    status: str = "planning"            # planning | searching | asking | ranking | photos | done | error
    plan: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))       # summary, max_price, criteria, queries, questions
    answers: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))    # {question id: answer}; {} = skipped
    cards: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # candidates while "asking"
    queries: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # per query: found, error
    results: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # ranked offers
    progress: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # {done, total}
    requests: int | None = None         # willhaben searches this run sent
    candidates: int | None = None
    note: str | None = None             # Claude's overall remark
    warning: str | None = None          # e.g. stopped early after a willhaben block
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Purchase(SQLModel, table=True):
    """A purchase order (Kaufauftrag, app/services/purchase.py): the agent negotiates as buyer until the user pays."""
    id: int | None = Field(default=None, primary_key=True)
    ad_id: str = Field(index=True)
    ad_title: str | None = None
    ad_url: str | None = None
    image_url: str | None = None
    asking_price: float | None = None      # price of the ad when the order started
    median_price: float | None = None      # median of comparable ads (deals.analyze), if there are enough
    max_price: float | None = None         # code-enforced ceiling incl. shipping; default = asking price
    mode: str = "paylivery"                # paylivery | direct | pickup
    pickup_window: str | None = None       # pickup: the user's preferred time window (free text)
    pickup: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # pickup: {start, minutes, location}
    status: str = "new"                    # see app/services/purchase.py STATUSES
    conversation_id: str | None = Field(default=None, index=True)
    agreed_price: float | None = None      # item price (without shipping)
    shipping_cost: float | None = None     # shipping in euro; the effective value is purchase.shipping_of(row)
    shipping_source: str | None = None     # "ad" (PayLivery price of the ad) | "chat" (named by the seller); else estimate
    agreed_total: float | None = None      # agreed item price + shipping (or the agreed total of the seller)
    market: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # {median, min, p25, count} at start
    payment: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # direct: {iban, holder, amount, reference}
    note: str | None = None                # last remark of Claude, or why it is paused / failed
    log: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # [{at, status, text}], newest last
    search_id: int | None = None           # ProductSearch the order came from
    query: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TelegramAction(SQLModel, table=True):
    """What a Telegram button does: callback_data is `t:<token>` (Telegram allows only 64 bytes)."""
    token: str = Field(primary_key=True)
    verb: str
    ref: str
    created_at: datetime = Field(default_factory=utcnow, index=True)


class TelegramMessage(SQLModel, table=True):
    """Telegram message sent for a notification tag, so a later notification with that tag edits it."""
    tag: str = Field(primary_key=True)
    chat_id: int
    message_id: int
    photo: bool = False


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


class LLMCall(SQLModel, table=True):
    """One Claude call (one row per model it used), for the token usage table in Settings → Claude."""
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    purpose: str = Field(index=True)          # chat | ads | screen | other
    model: str
    ok: bool = True
    call_part: int | None = None              # 0 = first model of the call (counts as the call), 1.. = further models
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None             # the CLI's estimate; the subscription is not billed per token
    duration_ms: int | None = None
    error: str | None = None


class AccessCount(SQLModel, table=True):
    """willhaben requests per minute (app/willhaben/access_log.py): aggregated counters only, no paths, ids or texts."""
    minute: int = Field(primary_key=True)     # UTC epoch minute (unix time // 60)
    channel: str = Field(primary_key=True)    # anonymous | account | chat
    kind: str = Field(primary_key=True)       # access_log.KINDS
    outcome: str = Field(primary_key=True)    # ok | client_error | blocked | error | cached
    count: int = 0


class EventLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    kind: str
    message: str
    url: str | None = None


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_engine(f"sqlite:///{s.db_path}", connect_args={"check_same_thread": False})
    return _engine


def set_engine(engine) -> None:
    """Used by tests to inject an in-memory database."""
    global _engine
    _engine = engine


def init_db() -> None:
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine) -> None:
    """Tiny forward-only migration: new nullable model fields become columns of existing tables."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col_ in table.columns:
                if col_.name not in existing and col_.nullable:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col_.name}" '
                                      f'{col_.type.compile(engine.dialect)}'))


def session() -> Session:
    return Session(get_engine(), expire_on_commit=False)


def log_event(kind: str, message: str, url: str | None = None) -> None:
    with session() as db:
        db.add(EventLog(kind=kind, message=message, url=url))
        db.commit()


DEFAULT_SETTINGS: dict[str, str] = {
    "reply_rules": (
        "- Ich verkaufe privat, keine Garantie oder Rücknahme.\n"
        "- Abholung bevorzugt, Versand nur gegen Vorauskasse oder mit willhaben PayLivery.\n"
        "- Bei Preisverhandlung höchstens 10 % Nachlass, sonst freundlich ablehnen.\n"
        "- Du-Form, freundlich, kurz, keine Emojis."
    ),
    "auto_draft": "0",
    "notify_messages": "1",
    "chat_sync_limit": "10",    # sync only the N latest conversations
    "chat_sync_since": "",      # YYYY-MM-DD: skip conversations whose last message is older; empty = no cutoff
    "expiry_warn_days": "3",
    "myads_expired_days": "30",
    "claude_token_limit_m": "10",   # Settings → Claude: monthly token limit in millions (sidebar meter); empty/0 = none
    "claude_limit_mode": "notify",  # My ads: expired ads older than this are listed with the inactive ones; 0 = always shown
    "auto_reply_rules": (
        "- Automatisch nur einfache Fragen beantworten: ob der Artikel noch da ist, Abholung/Versand, Zustand laut Inserat.\n"
        "- Keine Preiszusagen und kein Handeln: bei Preisfragen auf den Inseratspreis verweisen oder nicht automatisch antworten.\n"
        "- Keine Termine, Adressen oder Telefonnummern zusagen, die nicht in der Anweisung zur Anzeige stehen."
    ),
    "send_verified": "0",
    "reply_humanize": "1",      # auto replies: read receipt + randomized delay (app/services/humanize.py)
    "reply_delay_min_s": "40",
    "reply_delay_max_s": "180",
    "reply_fast_min_s": "5",    # ongoing chats (I answered before): reply within seconds
    "reply_fast_max_s": "20",
    "ui_lang": "auto",          # "auto" (browser), "en" or "de"; see app/i18n
    "ui_lang_seen": "",         # last language a browser asked for, used for pushes when ui_lang is auto
    # quiet hours for search agents (app/services/quiet_hours.py)
    "quiet_enabled": "1",
    "quiet_start": "01:00",
    "quiet_end": "07:00",
    "quiet_mode": "slow",
    "quiet_interval_min": "60",
}


def current_ad(db: Session, ad_id: str) -> MyAd | None:
    """The own ad, following renewals: chats keep the old ad id, the settings live on the newest copy."""
    ad, seen = db.get(MyAd, ad_id), {ad_id}
    while ad is not None and ad.replaced_by and ad.replaced_by not in seen:
        newer = db.get(MyAd, ad.replaced_by)
        if newer is None:
            break    # new copy not imported yet: keep the old row
        seen.add(newer.ad_id)
        ad = newer
    return ad


def requests_per_hour() -> int:
    """The hourly willhaben request budget: set in Settings → willhaben, else REQUESTS_PER_HOUR from .env (0 = none)."""
    raw = get_setting("requests_per_hour")
    return int(raw) if raw.isdigit() else get_settings().requests_per_hour


def get_setting(key: str) -> str:
    with session() as db:
        row = db.get(Setting, key)
        return row.value if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(key: str, value: str) -> None:
    with session() as db:
        row = db.get(Setting, key)
        if row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))
        db.commit()


def recent_events(limit: int = 30) -> list[EventLog]:
    with session() as db:
        return list(db.exec(select(EventLog).order_by(EventLog.ts.desc()).limit(limit)))


def iter_all(model) -> Iterator[Any]:
    with session() as db:
        yield from db.exec(select(model))
