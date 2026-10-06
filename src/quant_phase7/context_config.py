"""Strict optional loaders for operator-reviewed Phase 7 labels and thresholds."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import AssetKind, CanonicalAssetId, Chain, DataStatus
from .labels import AddressLabel, LabelCategory, LabelSnapshot
from .whale import WhaleThresholdConfig, WhaleTier


MAX_CONFIG_BYTES = 1_048_576


def _read_json(path: str) -> Any:
    raw = Path(path).read_bytes()
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("Phase 7 context config exceeds size limit")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Phase 7 context config is not valid JSON") from None


def _utc_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO UTC timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} must be an ISO UTC timestamp") from None
    if result.tzinfo is None or result.utcoffset() is None or result.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field} must be an ISO UTC timestamp")
    return result


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def load_reviewed_label_snapshots(path: str) -> dict[Chain, LabelSnapshot]:
    """Load reviewed snapshots whose content hash is verified before use."""
    if not path:
        return {}
    payload = _mapping(_read_json(path), "labels config")
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, list) or len(snapshots) > 2:
        raise ValueError("labels config must contain at most two chain snapshots")
    result: dict[Chain, LabelSnapshot] = {}
    for raw_snapshot in snapshots:
        item = dict(_mapping(raw_snapshot, "label snapshot"))
        claimed_hash = item.get("snapshot_hash")
        canonical = json.dumps(
            {key: value for key, value in item.items() if key != "snapshot_hash"},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        actual_hash = hashlib.sha256(canonical).hexdigest()
        if claimed_hash != actual_hash:
            raise ValueError("label snapshot content hash does not match")
        try:
            chain = Chain(item["chain"])
            effective_from = _utc_datetime(item["effective_from"], "effective_from")
            effective_to = (
                _utc_datetime(item["effective_to"], "effective_to")
                if item.get("effective_to") is not None else None
            )
            labels_raw = item["labels"]
            urls = item["source_urls"]
            if not isinstance(labels_raw, list) or len(labels_raw) > 100_000:
                raise ValueError("label list exceeds the bounded size")
            if not isinstance(urls, list):
                raise ValueError("source_urls must be a list")
            labels = tuple(AddressLabel(
                chain=chain,
                address=row["address"],
                category=LabelCategory(row["category"]),
                source_id=item["source_id"],
                source_version=item["source_version"],
                label_version=item["label_version"],
                confidence=Decimal(str(row["confidence"])),
                snapshot_hash=actual_hash,
                source_reference=str(row.get("source_reference") or urls[0]),
                effective_from=effective_from,
                effective_to=effective_to,
                observed_at=_utc_datetime(row.get("observed_at", item["effective_from"]), "observed_at"),
                updated_at=_utc_datetime(row.get("updated_at", item["effective_from"]), "updated_at"),
                status=DataStatus(row.get("status", "AVAILABLE")),
                reason=row.get("reason"),
            ) for row in (_mapping(value, "address label") for value in labels_raw))
            snapshot = LabelSnapshot(
                chain=chain,
                source_id=item["source_id"],
                source_version=item["source_version"],
                label_version=item["label_version"],
                source_urls=tuple(urls),
                reviewer=item["reviewer"],
                snapshot_hash=actual_hash,
                effective_from=effective_from,
                effective_to=effective_to,
                coverage_denominator=item["coverage_denominator"],
                reviewed=item["reviewed"],
                labels=labels,
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ValueError("label snapshot fields are invalid") from None
        if chain in result:
            raise ValueError("duplicate label snapshot chain")
        result[chain] = snapshot
    return result


def _asset_id(value: Any) -> CanonicalAssetId:
    if not isinstance(value, str):
        raise ValueError("threshold asset_id must be text")
    parts = value.split(":", 3)
    if len(parts) != 4:
        raise ValueError("threshold asset_id is malformed")
    return CanonicalAssetId(
        chain=Chain(parts[0]), kind=AssetKind(parts[1]),
        contract_or_native=parts[2], registry_version=parts[3],
    )


def load_whale_thresholds(path: str) -> dict[tuple[Chain, str], WhaleThresholdConfig]:
    """Load versioned event-time USD threshold tiers; no defaults are inferred."""
    if not path:
        return {}
    payload = _mapping(_read_json(path), "thresholds config")
    items = payload.get("thresholds")
    if not isinstance(items, list) or len(items) > 16:
        raise ValueError("thresholds config exceeds the bounded entry count")
    result: dict[tuple[Chain, str], WhaleThresholdConfig] = {}
    for raw_item in items:
        item = _mapping(raw_item, "whale threshold")
        try:
            chain = Chain(item["chain"])
            asset_id = _asset_id(item["asset_id"])
            tiers_raw = item["tiers"]
            if not isinstance(tiers_raw, list) or len(tiers_raw) > 16:
                raise ValueError("threshold tiers exceed the bounded count")
            tiers = tuple(WhaleTier(
                name=tier["name"],
                min_usd=Decimal(str(tier["min_usd"])),
                max_usd=Decimal(str(tier["max_usd"])) if tier.get("max_usd") is not None else None,
            ) for tier in (_mapping(value, "whale tier") for value in tiers_raw))
            config = WhaleThresholdConfig(
                chain=chain, asset_id=asset_id,
                threshold_version=item["threshold_version"], tiers=tiers,
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ValueError("whale threshold fields are invalid") from None
        key = (
            chain,
            f"{chain.value}:{asset_id.kind.value}:{asset_id.contract_or_native}:{asset_id.registry_version}",
        )
        if key in result:
            raise ValueError("duplicate whale threshold identity")
        result[key] = config
    return result
