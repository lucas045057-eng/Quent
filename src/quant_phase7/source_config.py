"""Phase 7 read-only RPC configuration and safe health diagnostics.

This module owns configuration boundaries only.  It never opens a network
connection and never promotes configuration to live source availability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from quant_phase1.config import Phase7RpcSourceSettings, Settings
from quant_phase1.contracts import DataStatus
from quant_phase1.health import ComponentHealth, HealthRegistry
from quant_phase1.logging import sanitize_endpoint


class SourceConfigurationStatus(StrEnum):
    SOURCE_DISABLED = "SOURCE_DISABLED"
    SOURCE_NOT_CONFIGURED = "SOURCE_NOT_CONFIGURED"
    SOURCE_CONFIG_INVALID = "SOURCE_CONFIG_INVALID"
    CONFIGURED = "CONFIGURED"


def _status(config: Phase7RpcSourceSettings) -> SourceConfigurationStatus:
    if not config.enabled:
        return SourceConfigurationStatus.SOURCE_DISABLED
    if not config.endpoint:
        return SourceConfigurationStatus.SOURCE_NOT_CONFIGURED
    parsed = urlparse(config.endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return SourceConfigurationStatus.SOURCE_CONFIG_INVALID
    try:
        parsed.port
    except ValueError:
        return SourceConfigurationStatus.SOURCE_CONFIG_INVALID
    if config.auth_mode == "BASIC" and (not config.username or not config.password):
        return SourceConfigurationStatus.SOURCE_CONFIG_INVALID
    if config.auth_mode == "API_KEY_HEADER" and (
        config.api_key is None or not config.api_key.get_secret_value().strip()
    ):
        return SourceConfigurationStatus.SOURCE_CONFIG_INVALID
    return SourceConfigurationStatus.CONFIGURED


def source_configuration_status(config: Phase7RpcSourceSettings) -> SourceConfigurationStatus:
    """Return a non-secret configuration state for one RPC source."""

    return _status(config)


def source_configuration_diagnostic(config: Phase7RpcSourceSettings) -> dict[str, Any]:
    """Return bounded metadata suitable for logs, health rows and reports."""

    status = _status(config)
    return {
        "source_id": config.source_id,
        "source_status": status.value,
        "configured": status is SourceConfigurationStatus.CONFIGURED,
        "host": sanitize_endpoint(config.endpoint),
        "auth_mode": config.auth_mode,
        "timeout_seconds": config.timeout_seconds,
        "requests_per_second": config.requests_per_second,
        "max_concurrency": config.max_concurrency,
        "max_response_bytes": config.max_response_bytes,
    }


def phase7_source_diagnostics(settings: Settings) -> dict[str, dict[str, Any]]:
    """Build diagnostics for both normative Chain sources without secrets."""

    return {
        config.source_id: source_configuration_diagnostic(config)
        for config in (settings.phase7_bitcoin_rpc, settings.phase7_ethereum_rpc)
    }


def validate_phase7_source_startup(settings: Settings) -> None:
    """Fail startup only for an explicitly enabled invalid/unconfigured source."""

    invalid: list[str] = []
    for config in (settings.phase7_bitcoin_rpc, settings.phase7_ethereum_rpc):
        status = _status(config)
        if config.enabled and status is not SourceConfigurationStatus.CONFIGURED:
            invalid.append(f"{config.source_id}={status.value}")
    if invalid:
        raise ValueError("Phase 7 source configuration invalid: " + ", ".join(invalid))


def register_phase7_source_health(
    registry: HealthRegistry,
    settings: Settings,
    *,
    checked_at: datetime | None = None,
) -> dict[str, ComponentHealth]:
    """Expose configuration state through the existing Phase 1 health registry.

    A configured endpoint is intentionally still ``NOT_AVAILABLE`` until a
    live adapter reports a successful read.  This prevents configuration from
    being mistaken for data availability.
    """

    checked_at = checked_at or datetime.now(timezone.utc)
    diagnostics = phase7_source_diagnostics(settings)
    result: dict[str, ComponentHealth] = {}
    for source_id, details in diagnostics.items():
        status = (
            DataStatus.ERROR
            if details["source_status"] == SourceConfigurationStatus.SOURCE_CONFIG_INVALID
            else DataStatus.NOT_AVAILABLE
        )
        component = ComponentHealth(source_id, status, checked_at, details)
        registry.set(component)
        result[source_id] = component
    return result


__all__ = [
    "SourceConfigurationStatus",
    "phase7_source_diagnostics",
    "register_phase7_source_health",
    "source_configuration_diagnostic",
    "source_configuration_status",
    "validate_phase7_source_startup",
]
