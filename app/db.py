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
    screen_at: datetime | None = None


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


class Message(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("conversation_id", "ext_id"),)
    id: int | None = Field(default=None, primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    ext_id: str
    direction: str  # in | out | unknown
    text: str
    created_at: datetime | None = None


class Draft(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    text: str
    intent: str | None = None
    needs_info: list[str] | None = Field(default=None, sa_column=Column(JSON))
    suggested_price: float | None = None
    model: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    status: str = "draft"  # draft | sent | discarded
    sent_at: datetime | None = None
    error: str | None = None
    auto: bool | None = None          # created by the auto-reply job
    auto_reason: str | None = None    # why it was / was not sent automatically
    feedback: str | None = None       # up | down, from Telegram buttons
    feedback_note: str | None = None  # what was wrong (goes into later automatic answers)
    meetup: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # appointment this answer fixes


class MyAd(SQLModel, table=True):
    ad_id: str = Field(primary_key=True)
    heading: str | None = None
    url: str | None = None
    image_url: str | None = None
    status: str = "unknown"  # active | expired | unknown
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


class Shipment(SQLModel, table=True):
    """A parcel to a buyer: address from the chat, size/weight, chosen carrier option, label."""
    id: int | None = Field(default=None, primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    ad_id: str | None = None
    status: str = "draft"            # draft | labelled | sent | discarded
    recipient: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # name, street, house_no, extra, zip, city, phone, email
    parcel: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))     # length/width/height cm, weight_kg, contents, source
    option: str | None = None        # tariff key, e.g. "gls:S:shop"
    price: float | None = None
    label_path: str | None = None    # file in DATA_DIR/labels
    tracking: str | None = None
    note: str | None = None          # Claude's remark (missing data, oddities)
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
    "expiry_warn_days": "3",
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
