"""LLM backend interface. The app only ever asks for one structured JSON object."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: dict[str, Any]
    model: str
    duration_ms: int | None = None
    cost_usd: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# Outcome of the most recent LLM call, read by the connection watchdog (app/services/health.py).
last_call: dict[str, Any] = {"ok": None, "at": None, "error": None}


def record_call(ok: bool, error: str | None = None) -> None:
    from app.db import utcnow

    last_call.update(ok=ok, at=utcnow(), error=None if ok else (error or "")[:300])


def _(text: str) -> str:
    """Marks a label for translation; it is translated where it is shown (settings page)."""
    return text


# Selectable in Settings. Full ids on purpose: in CLI 2.1.286 the alias "haiku" still means Haiku 4.5.
MODELS = [
    ("claude-opus-5-5", "Opus 5.5", _("best quality for replies and listings")),
    ("claude-fable-5-1", "Fable 5.1", _("newest model")),
    ("claude-sonnet-5-5", "Sonnet 5.5", _("faster, a little less thorough")),
    ("claude-haiku-5-5", "Haiku 5.5", _("fastest, for simple replies")),
]


# What a model is used for -> settings key. Unset keys fall back to PURPOSE_DEFAULTS, then llm_model / LLM_MODEL.
PURPOSES = {
    "chat": ("llm_model_chat", _("Chat replies"), _("reply drafts for willhaben messages")),
    "ads": ("llm_model_ads", _("Creating listings"), _("title, description and category from notes and photos")),
    "screen": ("llm_model_screen", _("Screening search hits"), _("right item? good deal? – runs for every new hit, a fast model is enough")),
    "search": ("llm_model_search", _("Product search"), _("plans willhaben searches from your description and picks the best offers – two or three calls per search")),
    "shipping": ("llm_model_shipping", _("Shipping & labels"), _("reads address and parcel size from chats, fills and buys carrier labels")),
    "purchase": ("llm_model_purchase", _("Purchases & PayLivery"), _("opening message and negotiation as buyer, PayLivery steps")),
}


# Opus only where quality matters most (writing listings); everything else runs on Sonnet by default.
PURPOSE_DEFAULTS = {
    "ads": "claude-opus-5-5",
    "chat": "claude-sonnet-5-5",
    "screen": "claude-sonnet-5-5",
    "search": "claude-sonnet-5-5",
    "shipping": "claude-sonnet-5-5",
    "purchase": "claude-sonnet-5-5",
}


def model_for(purpose: str | None) -> str:
    from app.config import get_settings
    from app.db import get_setting

    from app.llm import usage

    key = PURPOSES[purpose][0] if purpose in PURPOSES else None
    model = ((key and get_setting(key)) or PURPOSE_DEFAULTS.get(purpose or "")
             or get_setting("llm_model") or get_settings().llm_model)
    if get_setting("claude_limit_mode") == "haiku" and usage.limit()["over"]:
        return usage.HAIKU    # monthly token limit reached (Settings → Claude): everything on Haiku
    return model


class LLMBackend(Protocol):
    model: str

    async def complete(self, *, system: str, prompt: str, schema: dict[str, Any],
                       images: list[Path] | None = None) -> LLMResult: ...


def get_backend(purpose: str | None = None) -> LLMBackend:
    """Backend for one job (a PURPOSES key: "chat", "ads", "shipping", …), using the model chosen for it in Settings."""
    from app.config import get_settings

    from app.db import get_setting
    from app.llm import usage

    s = get_settings()
    if purpose == "screen" and get_setting("claude_limit_mode") == "pause" \
            and usage.limit()["over"]:
        from app.i18n import t

        raise LLMError(t("Monthly Claude token limit reached: hit screening is paused (Settings → Claude)."))
    if s.llm_backend == "claude_cli":
        from app.llm.claude_cli import ClaudeCLI

        return ClaudeCLI(model=model_for(purpose), purpose=purpose)
    raise LLMError(
        f"LLM_BACKEND={s.llm_backend!r} is not implemented. Option B (claude_cli) was chosen; "
        "the Anthropic SDK backend will only be built if option B stops working."
    )
