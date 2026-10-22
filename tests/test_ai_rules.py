"""Settings → AI rules and prompts: every rule and editable system prompt in one place, with its default, the
difference to it and a reset; the built-in prompts read only; the old pages link here."""
from fastapi.testclient import TestClient

from app.db import DEFAULT_SETTINGS, get_setting, set_setting
from app.services import ai_rules, purchase, system_prompts


def test_diff_lines_from_the_default_to_mine():
    assert ai_rules.diff("a\nb\nc", "a\nb\nc") == []
    d = ai_rules.diff("a\nb\nc", "a\nB\nc")
    assert ("-", "b") in d and ("+", "B") in d


def test_the_page_shows_rules_prompts_and_built_ins_and_saves_and_resets():
    from app.main import app
    set_setting("reply_rules", DEFAULT_SETTINGS["reply_rules"] + "\n- Immer mit € schreiben.")
    with TestClient(app) as c:
        page = c.get("/settings/rules").text
        for key in ("reply_rules", "auto_reply_rules", "purchase_rules", "auto_reply", "purchase", "sim_partner"):
            assert f'id="ai-{key}"' in page
        assert 'data-ai-builtin="SCREEN_SYSTEM"' in page and 'data-ai-builtin="DEAL_SYSTEM"' in page
        card = page[page.index('id="ai-reply_rules"'):page.index('id="ai-auto_reply_rules"')]
        assert "data-ai-diff" in card and "Immer mit € schreiben." in card and "data-ai-reset" in card
        assert "data-ai-diff" not in page[page.index('id="ai-auto_reply"'):page.index('id="ai-purchase"')]   # default

        c.post("/settings/ai/rule/reply_rules", data={"do": "reset"})
        assert get_setting("reply_rules") == DEFAULT_SETTINGS["reply_rules"]
        c.post("/settings/ai/rule/purchase_rules", data={"text": "- Nur Privat."})
        assert get_setting("purchase_rules") == "- Nur Privat."
        c.post("/settings/ai/rule/purchase_rules", data={"do": "reset"})
        assert get_setting("purchase_rules") == "" and ai_rules.rule_value(ai_rules.RULES[2]) == purchase.DEFAULT_RULES

        c.post("/settings/ai/prompt/auto_reply", data={"text": "Mein eigener Prompt."})
        assert system_prompts.get("auto_reply") == "Mein eigener Prompt." and system_prompts.custom("auto_reply")
        c.post("/settings/ai/prompt/auto_reply", data={"do": "reset"})
        assert not system_prompts.custom("auto_reply")
        assert c.post("/settings/ai/rule/nope", data={}).status_code == 404


def test_the_old_pages_link_here_and_keep_the_rules():
    from app.main import app
    set_setting("auto_reply_rules", "- Nur einfache Fragen.")
    set_setting("purchase_rules", "- Nur Privat.")
    with TestClient(app) as c:
        assert "/settings/rules#ai-auto_reply_rules" in c.get("/settings/chats").text
        c.post("/settings", data={"section": "chats", "chat_sync_limit": "10"})      # the chats form, no rules in it
        assert get_setting("auto_reply_rules") == "- Nur einfache Fragen."
        c.post("/settings/purchase", data={"discount_pct": "10"})
        assert get_setting("purchase_rules") == "- Nur Privat."
        c.post("/settings", data={"section": "rules", "reply_style": "- kurz"})       # the style card
        assert get_setting("reply_rules") == DEFAULT_SETTINGS["reply_rules"] or get_setting("reply_rules")
