"""TOML config plus secrets from the environment.

Secrets never come from the file. For a field named `password` in a source
named `payslips`, the value comes from SOURCE_PAYSLIPS_PASSWORD, or from the
file named by SOURCE_PAYSLIPS_PASSWORD_FILE (Docker secrets).
"""

from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from paperless_sources.providers import SourceProvider

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_UPLOAD_SINGLE = ("correspondent", "document_type", "storage_path")
_UPLOAD_MULTI = ("tags", "custom_fields")


class ConfigError(Exception):
    pass


@dataclasses.dataclass(frozen=True, slots=True)
class UploadSpec:
    """post_document fields as written in the config, as IDs or names."""

    correspondent: int | str | None = None
    document_type: int | str | None = None
    storage_path: int | str | None = None
    tags: tuple[int | str, ...] = ()
    custom_fields: tuple[int | str, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    provider: str
    config: dict[str, Any]
    upload: UploadSpec


@dataclasses.dataclass(frozen=True, slots=True)
class AppConfig:
    paperless_url: str
    paperless_token: str
    state_path: Path
    poll_interval: int
    max_attempts: int
    sources: tuple[SourceConfig, ...]


def load(
    path: Path,
    providers: Mapping[str, type[SourceProvider]],
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    env = os.environ if env is None else env
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e

    paperless = raw.get("paperless", {})
    url = env.get("PAPERLESS_URL") or paperless.get("url")
    if not url:
        raise ConfigError("paperless.url or PAPERLESS_URL is required")
    if "token" in paperless:
        raise ConfigError("Put the token in PAPERLESS_TOKEN, not in the config file")
    token = _from_env(env, "PAPERLESS_TOKEN")
    if not token:
        raise ConfigError("PAPERLESS_TOKEN or PAPERLESS_TOKEN_FILE is required")

    sources = tuple(
        _source(s, providers, env)
        for s in raw.get("sources", [])
        if s.get("enabled", True)
    )
    names = [s.name for s in sources]
    if len(set(names)) != len(names):
        raise ConfigError(f"Source names must be unique: {names}")

    return AppConfig(
        paperless_url=url,
        paperless_token=token,
        state_path=Path(raw.get("state_path", "/data/state.sqlite3")),
        poll_interval=_positive_int(raw, "poll_interval", 3600),
        max_attempts=_positive_int(raw, "max_attempts", 3),
        sources=sources,
    )


def _source(
    raw: dict[str, Any],
    providers: Mapping[str, type[SourceProvider]],
    env: Mapping[str, str],
) -> SourceConfig:
    name = raw.get("name", "")
    if not _NAME_RE.match(name):
        raise ConfigError(f"Source name {name!r} must match {_NAME_RE.pattern}")
    provider_key = raw.get("provider", "")
    provider = providers.get(provider_key)
    if provider is None:
        raise ConfigError(
            f"{name}: unknown provider {provider_key!r}, known: {sorted(providers)}",
        )

    file_config: dict[str, Any] = dict(raw.get("config", {}))
    fields = {f.name: f for f in provider.config_fields}
    unknown = set(file_config) - set(fields)
    if unknown:
        raise ConfigError(f"{name}: unknown config keys {sorted(unknown)}")

    config: dict[str, Any] = {}
    prefix = "SOURCE_" + name.upper().replace("-", "_") + "_"
    for field in provider.config_fields:
        if field.secret and field.name in file_config:
            raise ConfigError(
                f"{name}: {field.name} is secret, set {prefix}{field.name.upper()}",
            )
        value = _from_env(env, prefix + field.name.upper())
        if value is None:
            value = file_config.get(field.name)
        if value is None and field.required:
            raise ConfigError(
                f"{name}: {field.name} is required, "
                f"set it in the file or in {prefix}{field.name.upper()}",
            )
        if value is not None:
            config[field.name] = value

    return SourceConfig(
        name=name,
        provider=provider_key,
        config=config,
        upload=_upload(name, raw.get("upload", {})),
    )


def _upload(name: str, raw: dict[str, Any]) -> UploadSpec:
    unknown = set(raw) - set(_UPLOAD_SINGLE) - set(_UPLOAD_MULTI)
    if unknown:
        raise ConfigError(f"{name}: unknown upload keys {sorted(unknown)}")
    values: dict[str, Any] = {}
    for key in _UPLOAD_SINGLE:
        if key in raw:
            values[key] = _id_or_name(name, key, raw[key])
    for key in _UPLOAD_MULTI:
        if key in raw:
            if not isinstance(raw[key], list):
                raise ConfigError(f"{name}: upload.{key} must be a list")
            values[key] = tuple(_id_or_name(name, key, v) for v in raw[key])
    return UploadSpec(**values)


def _id_or_name(name: str, key: str, value: Any) -> int | str:
    # bool is an int subclass, and `true` is never a valid ID or name
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ConfigError(f"{name}: upload.{key} values must be IDs or names")
    return value


def _from_env(env: Mapping[str, str], key: str) -> str | None:
    if value := env.get(key):
        return value
    if file := env.get(f"{key}_FILE"):
        try:
            return Path(file).read_text().strip()
        except OSError as e:
            raise ConfigError(f"{key}_FILE: {e}") from e
    return None


def _positive_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer")
    return value
