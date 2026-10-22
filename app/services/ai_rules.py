"""Everything Claude is told, on one settings page (Settings → AI rules and prompts): the owner's free-text rules, the
editable system prompts (app/services/system_prompts.py) and, read only, the built-in prompts of every other task.

Each editable text has a default; the page shows the difference to it and a reset. A rule whose default applies when
it is empty (the purchase rules) is reset to empty; the others are reset to their default text.
"""
from __future__ import annotations

import difflib
import importlib
from dataclasses import dataclass

from app.db import DEFAULT_SETTINGS, get_setting, set_setting
from app.services import system_prompts

MAX_CHARS = 4000


def _(text: str) -> str:
    """Marks a string for the German catalog; translated where it is shown (t())."""
    return text


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    help: str
    empty_is_default: bool = False      # empty = the default applies (stored empty on reset)

    def default(self) -> str:
        if self.key == "purchase_rules":
            from app.services import purchase
            return purchase.DEFAULT_RULES
        return DEFAULT_SETTINGS.get(self.key, "")


RULES = (
    Rule("reply_rules", _("Rules for replies and listings"),
         _("In every Claude prompt: chat replies, automatic answers and new listings.")),
    Rule("auto_reply_rules", _("General instruction for automatic answers"),
         _("Automatic answers when selling, in addition to the rules above and each ad's own instruction.")),
    Rule("purchase_rules", _("My rules for purchases"),
         _("Purchase orders: the first message and the negotiation as buyer. Empty: the default applies."),
         empty_is_default=True),
)
_BY_KEY = {r.key: r for r in RULES}

# read only: (module, constant, label)
BUILT_IN = (
    ("app.llm.prompts", "REPLY_SYSTEM", _("Reply drafts")),
    ("app.llm.prompts", "PURCHASE_OPENER_SYSTEM", _("First message of a purchase")),
    ("app.llm.prompts", "SCREEN_SYSTEM", _("Checking new hits (screening)")),
    ("app.llm.prompts", "AD_SYSTEM", _("Writing new ads")),
    ("app.llm.prompts", "SORT_SYSTEM", _("Sorting photos into ads")),
    ("app.llm.prompts", "MEETUP_SYSTEM", _("Finding agreed appointments in chats")),
    ("app.llm.prompts", "SEARCH_PLAN_SYSTEM", _("Product search: the plan")),
    ("app.llm.prompts", "SEARCH_RANK_SYSTEM", _("Product search: choosing the best offers")),
    ("app.llm.prompts", "SEARCH_PHOTO_SYSTEM", _("Product search from a photo")),
    ("app.services.deals", "DEAL_SYSTEM", _("Price check of an ad")),
    ("app.services.deals", "GH_QUERIES_SYSTEM", _("Geizhals: search words")),
    ("app.services.deals", "GH_PICK_SYSTEM", _("Geizhals: picking the product")),
    ("app.services.geizhals_check", "CHECK_SYSTEM", _("Geizhals: checking a match")),
    ("app.services.geizhals_check", "PICK_SYSTEM", _("Geizhals: picking a match")),
    ("app.services.style", "STYLE_SYSTEM", _("Deriving my writing style")),
    ("app.services.paylivery", "STEP_SYSTEM", _("PayLivery: the next step")),
    ("app.services.shipping", "EXTRACT_SYSTEM", _("Reading a shipping address from a chat")),
    ("app.services.label_fill", "FILL_SYSTEM", _("Filling in a shipping label form")),
    ("app.services.label_buy", "BUY_SYSTEM", _("Buying a shipping label")),
    ("app.llm.prompts", "SIM_JUDGE_SYSTEM", _("Simulator: checking expectations")),
    ("app.llm.prompts", "SIM_SUGGEST_SYSTEM", _("Simulator: proposing a rule change")),
    ("app.llm.prompts", "SIM_REVIEW_SYSTEM", _("Simulator: reviewing a prompt")),
    ("app.llm.prompts", "SIM_ROUND_REVIEW_SYSTEM", _("Simulator: reviewing a round")),
)


def diff(default: str, current: str) -> list[tuple[str, str]]:
    """Line diff from the default to the current text: [(kind, line)], kind "-" (only in the default), "+" (only in
    mine) or " " (context); [] when they are the same."""
    a, b = (default or "").strip().splitlines(), (current or "").strip().splitlines()
    if a == b:
        return []
    out = []
    for line in difflib.unified_diff(a, b, lineterm="", n=1):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            out.append(("@", "…"))
        else:
            out.append((line[:1] or " ", line[1:]))
    return out


def rule_value(rule: Rule) -> str:
    """The rule as it applies now (an empty purchase rule: its default)."""
    raw = (get_setting(rule.key) or "").strip()
    return raw or (rule.default() if rule.empty_is_default else "")


def rules() -> list[dict]:
    out = []
    for r in RULES:
        value, default = rule_value(r), r.default()
        out.append({"key": r.key, "label": r.label, "help": r.help, "value": value, "default": default,
                    "custom": value.strip() != default.strip(), "diff": diff(default, value)})
    return out


def save_rule(key: str, text: str) -> None:
    r = _BY_KEY[key]
    text = (text or "").replace("\r\n", "\n").strip()[:MAX_CHARS]
    if r.empty_is_default and text == r.default().strip():
        text = ""
    set_setting(key, text)


def reset_rule(key: str) -> None:
    r = _BY_KEY[key]
    set_setting(key, "" if r.empty_is_default else r.default())


def is_rule(key: str) -> bool:
    return key in _BY_KEY


def prompts() -> list[dict]:
    """The editable system prompts (automatic answers when selling and buying, the simulator's other side)."""
    out = []
    for kind, (label, _role, _default) in system_prompts.KINDS.items():
        value, default = system_prompts.get(kind), system_prompts.default(kind)
        out.append({"key": kind, "label": label, "value": value, "default": default,
                    "custom": system_prompts.custom(kind), "diff": diff(default, value)})
    return out


def built_in() -> list[dict]:
    """The other prompts, read only, in the order above; a constant that is gone is left out."""
    out = []
    for module, name, label in BUILT_IN:
        text = getattr(importlib.import_module(module), name, None)
        if isinstance(text, str) and text.strip():
            out.append({"key": name, "label": label, "text": text.strip()})
    return out
