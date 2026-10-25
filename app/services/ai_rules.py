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


GROUPS = (("general", _("For every task")), ("sell", _("Selling")), ("buy", _("Buying")), ("sim", _("Simulator")))


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    help: str
    group: str = "general"
    empty_is_default: bool = False      # empty = the default applies (stored empty on reset)
    max_chars: int = MAX_CHARS

    def default(self) -> str:
        if self.key == "purchase_rules":
            from app.services import purchase
            return purchase.DEFAULT_RULES
        return DEFAULT_SETTINGS.get(self.key, "")


RULES = (
    Rule("reply_rules", _("General rules"),
         _("Tone, form of address, what always holds: in every chat answer (buying and selling) and in new listings.")),
    Rule("sell_notes", _("Rules when selling"),
         _("For answers to buyers, by hand and automatic, and for new listings. Handover, shipping and payment are chosen "
           "under Selling preferences."), group="sell", max_chars=2000),
    Rule("auto_reply_rules", _("Additionally for automatic answers"),
         _("What the app may answer on its own when selling; each ad's own instruction comes on top."), group="sell"),
    Rule("purchase_rules", _("Rules for purchase orders (automatic negotiation)"),
         _("The automatic instructions for buying: the first message and the negotiation of every purchase order. "
           "Empty: the default applies."), group="buy", empty_is_default=True),
)
_BY_KEY = {r.key: r for r in RULES}

# read only: (module, constant, label, task)
BUILT_IN = (
    ("app.llm.prompts", "REPLY_SYSTEM", _("Reply drafts"), "general"),
    ("app.services.style", "STYLE_SYSTEM", _("Deriving my writing style"), "general"),
    ("app.llm.prompts", "MEETUP_SYSTEM", _("Finding agreed appointments in chats"), "general"),
    ("app.llm.prompts", "AD_SYSTEM", _("Writing new ads"), "sell"),
    ("app.llm.prompts", "SORT_SYSTEM", _("Sorting photos into ads"), "sell"),
    ("app.services.paylivery", "STEP_SYSTEM", _("PayLivery: the next step"), "sell"),
    ("app.services.shipping", "EXTRACT_SYSTEM", _("Reading a shipping address from a chat"), "sell"),
    ("app.services.label_fill", "FILL_SYSTEM", _("Filling in a shipping label form"), "sell"),
    ("app.services.label_buy", "BUY_SYSTEM", _("Buying a shipping label"), "sell"),
    ("app.llm.prompts", "PURCHASE_OPENER_SYSTEM", _("First message of a purchase"), "buy"),
    ("app.llm.prompts", "SCREEN_SYSTEM", _("Checking new hits (screening)"), "buy"),
    ("app.llm.prompts", "SEARCH_PLAN_SYSTEM", _("Product search: the plan"), "buy"),
    ("app.llm.prompts", "SEARCH_RANK_SYSTEM", _("Product search: choosing the best offers"), "buy"),
    ("app.llm.prompts", "SEARCH_PHOTO_SYSTEM", _("Product search from a photo"), "buy"),
    ("app.services.deals", "DEAL_SYSTEM", _("Price check of an ad"), "buy"),
    ("app.services.deals", "GH_QUERIES_SYSTEM", _("Geizhals: search words"), "buy"),
    ("app.services.deals", "GH_PICK_SYSTEM", _("Geizhals: picking the product"), "buy"),
    ("app.services.geizhals_check", "CHECK_SYSTEM", _("Geizhals: checking a match"), "buy"),
    ("app.services.geizhals_check", "PICK_SYSTEM", _("Geizhals: picking a match"), "buy"),
    ("app.llm.prompts", "SIM_JUDGE_SYSTEM", _("Simulator: checking expectations"), "sim"),
    ("app.llm.prompts", "SIM_SUGGEST_SYSTEM", _("Simulator: proposing a rule change"), "sim"),
    ("app.llm.prompts", "SIM_REVIEW_SYSTEM", _("Simulator: reviewing a prompt"), "sim"),
    ("app.llm.prompts", "SIM_ROUND_REVIEW_SYSTEM", _("Simulator: reviewing a round"), "sim"),
    ("app.services.chat_sim", "FILL_SYSTEM", _("Simulator: filling the owner's gaps"), "sim"),
    ("app.services.agent_sim", "ADS_SYSTEM", _("Agent simulator: inventing test ads"), "sim"),
    ("app.services.agent_sim", "BRIEF_SYSTEM", _("Agent simulator: improving the brief"), "sim"),
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
        out.append({"key": r.key, "label": r.label, "help": r.help, "group": r.group, "value": value, "default": default,
                    "custom": value.strip() != default.strip(), "diff": diff(default, value)})
    return out


def save_rule(key: str, text: str) -> None:
    r = _BY_KEY[key]
    text = (text or "").replace("\r\n", "\n").strip()[:r.max_chars]
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
    groups = {"auto_reply": "sell", "purchase": "buy", "sim_partner": "sim"}
    for kind, (label, _role, _default) in system_prompts.KINDS.items():
        value, default = system_prompts.get(kind), system_prompts.default(kind)
        out.append({"key": kind, "label": label, "group": groups.get(kind, "general"), "value": value, "default": default,
                    "custom": system_prompts.custom(kind), "diff": diff(default, value)})
    return out


def built_in() -> list[dict]:
    """The other prompts, read only, in the order above; a constant that is gone is left out."""
    out = []
    for module, name, label, group in BUILT_IN:
        text = getattr(importlib.import_module(module), name, None)
        if isinstance(text, str) and text.strip():
            out.append({"key": name, "label": label, "group": group, "text": text.strip()})
    return out
