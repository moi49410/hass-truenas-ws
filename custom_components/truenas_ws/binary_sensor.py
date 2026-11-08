"""Binary sensors for the TrueNAS SCALE (WebSocket) integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ALERT_PROBLEM_LEVELS,
    DEV_APPS,
    DEV_DISKS,
    DEV_SERVICES,
    DEV_SYSTEM,
    GIB,
)
from .coordinator import (
    TrueNASConfigEntry,
    TrueNASCoordinator,
    TrueNASData,
    alert_text,
    prop,
)
from .entity import TrueNASEntity, add_dynamic_entities, app_icon, clean_attrs
from .sensor import _pool_attrs, _pool_value

PARALLEL_UPDATES = 0


class TrueNASBinarySensor(TrueNASEntity, BinarySensorEntity):
    """A binary sensor computed from the coordinator snapshot."""

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        category: str,
        item: str,
        unique_suffix: str,
        is_on_fn: Callable[[TrueNASData], bool | None],
        exists_fn: Callable[[TrueNASData], bool],
        attrs_fn: Callable[[TrueNASData], dict[str, Any]],
        device_class: BinarySensorDeviceClass | None = None,
        picture_fn: Callable[[TrueNASData], str | None] | None = None,
    ) -> None:
        """Create the binary sensor."""
        super().__init__(coordinator, "binary_sensor", category, item, unique_suffix)
        self._is_on_fn = is_on_fn
        self._exists_fn = exists_fn
        self._attrs_fn = attrs_fn
        self._attr_device_class = device_class
        self._picture_fn = picture_fn

    @property
    def entity_picture(self) -> str | None:
        """App icon from the TrueNAS catalog, when there is one."""
        if self._picture_fn is None:
            return None
        return self._picture_fn(self.data)

    @property
    def available(self) -> bool:
        """Unavailable when the item is gone (exported pool, removed app...)."""
        return super().available and self._exists_fn(self.data)

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        try:
            return self._is_on_fn(self.data)
        except (KeyError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the extra attributes."""
        try:
            return clean_attrs(self._attrs_fn(self.data))
        except (KeyError, TypeError, ValueError):
            return None


def _build(coordinator: TrueNASCoordinator) -> dict[str, Callable[[], Entity]]:
    data = coordinator.data
    out: dict[str, Callable[[], Entity]] = {}

    def add(key: str, *args: Any, **kwargs: Any) -> None:
        out[key] = lambda: TrueNASBinarySensor(coordinator, *args, **kwargs)

    # Pool health: on = healthy (kept as in the former integration).
    def _pool_health_attrs(pool: dict[str, Any]) -> dict[str, Any]:
        attrs = _pool_attrs(pool)
        attrs.pop("Usage", None)
        free = prop(_pool_value(pool, "free"))
        size = prop(_pool_value(pool, "size"))
        attrs["Available GiB"] = (
            round(free / GIB, 2) if isinstance(free, int | float) else None
        )
        attrs["Total GiB"] = (
            round(size / GIB, 2) if isinstance(size, int | float) else None
        )
        return attrs

    for pool_name in data.pools:
        add(
            f"pool_health_{pool_name}",
            DEV_SYSTEM,
            f"{pool_name} healthy",
            f"pool_{pool_name}_healthy",
            lambda d, n=pool_name: bool(d.pools[n].get("healthy")),
            lambda d, n=pool_name: n in d.pools,
            lambda d, n=pool_name: _pool_health_attrs(d.pools[n]),
        )

    # Services: on = running.
    for svc_name in data.services:
        add(
            f"svc_{svc_name}",
            DEV_SERVICES,
            svc_name,
            f"service_{svc_name}",
            lambda d, n=svc_name: d.services[n].get("state") == "RUNNING",
            lambda d, n=svc_name: n in d.services,
            lambda d, n=svc_name: {
                "Enable": d.services[n].get("enable"),
                "State": d.services[n].get("state"),
            },
            BinarySensorDeviceClass.RUNNING,
        )

    # Apps: on = running.
    def _app_attrs(app: dict[str, Any]) -> dict[str, Any]:
        summary = app.get("_upgrade_summary") or {}
        return {
            "Name": app.get("name"),
            "State": app.get("state"),
            "Version": app.get("version"),
            "Latest version": summary.get("latest_version") or app.get("version"),
            "Human version": app.get("human_version"),
            "Update available": app.get("upgrade_available"),
            "Image updates available": app.get("image_updates_available"),
            "Custom app": app.get("custom_app"),
            "Portal": next(iter((app.get("portals") or {}).values()), None),
        }

    for app_name in data.apps:
        add(
            f"app_{app_name}",
            DEV_APPS,
            app_name,
            f"app_{app_name}",
            lambda d, n=app_name: d.apps[n].get("state") == "RUNNING",
            lambda d, n=app_name: n in d.apps,
            lambda d, n=app_name: _app_attrs(d.apps[n]),
            BinarySensorDeviceClass.RUNNING,
            lambda d, n=app_name: app_icon(d.apps.get(n)),
        )

    # Disk health: on = problem (ZFS errors, degraded vdev member, or an
    # active TrueNAS alert naming the disk, e.g. SMART or temperature).
    def _disk_alerts(d: TrueNASData, disk: dict[str, Any]) -> list[str]:
        needles = [n for n in (disk.get("name"), disk.get("serial")) if n]
        return [
            alert_text(a)
            for a in d.alerts
            if not a.get("dismissed") and any(n in alert_text(a) for n in needles)
        ]

    def _disk_problem(d: TrueNASData, key: str) -> bool:
        disk = d.disks[key]
        health = d.disk_health.get(disk["name"]) or {}
        errors = sum(
            health.get(k) or 0
            for k in ("read_errors", "write_errors", "checksum_errors")
        )
        status = health.get("status")
        return bool(
            errors
            or (status and status not in ("ONLINE", "AVAIL"))
            or _disk_alerts(d, disk)
        )

    for disk_key, disk in data.disks.items():
        add(
            f"disk_health_{disk_key}",
            DEV_DISKS,
            f"{disk['name']} health",
            f"disk_{disk_key}_health",
            lambda d, k=disk_key: _disk_problem(d, k),
            lambda d, k=disk_key: k in d.disks,
            lambda d, k=disk_key: {
                **(d.disk_health.get(d.disks[k]["name"]) or {}),
                "alerts": _disk_alerts(d, d.disks[k]) or None,
            },
            BinarySensorDeviceClass.PROBLEM,
        )

    # Any active TrueNAS alert of level WARNING or above.
    def _problem_alerts(d: TrueNASData) -> list[str]:
        return [
            alert_text(a)
            for a in d.alerts
            if not a.get("dismissed")
            and str(a.get("level") or "").upper() in ALERT_PROBLEM_LEVELS
        ]

    out["sys_alert"] = lambda: TrueNASBinarySensor(
        coordinator,
        DEV_SYSTEM,
        "Alert",
        "system_alert",
        lambda d: bool(_problem_alerts(d)),
        lambda d: True,
        lambda d: {"Alerts": _problem_alerts(d) or None},
        BinarySensorDeviceClass.PROBLEM,
    )

    return out


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrueNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensors."""
    add_dynamic_entities(entry, async_add_entities, _build)
