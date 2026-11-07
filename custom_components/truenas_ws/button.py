"""Buttons to reboot or shut down the NAS."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import TrueNASError
from .const import DEV_SYSTEM
from .coordinator import TrueNASConfigEntry, TrueNASCoordinator
from .entity import TrueNASEntity

PARALLEL_UPDATES = 1

REASON = "Requested from Home Assistant"


class TrueNASPowerButton(TrueNASEntity, ButtonEntity):
    """Reboot (button.truenas_system_reboot) or shut down the NAS."""

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        label: str,
        method: str,
        icon: str,
        device_class: ButtonDeviceClass | None,
    ) -> None:
        """Create the button."""
        super().__init__(
            coordinator, "button", DEV_SYSTEM, label, f"system_{label.lower()}"
        )
        self._method = method
        self._attr_icon = icon
        self._attr_device_class = device_class

    async def async_press(self) -> None:
        """Send the order to the NAS."""
        try:
            await self.coordinator.client.call(self._method, [REASON])
        except TrueNASError as err:
            raise HomeAssistantError(f"{self._method} failed: {err}") from err


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrueNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the power buttons."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            TrueNASPowerButton(
                coordinator,
                "Reboot",
                "system.reboot",
                "mdi:restart",
                ButtonDeviceClass.RESTART,
            ),
            TrueNASPowerButton(
                coordinator, "Shutdown", "system.shutdown", "mdi:power", None
            ),
        ]
    )
