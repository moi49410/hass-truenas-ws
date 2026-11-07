"""TrueNAS SCALE integration using the JSON-RPC 2.0 WebSocket API."""

from __future__ import annotations

from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_HOST, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .api import (
    TrueNASAuthError,
    TrueNASCallError,
    TrueNASClient,
    TrueNASConnectionError,
    TrueNASError,
)
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_NAME,
    ATTR_RECURSIVE,
    ATTR_SNAPSHOT_NAME,
    ATTR_WAIT,
    CONF_VERIFY_SSL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    POOL_JOB_TIMEOUT,
    PROTECTED_POOLS,
    REPLICATION_TIMEOUT,
    SERVICE_APP_RESTART,
    SERVICE_APP_START,
    SERVICE_APP_STOP,
    SERVICE_CLOUDSYNC_RUN,
    SERVICE_POOL_EXPORT,
    SERVICE_POOL_IMPORT,
    SERVICE_REPLICATION_RUN,
    SERVICE_SERVICE_RESTART,
    SERVICE_SERVICE_START,
    SERVICE_SERVICE_STOP,
    SERVICE_SNAPSHOT_CREATE,
    SERVICE_SYSTEM_REBOOT,
    SERVICE_SYSTEM_SHUTDOWN,
)
from .coordinator import TrueNASConfigEntry, TrueNASCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_ENTRY = vol.Optional(ATTR_CONFIG_ENTRY_ID)
NAME_SCHEMA = vol.Schema({vol.Required(ATTR_NAME): cv.string, _ENTRY: cv.string})
ENTRY_SCHEMA = vol.Schema({_ENTRY: cv.string})
REPLICATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_WAIT, default=False): cv.boolean,
        _ENTRY: cv.string,
    }
)
SNAPSHOT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_SNAPSHOT_NAME): cv.string,
        vol.Optional(ATTR_RECURSIVE, default=False): cv.boolean,
        _ENTRY: cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration actions."""
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TrueNASConfigEntry) -> bool:
    """Connect to the NAS and set up the platforms."""
    session = async_get_clientsession(
        hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
    )
    client = TrueNASClient(
        session,
        entry.data[CONF_HOST],
        entry.data[CONF_API_KEY],
        entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
    )
    try:
        await client.connect()
    except TrueNASAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except TrueNASConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = TrueNASCoordinator(hass, entry, client)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.close()
        raise
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TrueNASConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.close()
    return unloaded


# ---------------------------------------------------------------------- #
# Actions
# ---------------------------------------------------------------------- #
def _coordinator(hass: HomeAssistant, call: ServiceCall) -> TrueNASCoordinator:
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]
    wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if wanted:
        entries = [e for e in entries if e.entry_id == wanted]
    if not entries:
        raise ServiceValidationError("No loaded TrueNAS entry matches this call")
    if len(entries) > 1:
        raise ServiceValidationError(
            "Several TrueNAS systems are configured: set config_entry_id"
        )
    return entries[0].runtime_data


def _find_id(items: dict[str, dict[str, Any]], name: str, *keys: str) -> int:
    for task_id, item in items.items():
        if task_id == name or any(item.get(k) == name for k in keys):
            return int(task_id)
    raise ServiceValidationError(f"No task named '{name}' on the NAS")


async def _run(coordinator: TrueNASCoordinator, method: str, params: list[Any]) -> None:
    try:
        await coordinator.client.call(method, params)
    except TrueNASError as err:
        raise HomeAssistantError(f"{method} failed: {err}") from err
    await coordinator.async_request_refresh()


def _register_services(hass: HomeAssistant) -> None:
    async def service_control(verb: str, call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        name = call.data[ATTR_NAME]
        if name not in coordinator.data.services:
            raise ServiceValidationError(f"Unknown TrueNAS service '{name}'")
        try:
            await coordinator.client.call_first(
                [
                    ("service.control", [verb.upper(), name, {}]),
                    (f"service.{verb}", [name, {}]),
                ]
            )
        except TrueNASError as err:
            raise HomeAssistantError(f"Cannot {verb} {name}: {err}") from err
        await coordinator.async_request_refresh()

    async def app_control(method: str, call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        name = call.data[ATTR_NAME]
        if name not in coordinator.data.apps:
            raise ServiceValidationError(f"Unknown TrueNAS app '{name}'")
        await _run(coordinator, method, [name])

    async def cloudsync_run(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        task_id = _find_id(
            coordinator.data.cloudsyncs, call.data[ATTR_NAME], "description"
        )
        await _run(coordinator, "cloudsync.sync", [task_id])

    async def replication_run(call: ServiceCall) -> ServiceResponse:
        """Start a replication; with wait, return its outcome when it ends."""
        coordinator = _coordinator(hass, call)
        task_id = _find_id(coordinator.data.replications, call.data[ATTR_NAME], "name")
        if not call.data[ATTR_WAIT]:
            await _run(coordinator, "replication.run", [task_id])
            return {"started": True}
        try:
            await coordinator.client.call_job(
                "replication.run", [task_id], timeout=REPLICATION_TIMEOUT
            )
            result: dict[str, Any] = {"success": True, "error": None}
        except TrueNASCallError as err:  # refused, or the job failed
            result = {"success": False, "error": err.reason}
        except TimeoutError:
            result = {"success": False, "error": "timeout"}
        except TrueNASError as err:  # connection lost
            raise HomeAssistantError(f"replication.run failed: {err}") from err
        await coordinator.async_request_refresh()
        return result

    async def pool_lock_state(coordinator: TrueNASCoordinator, pool: str) -> dict:
        """Whether any dataset of the pool is encrypted / still locked."""
        try:
            datasets = await coordinator.client.call(
                "pool.dataset.query", [[["pool", "=", pool]], {"extra": {"flat": True}}]
            )
        except TrueNASError:
            return {"encrypted": None, "locked": None}
        datasets = datasets or []
        return {
            "encrypted": any(bool(d.get("encrypted")) for d in datasets),
            "locked": any(bool(d.get("locked")) for d in datasets),
        }

    async def pool_import(call: ServiceCall) -> ServiceResponse:
        """Import an exported pool (e.g. an external backup disk) by name."""
        coordinator = _coordinator(hass, call)
        name = call.data[ATTR_NAME]
        if name in coordinator.data.pools:
            return {
                "imported": True,
                "already_imported": True,
                **await pool_lock_state(coordinator, name),
            }
        try:
            found = await coordinator.client.call_job(
                "pool.import_find", [], timeout=POOL_JOB_TIMEOUT
            )
            match = next((p for p in found or [] if p.get("name") == name), None)
            if match is None:
                raise HomeAssistantError(
                    f"Pool '{name}' is not among the pools TrueNAS can import "
                    "(disk not detected yet?)"
                )
            await coordinator.client.call_job(
                "pool.import_pool",
                [{"guid": str(match["guid"])}],
                timeout=POOL_JOB_TIMEOUT,
            )
        except (TrueNASError, TimeoutError) as err:
            raise HomeAssistantError(f"Import of pool '{name}' failed: {err}") from err
        await coordinator.async_request_refresh()
        return {
            "imported": True,
            "already_imported": False,
            **await pool_lock_state(coordinator, name),
        }

    async def pool_export(call: ServiceCall) -> None:
        """Export (disconnect) a pool so its disks can be unplugged. Never destroys data."""
        coordinator = _coordinator(hass, call)
        name = call.data[ATTR_NAME]
        if name in PROTECTED_POOLS:
            raise ServiceValidationError(f"The pool '{name}' cannot be exported")
        pool = coordinator.data.pools.get(name)
        if pool is None or "id" not in pool:
            raise ServiceValidationError(f"Pool '{name}' is not imported")
        options = {"cascade": False, "restart_services": False, "destroy": False}
        try:
            await coordinator.client.call_job(
                "pool.export", [pool["id"], options], timeout=POOL_JOB_TIMEOUT
            )
        except (TrueNASError, TimeoutError) as err:
            raise HomeAssistantError(f"Export of pool '{name}' failed: {err}") from err
        await coordinator.async_request_refresh()

    async def snapshot_create(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        dataset = call.data[ATTR_NAME]
        if dataset not in coordinator.data.datasets:
            raise ServiceValidationError(f"Unknown dataset '{dataset}'")
        snap = call.data.get(ATTR_SNAPSHOT_NAME) or dt_util.now().strftime(
            "homeassistant-%Y%m%d-%H%M%S"
        )
        payload = {
            "dataset": dataset,
            "name": snap,
            "recursive": call.data[ATTR_RECURSIVE],
        }
        try:
            await coordinator.client.call_first(
                [
                    ("pool.snapshot.create", [payload]),  # 25.10+
                    ("zfs.snapshot.create", [payload]),  # 25.04 and older
                ]
            )
        except TrueNASError as err:
            raise HomeAssistantError(f"Snapshot failed: {err}") from err

    async def power(method: str, call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        await _run(coordinator, method, ["Requested from Home Assistant"])

    for verb, service in (
        ("start", SERVICE_SERVICE_START),
        ("stop", SERVICE_SERVICE_STOP),
        ("restart", SERVICE_SERVICE_RESTART),
    ):
        hass.services.async_register(
            DOMAIN, service, partial(service_control, verb), NAME_SCHEMA
        )
    for method, service in (
        ("app.start", SERVICE_APP_START),
        ("app.stop", SERVICE_APP_STOP),
        ("app.redeploy", SERVICE_APP_RESTART),
    ):
        hass.services.async_register(
            DOMAIN, service, partial(app_control, method), NAME_SCHEMA
        )
    hass.services.async_register(
        DOMAIN, SERVICE_CLOUDSYNC_RUN, cloudsync_run, NAME_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REPLICATION_RUN,
        replication_run,
        REPLICATION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_POOL_IMPORT,
        pool_import,
        NAME_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, SERVICE_POOL_EXPORT, pool_export, NAME_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_SNAPSHOT_CREATE, snapshot_create, SNAPSHOT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SYSTEM_REBOOT, partial(power, "system.reboot"), ENTRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SYSTEM_SHUTDOWN, partial(power, "system.shutdown"), ENTRY_SCHEMA
    )
