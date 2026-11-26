from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from paperless_sources.providers import SourceProvider

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "paperless_sources.providers"

_REQUIRED_ATTRS = ("name", "version", "author", "url", "config_fields")


def discover_providers() -> dict[str, type[SourceProvider]]:
    """Load every provider registered under the entry point group.

    A provider that fails to load is logged and skipped, so one broken plugin
    does not stop the others.
    """
    providers: dict[str, type[SourceProvider]] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            provider_class = ep.load()
        except Exception:
            logger.exception("Failed to load provider entry point %r", ep.name)
            continue

        missing = [a for a in _REQUIRED_ATTRS if not hasattr(provider_class, a)]
        if missing:
            logger.warning(
                "Provider %r is missing attributes %r, skipping",
                ep.name,
                missing,
            )
            continue

        if ep.name in providers:
            logger.warning("Provider %r registered twice, keeping the first", ep.name)
            continue

        providers[ep.name] = provider_class
        logger.debug(
            "Loaded provider %r: %s v%s by %s",
            ep.name,
            provider_class.name,
            provider_class.version,
            provider_class.author,
        )
    return providers
