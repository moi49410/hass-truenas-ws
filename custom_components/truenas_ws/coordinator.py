"""Data coordinator for the TrueNAS SCALE (WebSocket) integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import re
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
    APP_STATS_INTERVAL,
    APP_UPGRADE_SUMMARY_INTERVAL,
    CONF_FILTER_TEMPS,
    CONF_MAC,
    CONF_SCAN_INTERVAL,
    DEFAULT_FILTER_TEMPS,
    DEFAULT_SCAN_INTERVAL,
    DISK_TEMP_INTERVAL,
    DOMAIN,
    HIDDEN_DATASET_PARTS,
    EVENT_ALERT,
    PHYSICAL_IFACE_TYPES,
    SPURIOUS_TEMP_MODELS,
    SPURIOUS_TEMP_RANGE,
    TOPOLOGY_ROLES,
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
    alerts: list[dict[str, Any]] = field(default_factory=list)
    app_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    smb_shares: dict[str, dict[str, Any]] = field(default_factory=dict)
    # disk name -> role, vdev, pool, ZFS status and error counters
    disk_health: dict[str, dict[str, Any]] = field(default_factory=dict)
    # pool name -> {role: ["RAIDZ1 (sdb, sdc, sdd)", ...]}
    topology: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # disk name -> {"raw": last raw reading, "ignored": count of ignored readings}
    temp_filter: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Enabled boot-time scripts that turn Wake-on-LAN on (no command text kept)
    wol_scripts: list[dict[str, Any]] | None = None


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
        self._app_stats: dict[str, dict[str, Any]] = {}
        self._good_temps: dict[str, float] = {}
        self._temp_filter: dict[str, dict[str, Any]] = {}
        self._seen_alerts: set[str] | None = None

    # ------------------------------------------------------------------ #
    async def async_shutdown(self) -> None:
        """Close the WebSocket on unload."""
        await super().async_shutdown()
        await self.client.close()

    def _on_realtime(self, _collection: str, fields: dict[str, Any]) -> None:
        if fields:
            self._realtime = fields

    def _on_app_stats(self, _collection: str, fields: Any) -> None:
        if isinstance(fields, list):
            self._app_stats = {
                item["app_name"]: item
                for item in fields
                if isinstance(item, dict) and item.get("app_name")
            }

    def _on_alert(self, _collection: str, _fields: Any) -> None:
        """New or cleared TrueNAS alert: refresh at once instead of waiting."""
        self.hass.async_create_task(self.async_request_refresh())

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
            for name, callback in (
                ("reporting.realtime", self._on_realtime),
                (f'app.stats:{{"interval": {APP_STATS_INTERVAL}}}', self._on_app_stats),
                ("alert.list", self._on_alert),
            ):
                try:
                    await client.subscribe(name, callback)
                except TrueNASError as err:
                    _LOGGER.warning("Cannot subscribe to %s: %s", name, err)
            self._subscribed = True

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
        # "extra.pools" fills the "pool" field (empty by default since 24.10).
        disks = await self._optional("disk.query", [[], {"extra": {"pools": True}}])
        if disks is None:
            disks = await self._optional("disk.query")
        for disk in disks or []:
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
            self._disk_temps = self._filter_temps(_parse_temps(temps or {}), data)
            self._last_disk_temps = now
        data.disk_temps = self._disk_temps
        data.temp_filter = self._temp_filter

        # Pool topology and per-disk ZFS health --------------------------- #
        # Join on the ZFS guid: device letters (sda, sdb...) can differ
        # between disk.query and the live pool topology after a reboot.
        guid_names = {
            str(d["zfs_guid"]): d["name"]
            for d in data.disks.values()
            if d.get("zfs_guid")
        }
        for pool_name, pool in data.pools.items():
            roles, health = parse_topology(pool, guid_names)
            data.topology[pool_name] = roles
            pool["_topology_summary"] = {
                role: ", ".join(vdevs) for role, vdevs in roles.items()
            }
            for disk_name, info in health.items():
                data.disk_health[disk_name] = {"pool": pool_name, **info}

        # Interfaces --------------------------------------------------- #
        for iface in await self._optional("interface.query") or []:
            if iface.get("type") in PHYSICAL_IFACE_TYPES and iface.get("name"):
                data.interfaces[iface["name"]] = iface
        self._remember_mac(data)

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
        data.app_stats = dict(self._app_stats)

        # SMB shares --------------------------------------------------- #
        for share in await self._optional("sharing.smb.query") or []:
            if share.get("id") is not None:
                data.smb_shares[str(share["id"])] = share

        # Wake-on-LAN at boot (init scripts running "ethtool ... wol g") -- #
        scripts = await self._optional("initshutdownscript.query")
        if scripts is not None:
            data.wol_scripts = [
                {
                    "id": sc.get("id"),
                    "when": sc.get("when"),
                    "comment": sc.get("comment") or None,
                }
                for sc in scripts
                if sc.get("enabled")
                and sc.get("when") in ("PREINIT", "POSTINIT")
                and _is_wol_command(str(sc.get("command") or sc.get("script") or ""))
            ]

        # Alerts ------------------------------------------------------- #
        alerts = await self._optional("alert.list")
        data.alerts = [a for a in alerts or [] if isinstance(a, dict)]
        self._fire_new_alerts(data.alerts)

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

    def _filter_temps(
        self, temps: dict[str, float | None], data: TrueNASData
    ) -> dict[str, float | None]:
        """Ignore known firmware glitches (exactly 84 °C on some NVMe SSDs)."""
        enabled = self.config_entry.options.get(CONF_FILTER_TEMPS, DEFAULT_FILTER_TEMPS)
        models = {d["name"]: str(d.get("model") or "") for d in data.disks.values()}
        out: dict[str, float | None] = {}
        low, high = SPURIOUS_TEMP_RANGE
        for name, value in temps.items():
            info = self._temp_filter.setdefault(name, {"raw": None, "ignored": 0})
            info["raw"] = value
            glitch = (
                enabled
                and value is not None
                and low <= value <= high
                and any(m in models.get(name, "") for m in SPURIOUS_TEMP_MODELS)
            )
            if glitch:
                info["ignored"] += 1
                out[name] = self._good_temps.get(name)
                continue
            if value is not None:
                self._good_temps[name] = value
            out[name] = value
        return out

    def _remember_mac(self, data: TrueNASData) -> None:
        """Store the NAS MAC address so Wake-on-LAN works while it is off."""
        mac = None
        for iface in data.interfaces.values():
            state = iface.get("state") or {}
            addr = state.get("link_address") or iface.get("link_address")
            if addr and state.get("link_state") == "LINK_STATE_UP":
                mac = addr
                break
        entry = self.config_entry
        if mac and entry.data.get(CONF_MAC) != mac:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_MAC: mac}
            )

    def _fire_new_alerts(self, alerts: list[dict[str, Any]]) -> None:
        """Fire truenas_ws_alert for alerts that appeared since the last poll."""
        active = {
            a.get("uuid") or a.get("id"): a for a in alerts if not a.get("dismissed")
        }
        if self._seen_alerts is not None:
            for uid, alert in active.items():
                if uid and uid not in self._seen_alerts:
                    self.hass.bus.async_fire(
                        EVENT_ALERT,
                        {
                            "uuid": uid,
                            "level": alert.get("level"),
                            "klass": alert.get("klass"),
                            "text": alert_text(alert),
                            "datetime": str(parse_date(alert.get("datetime")) or ""),
                        },
                    )
        self._seen_alerts = {uid for uid in active if uid}

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


_TAG_RE = re.compile(r"<[^>]+>")


def alert_text(alert: dict[str, Any]) -> str:
    """Plain text of a TrueNAS alert."""
    text = alert.get("formatted") or alert.get("text") or alert.get("klass") or ""
    return _TAG_RE.sub("", str(text)).strip()


def disk_base_name(name: str | None) -> str | None:
    """'sdb1' -> 'sdb', 'nvme0n1p2' -> 'nvme0n1', '/dev/sdc' -> 'sdc'."""
    if not name:
        return None
    name = name.rsplit("/", 1)[-1]
    if name.startswith("nvme"):
        return re.sub(r"p\d+$", "", name)
    return re.sub(r"(?<=[a-z])\d+$", "", name)


def _vdev_leaves(vdev: dict[str, Any]) -> list[dict[str, Any]]:
    children = vdev.get("children") or []
    if not children:
        return [vdev]
    leaves: list[dict[str, Any]] = []
    for child in children:
        leaves.extend(_vdev_leaves(child))
    return leaves


def parse_topology(
    pool: dict[str, Any], guid_names: dict[str, str] | None = None
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    """Return ({role: [vdev descriptions]}, {disk: health}) for one pool.

    ``guid_names`` maps ZFS vdev guids to disk names (from disk.query) and
    takes precedence over the device name reported in the topology.
    """
    guid_names = guid_names or {}
    topology = pool.get("topology") or {}
    roles: dict[str, list[str]] = {}
    health: dict[str, dict[str, Any]] = {}
    for role in TOPOLOGY_ROLES:
        for vdev in topology.get(role) or []:
            leaves = _vdev_leaves(vdev)
            names = [
                guid_names.get(str(leaf.get("guid")))
                or disk_base_name(
                    leaf.get("disk") or leaf.get("device") or leaf.get("path")
                )
                for leaf in leaves
            ]
            vtype = str(vdev.get("type") or "DISK")
            desc = f"{vtype} ({', '.join(n for n in names if n)})"
            roles.setdefault(role, []).append(desc)
            for leaf, name in zip(leaves, names, strict=True):
                if not name:
                    continue
                stats = leaf.get("stats") or {}
                health[name] = {
                    "role": role,
                    "vdev": desc,
                    "status": leaf.get("status"),
                    "read_errors": int(stats.get("read_errors") or 0),
                    "write_errors": int(stats.get("write_errors") or 0),
                    "checksum_errors": int(stats.get("checksum_errors") or 0),
                }
    return roles, health


_WOL_RE = re.compile(r"ethtool\s+(?:-s|--change)\s+\S+.*\bwol\s+\S*g", re.IGNORECASE)


def _is_wol_command(command: str) -> bool:
    """True for commands like 'ethtool -s eth0 wol g'."""
    return bool(_WOL_RE.search(command))
