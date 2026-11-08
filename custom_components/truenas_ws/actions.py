"""Operations shared by buttons and actions (services)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from pathlib import Path
import re
import socket
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import TrueNASError
from .const import (
    CONF_MAC,
    CONFIG_BACKUP_DIR,
    CONFIG_BACKUP_KEEP,
    POOL_JOB_TIMEOUT,
)
from .coordinator import TrueNASCoordinator

_LOGGER = logging.getLogger(__name__)

WOL_PORTS = (9, 7)
SMART_TYPES = ("SHORT", "LONG")


async def start_scrub(coordinator: TrueNASCoordinator, pool_name: str) -> None:
    """Start a scrub now (threshold 0 = even if one ran recently)."""
    pool = coordinator.data.pools.get(pool_name)
    if pool is None:
        raise ServiceValidationError(f"Unknown pool '{pool_name}'")
    candidates: list[tuple[str, list[Any] | None]] = [
        ("pool.scrub.run", [pool_name, 0])
    ]
    if pool.get("id") is not None:
        candidates.append(("pool.scrub.scrub", [pool["id"], "START"]))
    try:
        await coordinator.client.call_first(candidates)
    except TrueNASError as err:
        raise HomeAssistantError(f"Cannot start scrub of {pool_name}: {err}") from err
    await coordinator.async_request_refresh()


async def start_smart_test(
    coordinator: TrueNASCoordinator, test_type: str, disks: list[str] | None = None
) -> list[str]:
    """Start a SMART self-test on the given disks (default: every disk)."""
    test_type = test_type.upper()
    if test_type not in SMART_TYPES:
        raise ServiceValidationError(f"SMART test type must be one of {SMART_TYPES}")
    known = sorted(d["name"] for d in coordinator.data.disks.values())
    targets = disks or known
    unknown = [d for d in targets if d not in known]
    if unknown:
        raise ServiceValidationError(f"Unknown disk(s): {', '.join(unknown)}")
    try:
        await coordinator.client.call("disk.smart_test", [test_type, targets])
    except TrueNASError as err:
        raise HomeAssistantError(f"Cannot start SMART test: {err}") from err
    return targets


def _magic_packet(mac: str) -> bytes:
    digits = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(digits) != 12:
        raise ServiceValidationError(f"Invalid MAC address '{mac}'")
    return b"\xff" * 6 + bytes.fromhex(digits) * 16


def _broadcasts(host: str) -> list[str]:
    targets = ["255.255.255.255"]
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return targets
    if addr.version == 4:
        # Assume a /24 home network; the global broadcast covers the rest.
        targets.append(
            str(ipaddress.ip_network(f"{addr}/24", strict=False).broadcast_address)
        )
    return targets


def _send_wol(packet: bytes, targets: list[str]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in targets:
            for port in WOL_PORTS:
                sock.sendto(packet, (target, port))


async def wake(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Send a Wake-on-LAN magic packet to the NAS (works while it is off)."""
    mac = entry.data.get(CONF_MAC)
    if not mac:
        raise HomeAssistantError(
            "The NAS MAC address is not known yet (it is learned while the NAS is on)"
        )
    packet = _magic_packet(mac)
    host = str(entry.data.get(CONF_HOST, "")).strip()
    for prefix in ("https://", "http://", "wss://"):
        host = host.removeprefix(prefix)
    host = host.split("/", 1)[0].rsplit(":", 1)[0].strip("[]")
    await hass.async_add_executor_job(_send_wol, packet, _broadcasts(host))
    return mac


def _write_backup(folder: Path, name: str, content: bytes, keep: int) -> Path:
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / name
    path.write_bytes(content)
    path.chmod(0o600)
    backups = sorted(folder.glob("truenas-config-*"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-keep]:
        old.unlink(missing_ok=True)
    return path


async def config_backup(
    hass: HomeAssistant, coordinator: TrueNASCoordinator, include_secrets: bool
) -> dict[str, Any]:
    """Download the TrueNAS configuration into <config>/truenas_backups."""
    client = coordinator.client
    try:
        job_id, url = await client.call(
            "core.download",
            ["config.save", [{"secretseed": include_secrets}], "config", True],
        )
        await client.wait_job(job_id, "config.save", POOL_JOB_TIMEOUT)
    except (TrueNASError, TimeoutError, TypeError, ValueError) as err:
        raise HomeAssistantError(f"TrueNAS configuration backup failed: {err}") from err

    session = async_get_clientsession(hass, verify_ssl=client.verify_ssl)
    try:
        async with asyncio.timeout(120):
            resp = await session.get(f"{client.base_url}{url}", ssl=client.verify_ssl)
            resp.raise_for_status()
            content = await resp.read()
    except Exception as err:  # noqa: BLE001 - network errors of any kind
        raise HomeAssistantError(
            f"Download of the configuration failed: {err}"
        ) from err
    if not content:
        raise HomeAssistantError("TrueNAS returned an empty configuration file")

    host = re.sub(
        r"[^A-Za-z0-9_-]", "_", str(coordinator.data.system.get("hostname") or "nas")
    )
    stamp = dt_util.now().strftime("%Y%m%d-%H%M%S")
    ext = "tar" if include_secrets else "db"
    name = f"truenas-config-{host}-{stamp}{'-secrets' if include_secrets else ''}.{ext}"
    path = await hass.async_add_executor_job(
        _write_backup,
        Path(hass.config.path(CONFIG_BACKUP_DIR)),
        name,
        content,
        CONFIG_BACKUP_KEEP,
    )
    _LOGGER.info("TrueNAS configuration saved to %s", path)
    return {"path": str(path), "size": len(content), "include_secrets": include_secrets}
