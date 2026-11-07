"""Update entities: TrueNAS system and apps."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import TrueNASError, TrueNASMethodNotFound
from .const import DEV_APPS, DEV_SYSTEM
from .coordinator import TrueNASConfigEntry, TrueNASCoordinator
from .entity import TrueNASEntity, add_dynamic_entities

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


class TrueNASSystemUpdate(TrueNASEntity, UpdateEntity):
    """TrueNAS SCALE system update (installs, then reboots the NAS)."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES
    )
    _attr_title = "TrueNAS SCALE"

    def __init__(self, coordinator: TrueNASCoordinator) -> None:
        """Create the entity: update.truenas_system, named 'System'."""
        super().__init__(coordinator, "update", DEV_SYSTEM, "", "system_update")
        self._attr_name = DEV_SYSTEM
        self.entity_id = "update.truenas_system"

    @property
    def installed_version(self) -> str | None:
        """Running version."""
        return self.data.system.get("version_clean")

    @property
    def latest_version(self) -> str | None:
        """Newest available version, or the installed one."""
        upd = self.data.update
        if upd.get("available") and upd.get("version"):
            return upd["version"]
        return self.installed_version

    @property
    def release_url(self) -> str | None:
        """Release notes link."""
        return self.data.update.get("release_url")

    async def async_release_notes(self) -> str | None:
        """Release notes text, when TrueNAS provides it."""
        return self.data.update.get("release_notes")

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Start the download/apply job; TrueNAS reboots itself when done."""
        client = self.coordinator.client
        try:
            try:
                await client.call_job("update.run", [{"reboot": True}], wait=False)
            except TrueNASMethodNotFound:
                # TrueNAS 25.04 and older
                await client.call_job("update.update", [{"reboot": True}], wait=False)
        except TrueNASError as err:
            raise HomeAssistantError(f"TrueNAS update failed to start: {err}") from err
        finally:
            self.coordinator.refresh_update_soon()
        _LOGGER.info("TrueNAS update started; the NAS will reboot when done")


class TrueNASAppUpdate(TrueNASEntity, UpdateEntity):
    """Update of one TrueNAS app."""

    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    def __init__(self, coordinator: TrueNASCoordinator, app_name: str) -> None:
        """Create update.truenas_apps_<app>."""
        super().__init__(
            coordinator, "update", DEV_APPS, app_name, f"app_update_{app_name}"
        )
        self._app = app_name
        self._attr_title = app_name
        self._installing = False

    @property
    def available(self) -> bool:
        """Unavailable when the app is removed."""
        return super().available and self._app in self.data.apps

    @property
    def installed_version(self) -> str | None:
        """Installed app version."""
        app = self.data.apps.get(self._app) or {}
        return app.get("human_version") or app.get("version")

    @property
    def latest_version(self) -> str | None:
        """Latest version when an upgrade is available."""
        app = self.data.apps.get(self._app) or {}
        if not app.get("upgrade_available"):
            return self.installed_version
        summary = app.get("_upgrade_summary") or {}
        return (
            summary.get("latest_human_version")
            or summary.get("latest_version")
            or f"{self.installed_version} (new)"
        )

    @property
    def in_progress(self) -> bool:
        """True while upgrading."""
        return self._installing

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Upgrade the app to the latest version."""
        self._installing = True
        self.async_write_ha_state()
        try:
            await self.coordinator.client.call_job(
                "app.upgrade", [self._app, {"app_version": "latest"}], timeout=1800
            )
        except TrueNASError as err:
            raise HomeAssistantError(f"Upgrade of {self._app} failed: {err}") from err
        finally:
            self._installing = False
            self.coordinator.refresh_update_soon()
            await self.coordinator.async_request_refresh()


def _build(coordinator: TrueNASCoordinator) -> dict[str, Callable[[], Entity]]:
    out: dict[str, Callable[[], Entity]] = {
        "system": lambda: TrueNASSystemUpdate(coordinator)
    }
    for app_name in coordinator.data.apps:
        out[f"app_{app_name}"] = lambda n=app_name: TrueNASAppUpdate(coordinator, n)
    return out


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrueNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the update entities."""
    add_dynamic_entities(entry, async_add_entities, _build)
