"""Second-hand platforms the search agents can watch. See base.py for the adapter contract, registry.py for lookup."""
from app.platforms.base import (  # noqa: F401
    CAPABILITIES,
    Blocked,
    InvalidRef,
    InvalidSearchUrl,
    Platform,
    PlatformError,
    UnknownPlatform,
    split_ref,
)
from app.platforms.registry import (  # noqa: F401
    DEFAULT,
    aclose_all,
    all,
    enabled,
    for_host,
    for_url,
    get,
    multi,
    names,
    reset,
    set_platform,
    status,
)
from app.platforms.types import (  # noqa: F401
    DEFAULT_PLATFORM,
    AdDetail,
    FilterValue,
    Listing,
    SearchFilter,
    SearchFilters,
    SearchPage,
    SearchSpec,
    ad_ref,
)
