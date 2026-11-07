"""Data coordinator for the TrueNAS SCALE (WebSocket) integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    TrueNASAuthError,
    TrueNASClient,
    TrueNASConnectionError,
    TrueNASError,
    TrueNASMethodNotFound,
)
from .const import (
    APP_UPGRADE_SUMMARY_INTERVAL,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DISK_TEMP_INTERVAL,
    DOMAIN,
    HIDDEN_DATASET_PARTS,
    PHYSICAL_IFACE_TYPES,
    UPDATE_CHECK_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

type TrueNASConfigEntry = ConfigEntry[TrueNASCoordinator]


@dataclass
class TrueNASData:
    """Snapshot of everything read from the NAS."""

    system: dict[str, Any] = field(default_factory=dict)
    realtime: dict[str, Any] = field(default_factory=dict)
    pools: dict[str, dict[str, Any]] = field(default_factory=dict)
    datasets: dict[str, dict[str, Any]] = field(default_factory=dict)
    disks: dict[str, dict[str, Any]] = field(default_factory=dict)
    disk_temps: dict[str, float | None] = field(default_factory=dict)
    interfaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    apps: dict[str, dict[str, Any]] = field(default_factory=dict)
    replications: dict[str, dict[str, Any]] = field(default_factory=dict)
    cloudsyncs: dict[str, dict[str, Any]] = field(default_factory=dict)
    snapshot_tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    update: dict[str, Any] = field(default_factory=dict)


def parse_date(value: Any) -> datetime | None:
    """Convert a TrueNAS date ({"$date": ms}, epoch, ISO string) to a datetime."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("$date", value.get("date"))
    if isinstance(value, int | float):
        # Milliseconds since epoch when large, seconds otherwise.
        ts = value / 1000 if value > 1e11 else value
        return dt_util.utc_from_timestamp(ts)
    if isinstance(value, str):
        return dt_util.parse_datetime(value)
    return None


def prop(value: Any) -> Any:
    """Return the useful value of a ZFS property dict ({"value", "parsed"...})."""
    if isinstance(value, dict):
        if "parsed" in value and value["parsed"] is not None:
            return value["parsed"]
        return value.get("value")
    return value


def clean_version(version: str | None) -> str | None:
    """Turn 'TrueNAS-SCALE-25.10.1' into '25.10.1'."""
    if not version:
        return None
    for prefix in ("TrueNAS-SCALE-", "TrueNAS-", "TrueNAS SCALE "):
        version = version.removeprefix(prefix)
    return version


def _job_state(job: Any) -> str | None:
    if isinstance(job, dict):
        return job.get("state")
    return None


class TrueNASCoordinator(DataUpdateCoordinator[TrueNASData]):
    """Poll the NAS and keep a live reporting.realtime feed."""

    config_entry: TrueNASConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: TrueNASConfigEntry, client: TrueNASClient
    ) -> None:
        """Set up the coordinator."""
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self._realtime: dict[str, Any] = {}
        self._subscribed = False
        self._last_disk_temps: datetime | None = None
        self._last_update_check: datetime | None = None
        self._last_upgrade_summary: datetime | None = None
        self._disk_temps: dict[str, float | None] = {}
        self._update: dict[str, Any] = {}
        self._upgrade_summaries: dict[str, dict[str, Any]] = {}
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ #
    async def async_shutdown(self) -> None:
        """Close the WebSocket on unload."""
        await super().async_shutdown()
        await self.client.close()

    def _on_realtime(self, _collection: str, fields: dict[str, Any]) -> None:
        if fields:
            self._realtime = fields

    async def _optional(self, method: str, params: list[Any] | None = None) -> Any:
        """Call a method whose absence or failure must not break the update."""
        try:
            return await self.client.call(method, params)
        except TrueNASMethodNotFound:
            if method not in self._warned:
                self._warned.add(method)
                _LOGGER.info("%s is not available on this TrueNAS version", method)
        except (TrueNASConnectionError, TrueNASAuthError):
            raise
        except TrueNASError as err:
            if method not in self._warned:
                self._warned.add(method)
                _LOGGER.warning("%s failed: %s", method, err)
        return None

    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> TrueNASData:
        try:
            return await self._fetch()
        except TrueNASAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TrueNASConnectionError as err:
            raise UpdateFailed(f"TrueNAS unreachable: {err}") from err
        except TrueNASError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch(self) -> TrueNASData:
        client = self.client
        if not client.connected:
            # connect() also restores the event subscriptions.
            await client.connect()
        if not self._subscribed:
            try:
                await client.subscribe("reporting.realtime", self._on_realtime)
                self._subscribed = True
            except TrueNASError as err:
                _LOGGER.warning("Live statistics unavailable: %s", err)

        data = TrueNASData()
        now = dt_util.utcnow()

        # System ------------------------------------------------------- #
        info = await client.call("system.info") or {}
        data.system = dict(info)
        data.system["version_clean"] = clean_version(info.get("version"))
        uptime = info.get("uptime_seconds")
        if isinstance(uptime, int | float):
            boot = now - timedelta(seconds=uptime)
            data.system["boot_time"] = boot.replace(second=0, microsecond=0)
        data.realtime = self._realtime

        # Pools -------------------------------------------------------- #
        for pool in await self._optional("pool.query") or []:
            data.pools[pool["name"]] = pool
        boot_state = await self._optional("boot.get_state")
        if isinstance(boot_state, dict) and boot_state.get("name"):
            data.pools.setdefault(boot_state["name"], boot_state)

        # Datasets ----------------------------------------------------- #
        datasets = await self._optional(
            "pool.dataset.query",
            [[], {"extra": {"flat": True, "retrieve_children": True}}],
        )
        for ds in _flatten_datasets(datasets or []):
            name = ds.get("id") or ds.get("name")
            if not name or ds.get("type") not in (None, "FILESYSTEM"):
                continue
            if any(
                part.startswith(HIDDEN_DATASET_PARTS) for part in name.split("/")[1:]
            ):
                continue
            data.datasets[name] = ds

        # Disks -------------------------------------------------------- #
        for disk in await self._optional("disk.query") or []:
            key = disk.get("identifier") or disk.get("serial") or disk.get("name")
            if key and disk.get("name"):
                data.disks[key] = disk
        if (
            self._last_disk_temps is None
            or now - self._last_disk_temps >= DISK_TEMP_INTERVAL
        ):
            names = [d["name"] for d in data.disks.values()]
            temps = (
                await self._optional("disk.temperatures", [names, False])
                if names
                else {}
            )
            self._disk_temps = _parse_temps(temps or {})
            self._last_disk_temps = now
        data.disk_temps = self._disk_temps

        # Interfaces --------------------------------------------------- #
        for iface in await self._optional("interface.query") or []:
            if iface.get("type") in PHYSICAL_IFACE_TYPES and iface.get("name"):
                data.interfaces[iface["name"]] = iface

        # Services ----------------------------------------------------- #
        for svc in await self._optional("service.query") or []:
            data.services[svc["service"]] = svc

        # Apps --------------------------------------------------------- #
        for app in await self._optional("app.query") or []:
            data.apps[app["name"]] = app
        if (
            self._last_upgrade_summary is None
            or now - self._last_upgrade_summary >= APP_UPGRADE_SUMMARY_INTERVAL
        ):
            self._upgrade_summaries = {}
            for name, app in data.apps.items():
                if app.get("upgrade_available"):
                    summary = await self._optional("app.upgrade_summary", [name])
                    if isinstance(summary, dict):
                        self._upgrade_summaries[name] = summary
            self._last_upgrade_summary = now
        for name, summary in self._upgrade_summaries.items():
            if name in data.apps:
                data.apps[name]["_upgrade_summary"] = summary

        # Data protection tasks --------------------------------------- #
        for rep in await self._optional("replication.query") or []:
            data.replications[str(rep["id"])] = rep
        for cs in await self._optional("cloudsync.query") or []:
            data.cloudsyncs[str(cs["id"])] = cs
        for task in await self._optional("pool.snapshottask.query") or []:
            data.snapshot_tasks[str(task["id"])] = task

        # System update ------------------------------------------------ #
        if (
            self._last_update_check is None
            or now - self._last_update_check >= UPDATE_CHECK_INTERVAL
        ):
            self._update = await self._fetch_update_status()
            self._last_update_check = now
        data.update = self._update

        return data

    async def _fetch_update_status(self) -> dict[str, Any]:
        """Normalise update.status (25.10+) and update.check_available (older)."""
        status = await self._optional("update.status")
        if isinstance(status, dict) and "code" in status:
            inner = status.get("status") or {}
            new = inner.get("new_version") or {}
            progress = status.get("update_download_progress") or {}
            return {
                "available": bool(new),
                "version": new.get("version"),
                "release_notes": new.get("release_notes"),
                "release_url": new.get("release_notes_url"),
                "progress": progress.get("percent"),
                "error": (status.get("error") or {}).get("reason"),
            }
        legacy = await self._optional("update.check_available")
        if isinstance(legacy, dict):
            return {
                "available": legacy.get("status") == "AVAILABLE",
                "version": legacy.get("version"),
                "release_notes": None,
                "release_url": None,
                "progress": None,
                "error": None,
            }
        return {}

    def refresh_update_soon(self) -> None:
        """Force the next poll to re-read the system update status."""
        self._last_update_check = None
        self._last_upgrade_summary = None


def _flatten_datasets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    stack = list(items)
    while stack:
        ds = stack.pop(0)
        name = ds.get("id") or ds.get("name")
        if name and name not in seen:
            seen.add(name)
            out.append(ds)
        stack.extend(ds.get("children") or [])
    return out


def _parse_temps(raw: Any) -> dict[str, float | None]:
    """disk.temperatures returns {name: temp} or {name: {"temp": ..}}."""
    out: dict[str, float | None] = {}
    if not isinstance(raw, dict):
        return out
    for name, item in raw.items():
        value = (
            item.get("temp", item.get("temperature"))
            if isinstance(item, dict)
            else item
        )
        out[name] = float(value) if isinstance(value, int | float) else None
    return out
