"""Buttons: power, Wake-on-LAN, scrub, SMART tests and configuration backup."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import actions
from .api import TrueNASError
from .const import CONF_MAC, DEV_SYSTEM
from .coordinator import TrueNASConfigEntry, TrueNASCoordinator
from .entity import TrueNASEntity, add_dynamic_entities

PARALLEL_UPDATES = 1

REASON = "Requested from Home Assistant"

PressFn = Callable[[], Awaitable[object]]


class TrueNASButton(TrueNASEntity, ButtonEntity):
    """A button running one operation on the NAS."""

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        item: str,
        unique_suffix: str,
        press: Callable[[TrueNASButton], Awaitable[object]],
        icon: str,
        device_class: ButtonDeviceClass | None = None,
        entity_category: EntityCategory | None = None,
        always_available: bool = False,
    ) -> None:
        """Create the button."""
        super().__init__(coordinator, "button", DEV_SYSTEM, item, unique_suffix)
        self._press = press
        self._attr_icon = icon
        self._attr_device_class = device_class
        self._attr_entity_category = entity_category
        self._always_available = always_available

    @property
    def available(self) -> bool:
        """The Wake-on-LAN button stays usable while the NAS is off."""
        if self._always_available:
            return bool(self.coordinator.config_entry.data.get(CONF_MAC))
        return super().available

    async def async_press(self) -> None:
        """Run the operation."""
        await self._press(self)


async def _power(button: TrueNASButton, method: str) -> None:
    try:
        await button.coordinator.client.call(method, [REASON])
    except TrueNASError as err:
        raise HomeAssistantError(f"{method} failed: {err}") from err


def _build(coordinator: TrueNASCoordinator) -> dict[str, Callable[[], Entity]]:
    hass = coordinator.hass
    out: dict[str, Callable[[], Entity]] = {
        "reboot": lambda: TrueNASButton(
            coordinator,
            "Reboot",
            "system_reboot",
            lambda b: _power(b, "system.reboot"),
            "mdi:restart",
            ButtonDeviceClass.RESTART,
        ),
        "shutdown": lambda: TrueNASButton(
            coordinator,
            "Shutdown",
            "system_shutdown",
            lambda b: _power(b, "system.shutdown"),
            "mdi:power",
        ),
        "power_on": lambda: TrueNASButton(
            coordinator,
            "Power on",
            "system_power_on",
            lambda b: actions.wake(hass, b.coordinator.config_entry),
            "mdi:power-on",
            always_available=True,
        ),
        "smart_short": lambda: TrueNASButton(
            coordinator,
            "SMART short test",
            "system_smart_short",
            lambda b: actions.start_smart_test(b.coordinator, "SHORT"),
            "mdi:harddisk-plus",
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        "smart_long": lambda: TrueNASButton(
            coordinator,
            "SMART long test",
            "system_smart_long",
            lambda b: actions.start_smart_test(b.coordinator, "LONG"),
            "mdi:harddisk-plus",
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        "config_backup": lambda: TrueNASButton(
            coordinator,
            "Config backup",
            "system_config_backup",
            lambda b: actions.config_backup(hass, b.coordinator, False),
            "mdi:content-save-cog",
            entity_category=EntityCategory.CONFIG,
        ),
    }
    for pool_name in coordinator.data.pools:
        out[f"scrub_{pool_name}"] = lambda n=pool_name: TrueNASButton(
            coordinator,
            f"{n} start scrub",
            f"pool_{n}_scrub_start",
            lambda b: actions.start_scrub(b.coordinator, n),
            "mdi:magnify-scan",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
    return out


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrueNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the buttons."""
    add_dynamic_entities(entry, async_add_entities, _build)
