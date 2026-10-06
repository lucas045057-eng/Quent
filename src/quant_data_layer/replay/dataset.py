from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .manifest import (
    ReplayContractError,
    ReplayManifest,
    _contains_secret_key,
    load_manifest,
    parse_utc,
    verify_replay_components,
)


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_RECORD_STATUSES = {"AVAILABLE", "PARTIAL", "STALE", "NOT_AVAILABLE", "ERROR"}


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    phase: int
    stream_id: str
    source_id: str
    canonical_identity: str
    event_timestamp_utc: Any
    source_timestamp_utc: Any | None
    fetched_at_utc: Any
    processed_at_utc: Any
    provenance: str
    status: str
    fields: Mapping[str, Mapping[str, Any]]
    payload: Any


@dataclass(frozen=True, slots=True)
class ReplayDataset:
    manifest: ReplayManifest
    records: tuple[ReplayRecord, ...]
    dataset_sha256: str
    uncompressed_bytes: int


def _parse_record(value: Any, *, line_number: int, manifest: ReplayManifest) -> ReplayRecord:
    if not isinstance(value, Mapping) or _contains_secret_key(value):
        raise ReplayContractError(f"record {line_number} is malformed or contains a forbidden sensitive field")
    required = {
        "phase", "stream_id", "source_id", "canonical_identity", "event_timestamp_utc",
        "source_timestamp_utc", "fetched_at_utc", "processed_at_utc", "provenance", "status", "fields", "payload",
    }
    if not required.issubset(value):
        raise ReplayContractError(f"record {line_number} is missing required contract fields")
    phase = value["phase"]
    if not isinstance(phase, int) or isinstance(phase, bool) or phase not in manifest.required_phases:
        raise ReplayContractError(f"record {line_number} has an invalid phase")
    for field in ("stream_id", "source_id", "canonical_identity"):
        item = value[field]
        if not isinstance(item, str) or _SAFE_ID.fullmatch(item) is None:
            raise ReplayContractError(f"record {line_number} has an invalid {field}")
    event_time = parse_utc(value["event_timestamp_utc"], field=f"record {line_number} event_timestamp_utc")
    source_time = (
        None if value["source_timestamp_utc"] is None
        else parse_utc(value["source_timestamp_utc"], field=f"record {line_number} source_timestamp_utc")
    )
    fetched_at = parse_utc(value["fetched_at_utc"], field=f"record {line_number} fetched_at_utc")
    processed_at = parse_utc(value["processed_at_utc"], field=f"record {line_number} processed_at_utc")
    if any(timestamp > manifest.as_of_utc for timestamp in (event_time, fetched_at, processed_at)):
        raise ReplayContractError(f"record {line_number} contains a future timestamp")
    if source_time is not None and source_time > manifest.as_of_utc:
        raise ReplayContractError(f"record {line_number} contains a future source timestamp")
    provenance = value["provenance"]
    status = value["status"]
    if not isinstance(provenance, str) or not provenance or len(provenance) > 64:
        raise ReplayContractError(f"record {line_number} has invalid provenance")
    if status not in _RECORD_STATUSES:
        raise ReplayContractError(f"record {line_number} has an invalid status")
    fields = value["fields"]
    if not isinstance(fields, Mapping) or not fields:
        raise ReplayContractError(f"record {line_number} fields must be a non-empty object")
    for field_name, field_value in fields.items():
        if not isinstance(field_name, str) or not isinstance(field_value, Mapping):
            raise ReplayContractError(f"record {line_number} field metadata is malformed")
        if not {"value", "status", "provenance"}.issubset(field_value):
            raise ReplayContractError(f"record {line_number} field metadata is incomplete")
        field_status = field_value["status"]
        if field_status not in _RECORD_STATUSES:
            raise ReplayContractError(f"record {line_number} field status is invalid")
        field_provenance = field_value["provenance"]
        if not isinstance(field_provenance, str) or not field_provenance or len(field_provenance) > 64:
            raise ReplayContractError(f"record {line_number} field provenance is invalid")
        if (field_value["value"] is None) != (field_status == "NOT_AVAILABLE"):
            raise ReplayContractError(f"record {line_number} violates missing-versus-value semantics")
    payload = value["payload"]
    if not isinstance(payload, (dict, list)):
        raise ReplayContractError(f"record {line_number} payload must be an object or list")
    return ReplayRecord(
        phase=phase,
        stream_id=value["stream_id"],
        source_id=value["source_id"],
        canonical_identity=value["canonical_identity"],
        event_timestamp_utc=event_time,
        source_timestamp_utc=source_time,
        fetched_at_utc=fetched_at,
        processed_at_utc=processed_at,
        provenance=provenance,
        status=status,
        fields=fields,
        payload=payload,
    )


def load_replay_dataset(
    manifest_path: Path,
    *,
    max_compressed_bytes: int = 2 * 1024 * 1024,
    max_uncompressed_bytes: int = 16 * 1024 * 1024,
    max_records: int = 20_000,
    project_root: Path | None = None,
) -> ReplayDataset:
    if max_compressed_bytes <= 0 or max_uncompressed_bytes <= 0 or max_records <= 0:
        raise ReplayContractError("replay byte and record limits must be positive")
    manifest = load_manifest(manifest_path)
    if project_root is not None:
        verify_replay_components(manifest, project_root=project_root)
    fixture_path = (manifest_path.parent / manifest.fixture.filename).resolve()
    if fixture_path.parent != manifest_path.parent.resolve():
        raise ReplayContractError("fixture path must remain inside the manifest directory")
    if manifest.fixture.compressed_bytes > max_compressed_bytes:
        raise ReplayContractError("fixture exceeds configured compressed byte limit")
    if manifest.fixture.uncompressed_bytes > max_uncompressed_bytes:
        raise ReplayContractError("fixture exceeds configured uncompressed byte limit")
    if manifest.fixture.record_count > max_records:
        raise ReplayContractError("fixture exceeds configured record limit")
    try:
        compressed_digest = hashlib.sha256()
        compressed_bytes = 0
        with fixture_path.open("rb") as compressed_file:
            for chunk in iter(lambda: compressed_file.read(64 * 1024), b""):
                compressed_bytes += len(chunk)
                if compressed_bytes > max_compressed_bytes:
                    raise ReplayContractError("fixture exceeds configured compressed byte limit")
                compressed_digest.update(chunk)
    except OSError as exc:
        raise ReplayContractError("replay fixture cannot be read") from exc
    if compressed_digest.hexdigest() != manifest.fixture.compressed_sha256:
        raise ReplayContractError("fixture compressed hash mismatch")
    if compressed_bytes != manifest.fixture.compressed_bytes:
        raise ReplayContractError("fixture compressed byte count mismatch")

    records: list[ReplayRecord] = []
    digest = hashlib.sha256()
    byte_count = 0
    identities: set[tuple[int, str, str]] = set()
    try:
        with gzip.open(fixture_path, "rb") as stream:
            line_number = 0
            while True:
                line = stream.readline(max_uncompressed_bytes - byte_count + 1)
                if not line:
                    break
                byte_count += len(line)
                if byte_count > max_uncompressed_bytes:
                    raise ReplayContractError("fixture exceeds configured uncompressed byte limit")
                digest.update(line)
                line_number += 1
                try:
                    value = json.loads(line, parse_float=Decimal)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ReplayContractError(f"record {line_number} is not valid JSON") from exc
                record = _parse_record(value, line_number=line_number, manifest=manifest)
                identity = (record.phase, record.stream_id, record.canonical_identity)
                if identity in identities:
                    raise ReplayContractError(f"record {line_number} duplicates canonical identity")
                identities.add(identity)
                records.append(record)
                if len(records) > max_records:
                    raise ReplayContractError("fixture exceeds configured record limit")
    except (OSError, EOFError) as exc:
        raise ReplayContractError("fixture gzip stream is invalid") from exc
    if byte_count != manifest.fixture.uncompressed_bytes:
        raise ReplayContractError("fixture uncompressed byte count mismatch")
    if len(records) != manifest.fixture.record_count:
        raise ReplayContractError("fixture record count mismatch")
    if digest.hexdigest() != manifest.dataset_sha256:
        raise ReplayContractError("dataset hash mismatch")
    phases = {record.phase for record in records}
    if phases != set(manifest.required_phases):
        raise ReplayContractError("fixture does not cover every required phase")
    return ReplayDataset(manifest, tuple(records), digest.hexdigest(), byte_count)
