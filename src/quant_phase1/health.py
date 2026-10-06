"""Component health aggregation used to gate Stage1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .contracts import DataStatus


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    component: str
    status: DataStatus
    checked_at: datetime
    details: dict[str, Any]


class HealthRegistry:
    def __init__(self) -> None:
        self._components: dict[str, ComponentHealth] = {}

    def set(self, health: ComponentHealth) -> None:
        self._components[health.component] = health

    def snapshot(self) -> dict[str, ComponentHealth]:
        return dict(self._components)

    def overall(self) -> ComponentHealth:
        if not self._components:
            return ComponentHealth("system", DataStatus.NOT_AVAILABLE, datetime.now().astimezone(), {"reason": "no components"})
        priority = {DataStatus.ERROR: 4, DataStatus.STALE: 3, DataStatus.NOT_AVAILABLE: 2, DataStatus.AVAILABLE: 1}
        selected = max(self._components.values(), key=lambda item: priority[item.status])
        return ComponentHealth("system", selected.status, selected.checked_at, {"components": list(self._components)})

    def allows_stage1(self) -> bool:
        return self.overall().status is DataStatus.AVAILABLE
