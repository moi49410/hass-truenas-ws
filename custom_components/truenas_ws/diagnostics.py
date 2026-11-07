"""Diagnostics: what the integration reads from the NAS (secrets redacted)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant

from .coordinator import TrueNASConfigEntry

TO_REDACT = {
    CONF_API_KEY,
    CONF_HOST,
    "serial",
    "system_serial",
    "hostname",
    "identifier",
    "zfs_guid",
    "guid",
    "link_address",
    "link_address_b",
    "permanent_link_address",
    "aliases",
    "ipv4_dhcp",
    "credentials",
    "attributes",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TrueNASConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "connected": coordinator.client.connected,
        "data": async_redact_data(asdict(coordinator.data), TO_REDACT),
    }
