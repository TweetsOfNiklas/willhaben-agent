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


# What a model is used for -> settings key. Unset keys fall back to llm_model, then LLM_MODEL.
PURPOSES = {
    "chat": ("llm_model_chat", _("Chat replies"), _("reply drafts for willhaben messages")),
    "ads": ("llm_model_ads", _("Creating listings"), _("title, description and category from notes and photos")),
    "screen": ("llm_model_screen", _("Screening search hits"), _("right item? good deal? – runs for every new hit, a fast model is enough")),
}


def model_for(purpose: str | None) -> str:
    from app.config import get_settings
    from app.db import get_setting

    key = PURPOSES[purpose][0] if purpose in PURPOSES else None
    return (key and get_setting(key)) or get_setting("llm_model") or get_settings().llm_model


class LLMBackend(Protocol):
    model: str

    async def complete(self, *, system: str, prompt: str, schema: dict[str, Any],
                       images: list[Path] | None = None) -> LLMResult: ...


def get_backend(purpose: str | None = None) -> LLMBackend:
    """Backend for one job ("chat" or "ads"), using the model chosen for it in Settings."""
    from app.config import get_settings

    s = get_settings()
    if s.llm_backend == "claude_cli":
        from app.llm.claude_cli import ClaudeCLI

        return ClaudeCLI(model=model_for(purpose))
    raise LLMError(
        f"LLM_BACKEND={s.llm_backend!r} is not implemented. Option B (claude_cli) was chosen; "
        "the Anthropic SDK backend will only be built if option B stops working."
    )
