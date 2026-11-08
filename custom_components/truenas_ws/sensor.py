"""Sensors for the TrueNAS SCALE (WebSocket) integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEV_APPS,
    DEV_CLOUDSYNC,
    DEV_DATASETS,
    DEV_DISKS,
    DEV_REPLICATION,
    DEV_SNAPSHOT_TASKS,
    DEV_SYSTEM,
    GIB,
)
from .coordinator import (
    TrueNASConfigEntry,
    TrueNASCoordinator,
    TrueNASData,
    alert_text,
    parse_date,
    parse_topology,
    prop,
)
from .entity import TrueNASEntity, add_dynamic_entities, clean_attrs

PARALLEL_UPDATES = 0

ValueFn = Callable[[TrueNASData], Any]
AttrsFn = Callable[[TrueNASData], dict[str, Any]]


class TrueNASSensor(TrueNASEntity, SensorEntity):
    """A sensor whose value is computed from the coordinator snapshot."""

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        category: str,
        item: str,
        unique_suffix: str,
        value_fn: ValueFn,
        *,
        exists_fn: Callable[[TrueNASData], bool] | None = None,
        attrs_fn: AttrsFn | None = None,
        **attrs: Any,
    ) -> None:
        """Create the sensor; extra keyword arguments become ``_attr_*``."""
        super().__init__(coordinator, "sensor", category, item, unique_suffix)
        self._value_fn = value_fn
        self._exists_fn = exists_fn
        self._attrs_fn = attrs_fn
        for key, value in attrs.items():
            setattr(self, f"_attr_{key}", value)

    @property
    def available(self) -> bool:
        """Unavailable when the underlying item is gone from the NAS."""
        if not super().available:
            return False
        return self._exists_fn is None or self._exists_fn(self.data)

    @property
    def native_value(self) -> Any:
        """Return the computed value."""
        try:
            return self._value_fn(self.data)
        except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the extra attributes."""
        if self._attrs_fn is None:
            return None
        try:
            return clean_attrs(self._attrs_fn(self.data))
        except (KeyError, TypeError, ValueError):
            return None


# ---------------------------------------------------------------------- #
# Helpers to read the snapshot
# ---------------------------------------------------------------------- #
def _gib(value: Any) -> float | None:
    value = prop(value)
    if isinstance(value, int | float):
        return round(value / GIB, 2)
    return None


def _cpu(data: TrueNASData) -> dict[str, Any]:
    return (data.realtime.get("cpu") or {}).get("cpu") or {}


def _cpu_temp(data: TrueNASData) -> float | None:
    cpus = data.realtime.get("cpu") or {}
    temp = (cpus.get("cpu") or {}).get("temp")
    if isinstance(temp, int | float):
        return round(temp, 1)
    temps = [
        c.get("temp")
        for name, c in cpus.items()
        if name != "cpu"
        and isinstance(c, dict)
        and isinstance(c.get("temp"), int | float)
    ]
    return round(max(temps), 1) if temps else None


def _memory(data: TrueNASData) -> dict[str, Any]:
    return data.realtime.get("memory") or {}


def _memory_pct(data: TrueNASData) -> float | None:
    mem = _memory(data)
    total = mem.get("physical_memory_total")
    avail = mem.get("physical_memory_available")
    if not total or avail is None:
        return None
    return round((total - avail) / total * 100)


def _load(index: int) -> ValueFn:
    def _fn(data: TrueNASData) -> float | None:
        load = data.system.get("loadavg")
        if isinstance(load, list) and len(load) > index:
            return round(float(load[index]), 2)
        return None

    return _fn


def _pool_value(pool: dict[str, Any], key: str) -> Any:
    if pool.get(key) is not None:
        return pool[key]
    return prop((pool.get("properties") or {}).get(key))


def _pool_attrs(pool: dict[str, Any]) -> dict[str, Any]:
    scan = pool.get("scan") or {}
    size = _pool_value(pool, "size")
    allocated = _pool_value(pool, "allocated")
    return {
        "Path": pool.get("path"),
        "Status": pool.get("status"),
        "Healthy": pool.get("healthy"),
        "Is decrypted": pool.get("is_decrypted"),
        "Autotrim": prop(pool.get("autotrim")),
        "Scrub state": scan.get("state"),
        "Scrub start": parse_date(scan.get("start_time")),
        "Scrub end": parse_date(scan.get("end_time")),
        "Scrub secs left": scan.get("total_secs_left"),
        "Usage": round(allocated / size * 100, 1)
        if isinstance(size, int | float) and size and isinstance(allocated, int | float)
        else None,
        "Topology": {
            role: ", ".join(vdevs) for role, vdevs in parse_topology(pool)[0].items()
        }
        or None,
    }


def _job_attrs(job: Any) -> dict[str, Any]:
    job = job or {}
    progress = job.get("progress") or {}
    return {
        "Time started": parse_date(job.get("time_started")),
        "Time finished": parse_date(job.get("time_finished")),
        "Job percent": progress.get("percent"),
        "Job description": progress.get("description"),
    }


# ---------------------------------------------------------------------- #
# Builders
# ---------------------------------------------------------------------- #
def _build(coordinator: TrueNASCoordinator) -> dict[str, Callable[[], Entity]]:
    data = coordinator.data
    out: dict[str, Callable[[], Entity]] = {}

    def add(key: str, *args: Any, **kwargs: Any) -> None:
        out[key] = lambda: TrueNASSensor(coordinator, *args, **kwargs)

    temp_kw = {
        "device_class": SensorDeviceClass.TEMPERATURE,
        "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
        "state_class": SensorStateClass.MEASUREMENT,
    }
    size_kw = {
        "device_class": SensorDeviceClass.DATA_SIZE,
        "native_unit_of_measurement": UnitOfInformation.GIBIBYTES,
        "state_class": SensorStateClass.MEASUREMENT,
        "suggested_display_precision": 2,
    }

    # System -------------------------------------------------------- #
    add(
        "sys_uptime",
        DEV_SYSTEM,
        "Uptime",
        "system_uptime",
        lambda d: d.system.get("boot_time"),
        device_class=SensorDeviceClass.TIMESTAMP,
    )
    add(
        "sys_temp",
        DEV_SYSTEM,
        "Temperature",
        "system_temperature",
        _cpu_temp,
        **temp_kw,
    )
    for idx, label in enumerate(("shortterm", "midterm", "longterm")):
        add(
            f"sys_load_{label}",
            DEV_SYSTEM,
            f"CPU load {label}",
            f"system_load_{label}",
            _load(idx),
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
        )
    add(
        "sys_cpu",
        DEV_SYSTEM,
        "CPU usage",
        "system_cpu_usage",
        lambda d: round(_cpu(d)["usage"], 1),
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    )
    add(
        "sys_mem",
        DEV_SYSTEM,
        "Memory usage",
        "system_memory_usage",
        _memory_pct,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        attrs_fn=lambda d: {
            "Memory total value": _gib(_memory(d).get("physical_memory_total")),
            "Memory available value": _gib(_memory(d).get("physical_memory_available")),
        },
    )
    add(
        "sys_arc",
        DEV_SYSTEM,
        "ARC size",
        "system_arc_size",
        lambda d: _gib(_memory(d).get("arc_size")),
        **size_kw,
    )

    # Pools (free space) -------------------------------------------- #
    for pool_name in data.pools:
        add(
            f"pool_free_{pool_name}",
            DEV_SYSTEM,
            f"{pool_name} free",
            f"pool_{pool_name}_free",
            lambda d, n=pool_name: _gib(_pool_value(d.pools[n], "free")),
            exists_fn=lambda d, n=pool_name: n in d.pools,
            attrs_fn=lambda d, n=pool_name: _pool_attrs(d.pools[n]),
            **size_kw,
        )

    # Network interfaces -------------------------------------------- #
    rate_kw = {
        "device_class": SensorDeviceClass.DATA_RATE,
        "native_unit_of_measurement": UnitOfDataRate.BYTES_PER_SECOND,
        "suggested_unit_of_measurement": UnitOfDataRate.MEGABYTES_PER_SECOND,
        "state_class": SensorStateClass.MEASUREMENT,
        "suggested_display_precision": 3,
    }

    def _iface_attrs(d: TrueNASData, n: str) -> dict[str, Any]:
        iface = d.interfaces.get(n) or {}
        state = iface.get("state") or {}
        live = (d.realtime.get("interfaces") or {}).get(n) or {}
        return {
            "Description": iface.get("description"),
            "Mtu": iface.get("mtu"),
            "Link state": live.get("link_state") or state.get("link_state"),
            "Speed": live.get("speed"),
            "Active media type": state.get("active_media_type"),
            "Active media subtype": state.get("active_media_subtype"),
            "Link address": state.get("link_address"),
        }

    for iface_name in data.interfaces:
        for label, field in (("RX", "received_bytes_rate"), ("TX", "sent_bytes_rate")):
            add(
                f"iface_{iface_name}_{label}",
                DEV_SYSTEM,
                f"{iface_name} {label}",
                f"iface_{iface_name}_{label.lower()}",
                lambda d, n=iface_name, f=field: (
                    (d.realtime.get("interfaces") or {}).get(n) or {}
                )[f],
                exists_fn=lambda d, n=iface_name: n in d.interfaces,
                attrs_fn=lambda d, n=iface_name: _iface_attrs(d, n),
                **rate_kw,
            )

    # Datasets ------------------------------------------------------- #
    def _ds_attrs(ds: dict[str, Any]) -> dict[str, Any]:
        return {
            "Type": ds.get("type"),
            "Pool": ds.get("pool"),
            "Mountpoint": ds.get("mountpoint"),
            "Deduplication": prop(ds.get("deduplication")),
            "Atime": prop(ds.get("atime")),
            "Casesensitivity": prop(ds.get("casesensitivity")),
            "Checksum": prop(ds.get("checksum")),
            "Exec": prop(ds.get("exec")),
            "Sync": prop(ds.get("sync")),
            "Compression": prop(ds.get("compression")),
            "Compressratio": prop(ds.get("compressratio")),
            "Quota": prop(ds.get("quota")),
            "Copies": prop(ds.get("copies")),
            "Readonly": prop(ds.get("readonly")),
            "Recordsize": prop(ds.get("recordsize")),
            "Encryption algorithm": prop(ds.get("encryption_algorithm")),
            "Encrypted": ds.get("encrypted"),
            "Locked": ds.get("locked"),
            "Used": _gib(ds.get("used")),
            "Available": _gib(ds.get("available")),
        }

    for ds_name in data.datasets:
        add(
            f"ds_{ds_name}",
            DEV_DATASETS,
            ds_name,
            f"dataset_{ds_name}",
            lambda d, n=ds_name: _gib(d.datasets[n].get("used")),
            exists_fn=lambda d, n=ds_name: n in d.datasets,
            attrs_fn=lambda d, n=ds_name: _ds_attrs(d.datasets[n]),
            **size_kw,
        )

    # Disks ---------------------------------------------------------- #
    def _disk_attrs(d: TrueNASData, key: str) -> dict[str, Any]:
        disk = d.disks[key]
        health = d.disk_health.get(disk["name"]) or {}
        filt = d.temp_filter.get(disk["name"]) or {}
        return {
            "Serial": disk.get("serial"),
            "Size": _gib(disk.get("size")),
            "Hddstandby": disk.get("hddstandby"),
            "Advpowermgmt": disk.get("advpowermgmt"),
            "Togglesmart": disk.get("togglesmart"),
            "Model": disk.get("model"),
            "Rotationrate": disk.get("rotationrate"),
            "Type": disk.get("type"),
            "Name": disk.get("name"),
            "Devname": disk.get("devname"),
            "Zfs guid": disk.get("zfs_guid"),
            "Identifier": disk.get("identifier"),
            "Pool": disk.get("pool") or health.get("pool"),
            "Role": health.get("role"),
            "Vdev": health.get("vdev"),
            "ZFS status": health.get("status"),
            "Read errors": health.get("read_errors"),
            "Write errors": health.get("write_errors"),
            "Checksum errors": health.get("checksum_errors"),
            "Raw temperature": filt.get("raw"),
            "Spurious readings ignored": filt.get("ignored") or None,
        }

    for disk_key, disk in data.disks.items():
        add(
            f"disk_{disk_key}",
            DEV_DISKS,
            disk["name"],
            f"disk_{disk_key}",
            lambda d, k=disk_key: d.disk_temps.get(d.disks[k]["name"]),
            exists_fn=lambda d, k=disk_key: k in d.disks,
            attrs_fn=lambda d, k=disk_key: _disk_attrs(d, k),
            **temp_kw,
        )

    # Cloud sync tasks ------------------------------------------------ #
    for cs_id, cs in data.cloudsyncs.items():
        add(
            f"cs_{cs_id}",
            DEV_CLOUDSYNC,
            cs.get("description") or f"task {cs_id}",
            f"cloudsync_{cs_id}",
            lambda d, i=cs_id: (d.cloudsyncs[i].get("job") or {}).get("state"),
            exists_fn=lambda d, i=cs_id: i in d.cloudsyncs,
            attrs_fn=lambda d, i=cs_id: {
                "Direction": d.cloudsyncs[i].get("direction"),
                "Path": d.cloudsyncs[i].get("path"),
                "Enabled": d.cloudsyncs[i].get("enabled"),
                "Transfer mode": d.cloudsyncs[i].get("transfer_mode"),
                "Snapshot": d.cloudsyncs[i].get("snapshot"),
                **_job_attrs(d.cloudsyncs[i].get("job")),
            },
        )

    # Replication tasks ----------------------------------------------- #
    def _rep_state(rep: dict[str, Any]) -> str | None:
        state = rep.get("state")
        if isinstance(state, dict):
            return state.get("state")
        return state or (rep.get("job") or {}).get("state")

    for rep_id, rep in data.replications.items():
        add(
            f"rep_{rep_id}",
            DEV_REPLICATION,
            rep.get("name") or f"task {rep_id}",
            f"replication_{rep_id}",
            lambda d, i=rep_id: _rep_state(d.replications[i]),
            exists_fn=lambda d, i=rep_id: i in d.replications,
            attrs_fn=lambda d, i=rep_id: {
                "Source datasets": d.replications[i].get("source_datasets"),
                "Target dataset": d.replications[i].get("target_dataset"),
                "Recursive": d.replications[i].get("recursive"),
                "Enabled": d.replications[i].get("enabled"),
                "Direction": d.replications[i].get("direction"),
                "Transport": d.replications[i].get("transport"),
                "Auto": d.replications[i].get("auto"),
                "Retention policy": d.replications[i].get("retention_policy"),
                "State": _rep_state(d.replications[i]),
                **_job_attrs(d.replications[i].get("job")),
            },
        )

    # Periodic snapshot tasks ----------------------------------------- #
    def _snap_state(task: dict[str, Any]) -> str | None:
        state = task.get("state")
        return state.get("state") if isinstance(state, dict) else state

    for task_id, task in data.snapshot_tasks.items():
        add(
            f"snap_{task_id}",
            DEV_SNAPSHOT_TASKS,
            task.get("dataset") or f"task {task_id}",
            f"snapshot_task_{task_id}",
            lambda d, i=task_id: _snap_state(d.snapshot_tasks[i]),
            exists_fn=lambda d, i=task_id: i in d.snapshot_tasks,
            attrs_fn=lambda d, i=task_id: {
                "Recursive": d.snapshot_tasks[i].get("recursive"),
                "Lifetime value": d.snapshot_tasks[i].get("lifetime_value"),
                "Lifetime unit": d.snapshot_tasks[i].get("lifetime_unit"),
                "Enabled": d.snapshot_tasks[i].get("enabled"),
                "Naming schema": d.snapshot_tasks[i].get("naming_schema"),
                "Allow empty": d.snapshot_tasks[i].get("allow_empty"),
                "VMware sync": d.snapshot_tasks[i].get("vmware_sync"),
                "State": _snap_state(d.snapshot_tasks[i]),
                "Datetime": parse_date(
                    (d.snapshot_tasks[i].get("state") or {}).get("datetime")
                    if isinstance(d.snapshot_tasks[i].get("state"), dict)
                    else None
                ),
            },
        )

    # Alerts --------------------------------------------------------- #
    def _active_alerts(d: TrueNASData) -> list[dict[str, Any]]:
        return [a for a in d.alerts if not a.get("dismissed")]

    add(
        "sys_alerts",
        DEV_SYSTEM,
        "Alerts",
        "system_alerts",
        lambda d: len(_active_alerts(d)),
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:bell-alert-outline",
        attrs_fn=lambda d: {
            "Alerts": [
                {
                    "uuid": a.get("uuid") or a.get("id"),
                    "level": a.get("level"),
                    "text": alert_text(a),
                    "datetime": str(parse_date(a.get("datetime")) or ""),
                }
                for a in _active_alerts(d)
            ],
            "Dismissed": sum(1 for a in d.alerts if a.get("dismissed")),
        },
    )

    # Scrub per pool ------------------------------------------------- #
    for pool_name in data.pools:
        add(
            f"pool_scrub_{pool_name}",
            DEV_SYSTEM,
            f"{pool_name} scrub",
            f"pool_{pool_name}_scrub",
            lambda d, n=pool_name: (d.pools[n].get("scan") or {}).get("state"),
            exists_fn=lambda d, n=pool_name: n in d.pools,
            icon="mdi:magnify-scan",
            attrs_fn=lambda d, n=pool_name: {
                "Function": (d.pools[n].get("scan") or {}).get("function"),
                "Percentage": (d.pools[n].get("scan") or {}).get("percentage"),
                "Errors": (d.pools[n].get("scan") or {}).get("errors"),
                "Start": parse_date((d.pools[n].get("scan") or {}).get("start_time")),
                "End": parse_date((d.pools[n].get("scan") or {}).get("end_time")),
                "Secs left": (d.pools[n].get("scan") or {}).get("total_secs_left"),
            },
        )

    # Task progress -------------------------------------------------- #
    def _progress(job: Any) -> float:
        job = job or {}
        if job.get("state") not in ("RUNNING", "WAITING"):
            return 0
        return round(float((job.get("progress") or {}).get("percent") or 0), 1)

    pct_kw = {
        "native_unit_of_measurement": PERCENTAGE,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:progress-upload",
    }
    for rep_id, rep in data.replications.items():
        add(
            f"rep_progress_{rep_id}",
            DEV_REPLICATION,
            f"{rep.get('name') or f'task {rep_id}'} progress",
            f"replication_{rep_id}_progress",
            lambda d, i=rep_id: _progress(d.replications[i].get("job")),
            exists_fn=lambda d, i=rep_id: i in d.replications,
            attrs_fn=lambda d, i=rep_id: {
                "Description": (
                    (d.replications[i].get("job") or {}).get("progress") or {}
                ).get("description")
            },
            **pct_kw,
        )
    for cs_id, cs in data.cloudsyncs.items():
        add(
            f"cs_progress_{cs_id}",
            DEV_CLOUDSYNC,
            f"{cs.get('description') or f'task {cs_id}'} progress",
            f"cloudsync_{cs_id}_progress",
            lambda d, i=cs_id: _progress(d.cloudsyncs[i].get("job")),
            exists_fn=lambda d, i=cs_id: i in d.cloudsyncs,
            attrs_fn=lambda d, i=cs_id: {
                "Description": (
                    (d.cloudsyncs[i].get("job") or {}).get("progress") or {}
                ).get("description")
            },
            **pct_kw,
        )

    # Per-app resource usage ----------------------------------------- #
    for app_name in data.apps:
        add(
            f"app_cpu_{app_name}",
            DEV_APPS,
            f"{app_name} CPU",
            f"app_{app_name}_cpu",
            lambda d, n=app_name: (d.app_stats.get(n) or {}).get("cpu_usage"),
            exists_fn=lambda d, n=app_name: n in d.apps,
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:cpu-64-bit",
        )
        add(
            f"app_mem_{app_name}",
            DEV_APPS,
            f"{app_name} memory",
            f"app_{app_name}_memory",
            lambda d, n=app_name: (d.app_stats.get(n) or {}).get("memory"),
            exists_fn=lambda d, n=app_name: n in d.apps,
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            suggested_unit_of_measurement=UnitOfInformation.MEBIBYTES,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
        )

    return out


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrueNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensors."""
    add_dynamic_entities(entry, async_add_entities, _build)
