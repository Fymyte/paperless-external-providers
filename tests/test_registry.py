from paperless_sources.providers.edocperso import EDocPersoProvider
from paperless_sources.providers.registry import discover_providers


def test_builtin_provider_is_registered():
    assert discover_providers()["edocperso"] is EDocPersoProvider
