"""Base entity for the TrueNAS SCALE (WebSocket) integration.

Entity ids and friendly names follow the scheme of the former ``truenas``
custom integration ("<platform>.truenas_<category>_<item>" and
"<Category> <item>"), so it can replace it without breaking automations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DEV_SYSTEM, DOMAIN, ENTITY_PREFIX, MANUFACTURER
from .coordinator import TrueNASConfigEntry, TrueNASCoordinator, TrueNASData


class TrueNASEntity(CoordinatorEntity[TrueNASCoordinator]):
    """An entity attached to one TrueNAS category device."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        platform: str,
        category: str,
        item: str,
        unique_suffix: str,
    ) -> None:
        """Build ids, names and device info."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_name = f"{category} {item}"
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{unique_suffix}"
        # Requested entity id; HA adds _2, _3... if it is already taken.
        self.entity_id = f"{platform}.{ENTITY_PREFIX}_{slugify(self._attr_name)}"
        system = coordinator.data.system if coordinator.data else {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{slugify(category)}")},
            name=category,
            manufacturer=MANUFACTURER,
            model=system.get("system_product") or "TrueNAS SCALE",
            sw_version=system.get("version_clean"),
            entry_type=DeviceEntryType.SERVICE if category != DEV_SYSTEM else None,
            configuration_url=f"https://{entry.data['host']}",
        )

    @property
    def data(self) -> TrueNASData:
        """Shortcut to the latest snapshot."""
        return self.coordinator.data


def add_dynamic_entities(
    entry: TrueNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
    builder: Callable[[TrueNASCoordinator], dict[str, Callable[[], Entity]]],
) -> None:
    """Add entities now, and later whenever new items show up on the NAS.

    ``builder`` returns {stable key: entity factory} for everything that currently
    exists; keys already added are skipped (an imported pool, a plugged-in
    disk or a new app appears without restarting Home Assistant).
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new: list[Entity] = []
        for key, factory in builder(coordinator).items():
            if key not in known:
                known.add(key)
                new.append(factory())
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


def clean_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop None values from state attributes."""
    return {k: v for k, v in attrs.items() if v is not None}
