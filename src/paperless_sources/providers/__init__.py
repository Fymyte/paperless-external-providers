from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol
from typing import Self
from typing import runtime_checkable

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterator
    from collections.abc import Mapping
    from pathlib import Path
    from types import TracebackType

    import httpx


class ProviderError(Exception):
    """The remote service failed or answered something unexpected."""


class ProviderAuthError(ProviderError):
    """The remote service rejected the credentials."""


@dataclasses.dataclass(frozen=True, slots=True)
class ConfigField:
    """One configuration value a provider needs.

    Secret fields are never read from the config file. The runner reads them
    from the environment, and a future web UI would mask them.
    """

    name: str
    secret: bool = False
    required: bool = True
    help: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class RemoteDocument:
    """A document listed by a provider.

    remote_id must stay the same for the life of the document at the
    provider. It is the key that prevents a second upload.
    """

    remote_id: str
    filename: str
    path: tuple[str, ...] = ()
    modified: datetime.datetime | None = None


@runtime_checkable
class SourceProvider(Protocol):
    """Contract for a document source.

    Providers are discovered through the "paperless_sources.providers" entry
    point group. The entry point name is the value of `provider` in the config.

    Class attributes, read without creating an instance:

    name : str
        Human-readable name, such as "eDocPerso".
    version : str
        Semantic version string.
    author : str
        Author or organisation name.
    url : str
        Documentation, source code or issue tracker.
    config_fields : tuple[ConfigField, ...]
        Values the provider reads from its config mapping.

    The runner always uses a provider as a context manager. __enter__ logs in
    and __exit__ releases the session.

    A provider only talks to its remote service. It knows nothing of
    Paperless-ngx, so the same class works from this tool and from the
    Paperless-ngx server.
    """

    name: str
    version: str
    author: str
    url: str
    config_fields: tuple[ConfigField, ...]

    def __init__(self, config: Mapping[str, Any], http: httpx.Client) -> None:
        """Store the config. Do not touch the network here.

        http is owned by the caller, who sets timeouts, proxies and any
        outbound URL restrictions.
        """
        ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    def list_documents(self) -> Iterator[RemoteDocument]:
        """Yield every document the config selects.

        Raises ProviderAuthError if the session expired and ProviderError for
        any other remote failure.
        """
        ...

    def download(self, document: RemoteDocument, dest_dir: Path) -> Path:
        """Write the document into dest_dir and return the file path.

        The caller owns dest_dir and deletes it after use.
        """
        ...
