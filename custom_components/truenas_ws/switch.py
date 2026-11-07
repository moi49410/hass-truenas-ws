"""Switches to start and stop TrueNAS apps."""

from __future__ import annotations

from collections.abc import Callable
import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import TrueNASError
from .const import DEV_APPS
from .coordinator import TrueNASConfigEntry, TrueNASCoordinator
from .entity import TrueNASEntity, add_dynamic_entities

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

# States in which an app counts as "on" (TrueNAS app.query "state").
APP_ON_STATES = {"RUNNING", "DEPLOYING"}
# How long the requested state is shown while TrueNAS catches up.
PENDING_TIMEOUT = 180  # seconds


class TrueNASAppSwitch(TrueNASEntity, SwitchEntity):
    """Start (on) or stop (off) one TrueNAS app."""

    _attr_icon = "mdi:application-cog"

    def __init__(self, coordinator: TrueNASCoordinator, app_name: str) -> None:
        """Create switch.truenas_apps_<app>."""
        super().__init__(
            coordinator, "switch", DEV_APPS, app_name, f"app_switch_{app_name}"
        )
        self._app = app_name
        self._pending: bool | None = None
        self._pending_since = 0.0

    @property
    def available(self) -> bool:
        """Unavailable when the app is removed."""
        return super().available and self._app in self.data.apps

    @property
    def is_on(self) -> bool | None:
        """On while the app runs (or while a start we asked for is pending)."""
        if self._pending is not None:
            return self._pending
        state = (self.data.apps.get(self._app) or {}).get("state")
        return state in APP_ON_STATES if state else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw TrueNAS state (RUNNING, STOPPED, DEPLOYING, CRASHED...)."""
        return {"State": (self.data.apps.get(self._app) or {}).get("state")}

    @callback
    def _handle_coordinator_update(self) -> None:
        """Drop the requested state once TrueNAS reports it (or after a timeout)."""
        if self._pending is not None:
            state = (self.data.apps.get(self._app) or {}).get("state")
            reached = state is not None and (state == "RUNNING") == self._pending
            if reached or time.monotonic() - self._pending_since > PENDING_TIMEOUT:
                self._pending = None
        super()._handle_coordinator_update()

    async def _control(self, method: str, target: bool) -> None:
        try:
            # Jobs: TrueNAS returns at once, the app starts/stops in the background.
            await self.coordinator.client.call(method, [self._app])
        except TrueNASError as err:
            raise HomeAssistantError(f"{method} {self._app} failed: {err}") from err
        self._pending = target
        self._pending_since = time.monotonic()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the app."""
        await self._control("app.start", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the app."""
        await self._control("app.stop", False)


def _build(coordinator: TrueNASCoordinator) -> dict[str, Callable[[], Entity]]:
    return {
        f"app_{name}": (lambda n=name: TrueNASAppSwitch(coordinator, n))
        for name in coordinator.data.apps
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrueNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the app switches."""
    add_dynamic_entities(entry, async_add_entities, _build)
