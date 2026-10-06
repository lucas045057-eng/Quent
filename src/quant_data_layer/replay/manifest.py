from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import random
import re
from typing import Any, Mapping


class ReplayContractError(ValueError):
    """Raised when replay provenance or fixture data violates its contract."""


_SHA256 = re.compile(r"[a-f0-9]{64}")
_GIT_SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
_IMAGE_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|password|secret|authorization|access[_-]?token|refresh[_-]?token|private[_-]?key)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:https?|wss?)://[^\s/@:]+:[^\s/@]+@"
    r"|[?&\s](?:api[_-]?key|token|access[_-]?token|secret|password|authorization)=[^&#\s]+"
    r"|\bauthorization\s*:\s*(?:basic|bearer)\s+\S+",
    re.IGNORECASE,
)
_SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def parse_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ReplayContractError(f"{field} must be an explicit UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayContractError(f"{field} must be an explicit UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ReplayContractError(f"{field} must be an explicit UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _SECRET_KEY.search(str(key)) is not None
            or (_SECRET_VALUE.search(child) is not None if isinstance(child, str) else _contains_secret_key(child))
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_key(child) for child in value)
    if isinstance(value, str):
        return _SECRET_VALUE.search(value) is not None
    return False


@dataclass(frozen=True, slots=True)
class ReplayFixture:
    filename: str
    compressed_sha256: str
    compressed_bytes: int
    uncompressed_bytes: int
    record_count: int

    @classmethod
    def from_mapping(cls, value: Any) -> ReplayFixture:
        if not isinstance(value, Mapping):
            raise ReplayContractError("fixture descriptor must be an object")
        filename = value.get("filename")
        digest = value.get("compressed_sha256")
        compressed_bytes = value.get("compressed_bytes")
        uncompressed_bytes = value.get("uncompressed_bytes")
        record_count = value.get("record_count")
        if not isinstance(filename, str) or _SAFE_FILENAME.fullmatch(filename) is None or not filename.endswith(".gz"):
            raise ReplayContractError("fixture filename must be a bounded local gzip filename")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ReplayContractError("fixture compressed_sha256 must be a lowercase SHA-256")
        if not isinstance(compressed_bytes, int) or isinstance(compressed_bytes, bool) or compressed_bytes <= 0:
            raise ReplayContractError("fixture compressed_bytes must be a positive integer")
        if not isinstance(uncompressed_bytes, int) or isinstance(uncompressed_bytes, bool) or uncompressed_bytes <= 0:
            raise ReplayContractError("fixture uncompressed_bytes must be a positive integer")
        if not isinstance(record_count, int) or isinstance(record_count, bool) or record_count <= 0:
            raise ReplayContractError("fixture record_count must be a positive integer")
        return cls(filename, digest, compressed_bytes, uncompressed_bytes, record_count)


@dataclass(frozen=True, slots=True)
class ReplayManifest:
    replay_version: str
    git_sha: str
    collector_image_digest: str
    engine_image_digest: str
    postgres_image_digest: str
    postgres_server_version: str
    migration_version: int
    config_sha256: str
    dataset_sha256: str
    seed: int
    as_of_utc: datetime
    fixture: ReplayFixture
    required_phases: tuple[int, ...]
    replay_components_sha256: tuple[tuple[str, str], ...]

    @classmethod
    def from_mapping(cls, value: Any) -> ReplayManifest:
        if not isinstance(value, Mapping):
            raise ReplayContractError("replay manifest must be an object")
        if _contains_secret_key(value):
            raise ReplayContractError("replay manifest contains a forbidden sensitive field")
        if value.get("replay_version") != "DATA_LAYER_REPLAY_V1":
            raise ReplayContractError("replay_version must be DATA_LAYER_REPLAY_V1")
        git_sha = value.get("git_sha")
        if not isinstance(git_sha, str) or _GIT_SHA.fullmatch(git_sha) is None:
            raise ReplayContractError("git_sha must be a full hexadecimal commit identity")
        image_digests = (
            value.get("collector_image_digest"),
            value.get("engine_image_digest"),
            value.get("postgres_image_digest"),
        )
        if any(not isinstance(item, str) or _IMAGE_DIGEST.fullmatch(item) is None for item in image_digests):
            raise ReplayContractError("application and PostgreSQL image digests must be pinned")
        postgres_server_version = value.get("postgres_server_version")
        if not isinstance(postgres_server_version, str) or not re.fullmatch(r"16\.15(?:\.[0-9]+)?", postgres_server_version):
            raise ReplayContractError("postgres_server_version must pin PostgreSQL 16.15")
        migration_version = value.get("migration_version")
        if not isinstance(migration_version, int) or isinstance(migration_version, bool) or migration_version < 1:
            raise ReplayContractError("migration_version must be a positive integer")
        for name in ("config_sha256", "dataset_sha256"):
            digest = value.get(name)
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise ReplayContractError(f"{name} must be a lowercase SHA-256")
        seed = value.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0 or seed > 2**64 - 1:
            raise ReplayContractError("seed must be an unsigned 64-bit integer")
        required_phases = value.get("required_phases")
        if (
            not isinstance(required_phases, list)
            or any(not isinstance(phase, int) or isinstance(phase, bool) for phase in required_phases)
            or tuple(required_phases) != tuple(range(1, 9))
        ):
            raise ReplayContractError("required_phases must list Phase 1 through Phase 8 in order")
        components = value.get("replay_components_sha256")
        if (
            not isinstance(components, Mapping)
            or not components
            or any(
                not isinstance(name, str)
                or PurePosixPath(name).is_absolute()
                or any(part in {"", ".", ".."} for part in PurePosixPath(name).parts)
                or "\\" in name
                or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
                for name, digest in components.items()
            )
        ):
            raise ReplayContractError("replay_components_sha256 must pin named source components")
        if value.get("fixture_origin") != "synthetic_test_data_only":
            raise ReplayContractError("fixture_origin must identify synthetic test data")
        for flag in ("external_provider_calls", "private_api_calls", "trading_enabled"):
            if value.get(flag) is not False:
                raise ReplayContractError(f"{flag} must be false for deterministic replay")
        return cls(
            replay_version="DATA_LAYER_REPLAY_V1",
            git_sha=git_sha,
            collector_image_digest=image_digests[0],
            engine_image_digest=image_digests[1],
            postgres_image_digest=image_digests[2],
            postgres_server_version=postgres_server_version,
            migration_version=migration_version,
            config_sha256=value["config_sha256"],
            dataset_sha256=value["dataset_sha256"],
            seed=seed,
            as_of_utc=parse_utc(value.get("as_of_utc"), field="as_of_utc"),
            fixture=ReplayFixture.from_mapping(value.get("fixture")),
            required_phases=tuple(required_phases),
            replay_components_sha256=tuple(sorted(components.items())),
        )


def load_manifest(path: Path, *, max_manifest_bytes: int = 1_048_576) -> ReplayManifest:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReplayContractError("replay manifest cannot be read") from exc
    if len(raw) > max_manifest_bytes:
        raise ReplayContractError("replay manifest exceeds configured byte limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayContractError("replay manifest is not valid UTF-8 JSON") from exc
    return ReplayManifest.from_mapping(value)


def verify_replay_components(manifest: ReplayManifest, *, project_root: Path) -> None:
    root = project_root.resolve()
    for relative, expected in manifest.replay_components_sha256:
        component = PurePosixPath(relative)
        candidate = (root / Path(*component.parts)).resolve()
        if candidate != root and root not in candidate.parents:
            raise ReplayContractError("replay component path escapes the project root")
        try:
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError as exc:
            raise ReplayContractError("replay component cannot be read") from exc
        if digest != expected:
            raise ReplayContractError("replay component hash mismatch")


def seeded_random(seed: int, namespace: str) -> random.Random:
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0 or seed > 2**64 - 1:
        raise ReplayContractError("seed must be an unsigned 64-bit integer")
    if not isinstance(namespace, str) or not namespace or len(namespace) > 128:
        raise ReplayContractError("seed namespace must be non-empty and bounded")
    derived = hashlib.sha256(f"DATA_LAYER_REPLAY_V1\0{seed}\0{namespace}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(derived[:8], "big"))
