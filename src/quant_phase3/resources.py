"""Low-overhead resource guardrails for the existing deployment."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceAssessment:
    status: str
    reasons: tuple[str, ...]


def assess_resource_usage(
    *,
    collector_memory_mib: float,
    engine_memory_mib: float,
    postgres_memory_mib: float,
    overall_cpu_percent: float | None = None,
) -> ResourceAssessment:
    reasons: list[str] = []
    critical = False
    if collector_memory_mib > 220:
        reasons.append(f"collector memory {collector_memory_mib:.1f} MiB exceeds warning threshold 220 MiB")
    if collector_memory_mib > 256:
        reasons.append(f"collector memory {collector_memory_mib:.1f} MiB exceeds limit 256 MiB")
        critical = True
    if engine_memory_mib > 384:
        reasons.append(f"engine memory {engine_memory_mib:.1f} MiB exceeds limit 384 MiB")
        critical = True
    if postgres_memory_mib > 384:
        reasons.append(f"postgres memory {postgres_memory_mib:.1f} MiB exceeds limit 384 MiB")
        critical = True
    if overall_cpu_percent is not None and overall_cpu_percent > 90:
        reasons.append(f"overall CPU {overall_cpu_percent:.1f}% exceeds warning threshold 90%")
    if critical:
        return ResourceAssessment("RESOURCE_CRITICAL", tuple(reasons))
    if reasons:
        return ResourceAssessment("RESOURCE_WARNING", tuple(reasons))
    return ResourceAssessment("AVAILABLE", ())
