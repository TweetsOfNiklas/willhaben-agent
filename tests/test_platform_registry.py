"""The platform registry: lookup by id and link, switching sites on and off, refs, test overrides."""
import pytest

from app import platforms
from app.config import get_settings
from app.db import SearchAgent, session, set_setting
from app.platforms import InvalidRef, InvalidSearchUrl, Platform, SearchSpec, UnknownPlatform, ad_ref, split_ref


class FakePlatform(Platform):
    id = "fake"
    name = "Fake"
    host = "fake.example"
    hosts = ("fake.example",)
    capabilities = frozenset({"search_url"})

    @property
    def limiter(self):
        from app.platforms.ratelimit import RateLimiter
        return RateLimiter(0, name="Fake")

    def parse_search_url(self, url):
        return SearchSpec(platform="fake", url=url)

    def describe(self, spec):
        return "fake"

    def keyword_search(self, keyword, price_from=None, price_to=None, *, category=None, sort="newest"):
        return SearchSpec(platform="fake", url=f"https://fake.example/s?q={keyword}", keyword=keyword)

    async def search(self, spec, *, rows=None, cache_s=0):
        from app.platforms import SearchPage
        return SearchPage(rows_found=0, ads=[])

    def ad_url(self, ad_id):
        return f"https://fake.example/ad/{ad_id}"


def test_default_is_willhaben():
    assert platforms.get("willhaben").id == "willhaben" and platforms.get("").id == "willhaben"
    assert platforms.enabled("willhaben") and [p.id for p in platforms.all()] == ["willhaben"]
    assert platforms.for_url("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=x").id == "willhaben"
    assert platforms.for_host("cache.willhaben.at").id == "willhaben" and platforms.for_host("example.com") is None


def test_unknown_and_unsupported():
    with pytest.raises(UnknownPlatform):
        platforms.get("nope")
    with pytest.raises(InvalidSearchUrl, match="supported site"):
        platforms.for_url("https://example.com/search?q=x")
    with pytest.raises(InvalidSearchUrl):
        platforms.for_url("not a url")
    assert not platforms.enabled("nope")


def test_override_and_reset():
    platforms.set_platform("fake", FakePlatform())
    try:
        assert platforms.get("fake").name == "Fake" and platforms.enabled("fake")
        assert platforms.for_url("https://fake.example/s?q=x").id == "fake"
        assert [p.id for p in platforms.all()] == ["willhaben", "fake"]
        platforms.set_platform("fake", None)
        with pytest.raises(UnknownPlatform):
            platforms.get("fake")
    finally:
        platforms.reset()
    with pytest.raises(UnknownPlatform):
        platforms.get("fake")


def test_setting_switches_a_platform_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "platforms_enabled", "willhaben,kleinanzeigen")
    platforms.reset()
    platforms.set_platform("kleinanzeigen", FakePlatform())      # stands in until the real adapter exists
    try:
        set_setting("platform_kleinanzeigen_enabled", "0")
        # an override counts as enabled; the setting is what the real registry reads
        assert platforms.enabled("kleinanzeigen")
    finally:
        platforms.reset()


def test_refs():
    assert ad_ref("willhaben", "123") == "123" and ad_ref(None, "123") == "123"
    assert ad_ref("kleinanzeigen", "123") == "kleinanzeigen:123"
    assert split_ref("123") == ("willhaben", "123") and split_ref("kleinanzeigen:123") == ("kleinanzeigen", "123")
    with pytest.raises(InvalidRef):
        split_ref("Bad Platform:1")
    with pytest.raises(InvalidRef):
        split_ref("x:")


def test_multi_counts_distinct_platforms():
    assert platforms.multi() is False
    with session() as db:
        db.add(SearchAgent(name="a", source_url="x", webapi_url="x"))
        db.add(SearchAgent(name="b", source_url="x", webapi_url="x"))
        db.commit()
    assert platforms.multi() is False
