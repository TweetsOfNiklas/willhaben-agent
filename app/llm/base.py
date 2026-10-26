"""LLM backend interface. The app only ever asks for one structured JSON object."""
from __future__ import annotations

from collections.abc import Iterable
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


# Static fallback for the selector in Settings (id, name), used only until the list has been fetched from the API
# (app/llm/models.py). Full ids on purpose: in CLI 2.1.286 the alias "haiku" still means Haiku 4.5.
MODELS = [
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-fable-5-1", "Fable 5.1"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-haiku-5-5", "Haiku 5.5"),
]

# API list prices, USD per million tokens (input, output), and what a cache read costs relative to input.
# Source: platform.claude.com/docs/en/about-claude/pricing, read 2026-10-19. Cache writes: 2x input (the CLI writes
# the 1-hour cache: its own costUSD matched that for Opus and Fable); cache reads 0.05x on Opus 5.5 and Sonnet 5.5,
# 0.025x on Fable 5.1, else 0.1x. Haiku 5.5 costs $0.50 / $2.50 for prompts over 100,000 tokens (input incl. cache).
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
CACHE_READ = {"claude-opus-5-5": 0.05, "claude-sonnet-5-5": 0.05, "claude-fable-5-1": 0.025}
CACHE_WRITE = 2.0
HAIKU_LONG = ("claude-haiku-5-5", 100_000, (0.50, 2.50))


def _priced(model: str | None) -> str | None:
    """The PRICES key of a model id as the CLI reports it ("claude-sonnet-5-5[1m]", a dated id)."""
    m = (model or "").lower()
    return next((k for k in PRICES if m == k or m.startswith(k)), None)


def cost_usd(model: str | None, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0,
             cache_write: int = 0) -> float | None:
    """What a call costs at the API list prices; None for a model without a price (then the CLI's own figure is
    kept). The CLI's costUSD priced Haiku 5.5 like Sonnet and Sonnet 5.5's cache reads at 0.1x."""
    key = _priced(model)
    if key is None:
        return None
    price_in, price_out = PRICES[key]
    name, limit, long_prices = HAIKU_LONG
    if key == name and input_tokens + cache_read + cache_write > limit:
        price_in, price_out = long_prices
    total = (price_in * (input_tokens + CACHE_WRITE * cache_write + CACHE_READ.get(key, 0.1) * cache_read)
             + price_out * output_tokens)
    return round(total / 1_000_000, 6)


def cost_factors(ids: Iterable[str] | None = None) -> dict[str, int]:
    """Token price of each model (default: MODELS) relative to the cheapest one (input + output list price),
    cheapest first, e.g. {"claude-haiku-5-5": 1, "claude-sonnet-5-5": 20, …}. Models without a price are left out."""
    priced = sorted((sum(PRICES[m]), m) for m in (ids if ids is not None else [m for m, _n in MODELS]) if m in PRICES)
    return {m: round(p / priced[0][0]) for p, m in priced} if priced else {}


# What a model is used for -> settings key. Unset keys fall back to PURPOSE_DEFAULTS, then llm_model / LLM_MODEL.
PURPOSES = {
    "chat": ("llm_model_chat", _("Chat replies"), _("reply drafts for willhaben messages")),
    "ads": ("llm_model_ads", _("Creating listings"), _("title, description and category from notes and photos")),
    "screen": ("llm_model_screen", _("Screening search hits"), _("right item? good deal? – runs for every new hit, a fast model is enough")),
    "search": ("llm_model_search", _("Product search"), _("plans willhaben searches from your description and picks the best offers – two or three calls per search")),
    "shipping": ("llm_model_shipping", _("Shipping & labels"), _("reads address and parcel size from chats, fills and buys carrier labels")),
    "purchase": ("llm_model_purchase", _("Purchases & PayLivery"), _("opening message and negotiation as buyer, PayLivery steps")),
    "style": ("llm_model_style", _("Writing style"), _("the style profile derived from your own chat messages – runs only when you ask for it")),
    "compare": ("llm_model_compare", _("Similar ads"), _("names the product and picks the similar ads for the price check – two calls per check")),
}


# Opus only where quality matters most (writing listings); everything else runs on Sonnet by default.
PURPOSE_DEFAULTS = {
    "ads": "claude-opus-5-5",
    "chat": "claude-sonnet-5-5",
    "screen": "claude-sonnet-5-5",
    "search": "claude-sonnet-5-5",
    "shipping": "claude-sonnet-5-5",
    "purchase": "claude-sonnet-5-5",
    "style": "claude-sonnet-5-5",
    "compare": "claude-sonnet-5-5",
}


def model_for(purpose: str | None) -> str:
    from app.config import get_settings
    from app.db import get_setting

    from app.llm import usage

    key = PURPOSES[purpose][0] if purpose in PURPOSES else None
    model = ((key and get_setting(key)) or PURPOSE_DEFAULTS.get(purpose or "")
             or get_setting("llm_model") or get_settings().llm_model)
    if get_setting("claude_limit_mode") == "haiku" and usage.limit()["over"]:
        return usage.haiku()  # monthly token limit reached (Settings → Claude): everything on Haiku
    return model


class LLMBackend(Protocol):
    model: str

    async def complete(self, *, system: str, prompt: str, schema: dict[str, Any],
                       images: list[Path] | None = None) -> LLMResult: ...


def get_backend(purpose: str | None = None, model: str | None = None) -> LLMBackend:
    """Backend for one job (a PURPOSES key: "chat", "ads", "shipping", …), using the model chosen for it in Settings
    (`model`: another one for this call, e.g. the chat simulator's choice; the monthly limit still applies)."""
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

        if model and not (get_setting("claude_limit_mode") == "haiku" and usage.limit()["over"]):
            return ClaudeCLI(model=model, purpose=purpose)
        return ClaudeCLI(model=model_for(purpose), purpose=purpose)
    raise LLMError(
        f"LLM_BACKEND={s.llm_backend!r} is not implemented. Option B (claude_cli) was chosen; "
        "the Anthropic SDK backend will only be built if option B stops working."
    )
