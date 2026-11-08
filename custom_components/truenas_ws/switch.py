"""Switches: start/stop TrueNAS apps, enable/disable SMB shares."""

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
from .const import DEV_APPS, DEV_SHARES
from .coordinator import TrueNASConfigEntry, TrueNASCoordinator
from .entity import TrueNASEntity, add_dynamic_entities, app_icon

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
    def entity_picture(self) -> str | None:
        """App icon from the TrueNAS catalog, when there is one."""
        return app_icon(self.data.apps.get(self._app))

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


class TrueNASShareSwitch(TrueNASEntity, SwitchEntity):
    """Enable (on) or disable (off) one SMB share."""

    _attr_icon = "mdi:folder-network"

    def __init__(self, coordinator: TrueNASCoordinator, share_id: str) -> None:
        """Create switch.truenas_shares_<name>."""
        share = coordinator.data.smb_shares[share_id]
        super().__init__(
            coordinator,
            "switch",
            DEV_SHARES,
            share.get("name") or f"share {share_id}",
            f"smb_share_{share_id}",
        )
        self._share_id = share_id

    @property
    def available(self) -> bool:
        """Unavailable when the share is deleted."""
        return super().available and self._share_id in self.data.smb_shares

    @property
    def is_on(self) -> bool | None:
        """On when the share is enabled."""
        share = self.data.smb_shares.get(self._share_id)
        return bool(share.get("enabled")) if share else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Path and options of the share."""
        share = self.data.smb_shares.get(self._share_id) or {}
        return {
            "Path": share.get("path"),
            "Comment": share.get("comment") or None,
            "Read only": share.get("readonly"),
            "Browsable": share.get("browsable"),
            "Locked": share.get("locked"),
        }

    async def _set(self, enabled: bool) -> None:
        try:
            await self.coordinator.client.call(
                "sharing.smb.update", [int(self._share_id), {"enabled": enabled}]
            )
        except TrueNASError as err:
            raise HomeAssistantError(f"Cannot update the SMB share: {err}") from err
        self.data.smb_shares[self._share_id]["enabled"] = enabled
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the share."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the share."""
        await self._set(False)


def _build(coordinator: TrueNASCoordinator) -> dict[str, Callable[[], Entity]]:
    out: dict[str, Callable[[], Entity]] = {
        f"app_{name}": (lambda n=name: TrueNASAppSwitch(coordinator, n))
        for name in coordinator.data.apps
    }
    for share_id in coordinator.data.smb_shares:
        out[f"smb_{share_id}"] = lambda i=share_id: TrueNASShareSwitch(coordinator, i)
    return out


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrueNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the app and share switches."""
    add_dynamic_entities(entry, async_add_entities, _build)
