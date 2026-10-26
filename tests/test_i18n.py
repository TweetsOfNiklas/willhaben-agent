"""Every translatable string in templates and Python code has a German entry, and placeholders match."""
import re
import string
from pathlib import Path

from app import i18n

APP = Path(__file__).resolve().parent.parent / "app"
# _("...") / _('...') in templates, t("...") / t('...') in Python; plain literals only
CALL = re.compile(r"""(?<![\w.])(?:_|t)\(\s*(?P<q>["'])(?P<s>(?:\\.|(?!(?P=q)).)*?)(?P=q)\s*[,)]""", re.S)


def _strings():
    found = {}
    for f in list(APP.rglob("*.html")) + list(APP.rglob("*.py")):
        if f.parent.name == "i18n":
            continue
        for m in CALL.finditer(f.read_text(encoding="utf-8")):
            s = m.group("s").encode().decode("unicode_escape").encode("latin-1").decode("utf-8") if "\\" in m.group("s") else m.group("s")
            found.setdefault(s, f.relative_to(APP).as_posix())
    return found


def _fields(s):
    return {name for _, name, _, _ in string.Formatter().parse(s) if name}


def test_german_catalog_complete():
    de = i18n.catalog("de")
    missing = {s: f for s, f in _strings().items() if s not in de}
    assert not missing, "missing German translations:\n" + "\n".join(f"{f}: {s!r}" for s, f in sorted(missing.items(), key=lambda x: x[1]))


def test_placeholders_match():
    bad = [(en, de) for en, de in i18n.catalog("de").items() if _fields(en) != _fields(de)]
    assert not bad, bad


def test_t_formats_and_falls_back():
    i18n.set_request_lang("de")
    assert i18n.t("{t} ago", t="5 min") == "vor 5 min"
    assert i18n.t("Some string without translation") == "Some string without translation"
    i18n.set_request_lang("en")
    assert i18n.t("{t} ago", t="5 min") == "5 min ago"


def test_fragments_do_not_conflict():
    """The same English string must not get different German translations in different fragment files."""
    import json
    seen, clashes = {}, []
    for f in sorted((APP / "i18n" / "de").glob("*.json")):
        for en, de in json.loads(f.read_text(encoding="utf-8")).items():
            if en in seen and seen[en][1] != de:
                clashes.append((en, seen[en], (f.name, de)))
            seen.setdefault(en, (f.name, de))
    assert not clashes, clashes


def test_one_takes_the_singular():
    """A count of one says "1 comparable ad"; every singular has its German entry with the same placeholders."""
    from app.i18n.plural import SINGULAR
    de = i18n.catalog("de")
    assert all(one in de and _fields(one) == _fields(many) for many, one in SINGULAR.items()), \
        [one for one in SINGULAR.values() if one not in de]
    i18n.set_request_lang("en")
    assert i18n.t("{n} comparable ads", n=1) == "1 comparable ad" and i18n.t("{n} comparable ads", n=2) == "2 comparable ads"
    i18n.set_request_lang("de")
    assert i18n.t("{n} comparable ads", n=1) == "1 vergleichbare Anzeige"
    assert i18n.t("{n} new hits", n=3) == "3 neue Treffer"
    i18n.set_request_lang("en")
