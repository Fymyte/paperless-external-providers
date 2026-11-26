"""eDocPerso (https://edocperso.fr), a French digital safe used for payslips.

eDocPerso has no public API. The endpoints below are the ones its web app
calls, captured in November 2026. They can change without notice.
"""

from __future__ import annotations

import datetime
import logging
from email.message import EmailMessage
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from typing import Any
from typing import Self

from paperless_sources.providers import ConfigField
from paperless_sources.providers import ProviderAuthError
from paperless_sources.providers import ProviderError
from paperless_sources.providers import RemoteDocument

if TYPE_CHECKING:
    from collections.abc import Iterator
    from collections.abc import Mapping
    from pathlib import Path
    from types import TracebackType

    import httpx

logger = logging.getLogger(__name__)

API_URL = "https://edocperso.fr/edp-back/api/v1"

PAGE_SIZE = 1000
# Folder trees are a few levels deep. This only stops a parentId loop
_MAX_DEPTH = 32


class EDocPersoProvider:
    name = "eDocPerso"
    version = "0.2.0"
    author = "paperless-sources"
    url = "https://edocperso.fr"
    config_fields = (
        ConfigField("login", help="Account e-mail address"),
        ConfigField("password", secret=True),
        ConfigField(
            "folders",
            required=False,
            help='Folder paths to import, such as ["Mes employeurs"]. Empty means all',
        ),
        ConfigField(
            "type_codes",
            required=False,
            help='eDocPerso document types to import, such as ["PAYE"]. Empty means all',
        ),
    )

    def __init__(self, config: Mapping[str, Any], http: httpx.Client) -> None:
        self._login = str(config["login"])
        self._password = str(config["password"])
        self._folders = _parse_folders(config.get("folders"))
        self._type_codes = _parse_strings(config.get("type_codes"), "type_codes")
        self._http = http
        self._token: str | None = None

    def __enter__(self) -> Self:
        self._authenticate()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        # NOTE: no logout endpoint is known, the token expires after 1 h
        self._token = None

    def list_documents(self) -> Iterator[RemoteDocument]:
        body = self._json(self._request("GET", "/folders"))
        folders = body.get("folders")
        if not isinstance(folders, list):
            raise ProviderError("/folders: no folders list")

        for folder_id, path in _folder_paths(folders).items():
            if self._selected(path):
                yield from self._folder_documents(folder_id, path)

    def download(self, document: RemoteDocument, dest_dir: Path) -> Path:
        response = self._request(
            "POST",
            "/documents/download",
            json={"documentIds": [document.remote_id], "folderIds": []},
            headers={"Accept": "application/octet-stream"},
        )
        if response.headers.get("content-type", "").startswith("application/json"):
            raise ProviderError(f"/documents/download: {response.text[:200]}")
        filename = _attachment_filename(response) or document.filename
        dest = dest_dir / _safe_filename(filename, document.remote_id)
        dest.write_bytes(response.content)
        return dest

    def _folder_documents(
        self,
        folder_id: str,
        path: tuple[str, ...],
    ) -> Iterator[RemoteDocument]:
        offset = 0
        while True:
            body = self._json(
                self._request(
                    "POST",
                    "/documents",
                    json={
                        "paging": {"limit": PAGE_SIZE, "offset": offset},
                        "folderId": folder_id,
                    },
                ),
            )
            items = body.get("items") or []
            for item in items:
                if self._wanted(item, path):
                    yield _to_document(item, path)
            offset += len(items)
            total = (body.get("paging") or {}).get("total", 0)
            if not items or offset >= total:
                return

    def _wanted(self, item: dict[str, Any], path: tuple[str, ...]) -> bool:
        if self._type_codes and item.get("typeCode") not in self._type_codes:
            return False
        # Not downloadable yet, or locked by eDocPerso. A later cycle sees them again
        if item.get("state") != "Ready" or item.get("isGDPRBlocked"):
            logger.debug(
                "Skipping %s in %s: state=%s gdpr_blocked=%s",
                item.get("id"),
                "/".join(path),
                item.get("state"),
                item.get("isGDPRBlocked"),
            )
            return False
        return True

    def _selected(self, path: tuple[str, ...]) -> bool:
        if not self._folders:
            return True
        lowered = tuple(p.casefold() for p in path)
        return any(lowered[: len(f)] == f for f in self._folders)

    def _authenticate(self) -> None:
        response = self._http.post(
            f"{API_URL}/login",
            json={"email": self._login, "password": self._password},
        )
        if response.status_code in {400, 401, 403}:
            raise ProviderAuthError(f"/login: HTTP {response.status_code}")
        _raise_for_status(response)
        token = self._json(response).get("token")
        if not token:
            raise ProviderAuthError("/login: no token in the answer")
        self._token = token

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if self._token is None:
            raise ProviderError("Not logged in, use the provider as a context manager")
        response = self._send(method, path, json, headers)
        if response.status_code == 401:
            # The token lives 1 h. A long first import outlives it, so log in again once
            logger.info("eDocPerso token expired, logging in again")
            self._authenticate()
            response = self._send(method, path, json, headers)
        _raise_for_status(response)
        return response

    def _send(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        return self._http.request(
            method,
            f"{API_URL}{path}",
            json=json,
            headers={
                "Accept": "application/json",
                **(headers or {}),
                "Authorization": f"Bearer {self._token}",
            },
        )

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as e:
            raise ProviderError(f"{response.request.url.path}: not JSON") from e
        if not isinstance(body, dict):
            raise ProviderError(f"{response.request.url.path}: not a JSON object")
        return body


def _folder_paths(folders: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    by_id = {str(f["id"]): f for f in folders if "id" in f}
    paths: dict[str, tuple[str, ...]] = {}
    for folder_id, folder in by_id.items():
        names: list[str] = []
        current: dict[str, Any] | None = folder
        for _ in range(_MAX_DEPTH):
            if current is None:
                break
            names.append(str(current.get("name") or current["id"]))
            parent = current.get("parentId")
            current = by_id.get(str(parent)) if parent else None
        paths[folder_id] = tuple(reversed(names))
    return paths


def _to_document(item: dict[str, Any], path: tuple[str, ...]) -> RemoteDocument:
    return RemoteDocument(
        remote_id=str(item["id"]),
        filename=_filename(item),
        path=path,
        modified=_parse_date(item.get("updatedAt") or item.get("dateAdded")),
    )


def _filename(item: dict[str, Any]) -> str:
    name = str(item.get("title") or item["id"])
    ext = item.get("fileExtension") or ""
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    if ext and not name.lower().endswith(ext.lower()):
        name += ext
    return name


def _attachment_filename(response: httpx.Response) -> str | None:
    value = response.headers.get("content-disposition")
    if not value:
        return None
    # EmailMessage handles quoting and RFC 2231 filename* for us
    message = EmailMessage()
    message["content-disposition"] = value
    return message.get_filename()


def _safe_filename(filename: str, fallback: str) -> str:
    name = PurePosixPath(filename.replace("\\", "/")).name
    return name if name not in {"", ".", ".."} else fallback


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code in {401, 403}:
        raise ProviderAuthError(
            f"{response.request.url.path}: HTTP {response.status_code}",
        )
    if response.is_error:
        raise ProviderError(f"{response.request.url.path}: HTTP {response.status_code}")


def _parse_date(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        logger.debug("Unparsed date %r", value)
        return None


def _parse_strings(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, list):
        raise ValueError(f"{key} must be a list of strings")
    return tuple(str(v) for v in value)


def _parse_folders(value: Any) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(part.casefold() for part in f.strip("/").split("/") if part)
        for f in _parse_strings(value, "folders")
    )
