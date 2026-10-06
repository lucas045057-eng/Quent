"""Audited source registry and bounded URL policy.

This module deliberately does not perform network I/O.  Adapters and the
future fetcher must receive a source definition from ``SourceRegistry`` before
they can request a URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlparse


class SourceType(StrEnum):
    OFFICIAL = "OFFICIAL"
    RSS = "RSS"
    PUBLIC_API = "PUBLIC_API"
    LICENSED = "LICENSED"
    EXCHANGE = "EXCHANGE"
    PROJECT = "PROJECT"
    THIRD_PARTY = "THIRD_PARTY"


@dataclass(frozen=True, slots=True)
class BoundedFetchPolicy:
    max_bytes: int = 256 * 1024
    timeout_seconds: float = 10.0
    max_redirects: int = 0
    max_concurrency: int = 2
    min_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_bytes <= 8 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 8 MiB")
        if not 0 < self.timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be in (0, 60]")
        if not 0 <= self.max_redirects <= 3:
            raise ValueError("max_redirects must be between 0 and 3")
        if not 1 <= self.max_concurrency <= 16:
            raise ValueError("max_concurrency must be between 1 and 16")
        if self.min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    source_id: str
    source_type: SourceType
    base_url: str
    allowed_hosts: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    parser_version: str
    policy_version: str
    fetch_policy: BoundedFetchPolicy = field(default_factory=BoundedFetchPolicy)

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("source_id is required")
        if not self.parser_version.strip() or not self.policy_version.strip():
            raise ValueError("parser_version and policy_version are required")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("source base_url must be HTTPS with a hostname")
        hosts = tuple(host.strip().lower() for host in self.allowed_hosts if host.strip())
        paths = tuple(path.strip() for path in self.allowed_paths if path.strip())
        if parsed.hostname.lower() not in hosts:
            raise ValueError("base_url host must be allowlisted")
        if not paths or not any(_path_allowed(parsed.path or "/", path) for path in paths):
            raise ValueError("base_url path must be allowlisted")
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "allowed_paths", paths)


class SourceRegistry:
    """Immutable-at-use audited source lookup with exact URL validation."""

    def __init__(self, definitions: list[SourceDefinition] | tuple[SourceDefinition, ...] = ()) -> None:
        by_id: dict[str, SourceDefinition] = {}
        for definition in definitions:
            if definition.source_id in by_id:
                raise ValueError(f"duplicate source_id: {definition.source_id}")
            by_id[definition.source_id] = definition
        self._definitions = by_id

    def require(self, source_id: str) -> SourceDefinition:
        try:
            return self._definitions[source_id]
        except KeyError as exc:
            raise KeyError(f"source is not approved: {source_id}") from exc

    def validate_url(self, source_id: str, url: str) -> bool:
        definition = self.require(source_id)
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        if parsed.username or parsed.password or parsed.port:
            return False
        if parsed.hostname.lower() not in definition.allowed_hosts:
            return False
        return any(_path_allowed(parsed.path or "/", path) for path in definition.allowed_paths)

    def validate_redirect(self, source_id: str, location: str) -> bool:
        return self.validate_url(source_id, location)

    def all(self) -> tuple[SourceDefinition, ...]:
        return tuple(self._definitions.values())


def _path_allowed(actual: str, allowed: str) -> bool:
    normalized_allowed = "/" + allowed.strip("/") if allowed.strip("/") else "/"
    normalized_actual = actual or "/"
    return normalized_actual == normalized_allowed or normalized_actual.startswith(normalized_allowed + "/")
