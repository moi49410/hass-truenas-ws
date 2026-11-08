"""Constants for the TrueNAS SCALE (WebSocket) integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "truenas_ws"
MANUFACTURER: Final = "iXsystems"

CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_FILTER_TEMPS: Final = "filter_spurious_temps"
CONF_MAC: Final = "mac"

DEFAULT_VERIFY_SSL: Final = False
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds
MIN_SCAN_INTERVAL: Final = 10
DEFAULT_FILTER_TEMPS: Final = True
APP_STATS_INTERVAL: Final = 10  # seconds between app.stats events

# Some NVMe SSDs (Samsung 980 family) intermittently report exactly 84 °C
# (357 K) because of a firmware issue; such readings can be ignored.
SPURIOUS_TEMP_MODELS: Final = ("SSD 980",)
SPURIOUS_TEMP_RANGE: Final = (83.5, 84.5)

# Pool topology roles, in display order.
TOPOLOGY_ROLES: Final = ("data", "special", "dedup", "log", "cache", "spare")
ALERT_PROBLEM_LEVELS: Final = {"WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY"}
EVENT_ALERT: Final = "truenas_ws_alert"
CONFIG_BACKUP_DIR: Final = "truenas_backups"
CONFIG_BACKUP_KEEP: Final = 10

# Slow data, refreshed less often than the main poll.
DISK_TEMP_INTERVAL: Final = timedelta(minutes=5)  # TrueNAS caches temps 5 min
UPDATE_CHECK_INTERVAL: Final = timedelta(minutes=30)
APP_UPGRADE_SUMMARY_INTERVAL: Final = timedelta(hours=1)

# Entity-id prefix kept identical to the former "truenas" custom integration
# so that existing automations, dashboards and history keep working.
ENTITY_PREFIX: Final = "truenas"

# Device (category) names, identical to the former integration.
DEV_SYSTEM: Final = "System"
DEV_DATASETS: Final = "Datasets"
DEV_DISKS: Final = "Disks"
DEV_SERVICES: Final = "Services"
DEV_APPS: Final = "Apps"
DEV_CLOUDSYNC: Final = "Cloudsync"
DEV_REPLICATION: Final = "Replication"
DEV_SNAPSHOT_TASKS: Final = "Snapshot tasks"
DEV_SHARES: Final = "Shares"

GIB: Final = 1024**3

# Interfaces types worth exposing (interface.query "type").
PHYSICAL_IFACE_TYPES: Final = {"PHYSICAL", "BRIDGE", "LINK_AGGREGATION", "VLAN"}

# Datasets that are internal to TrueNAS and are not exposed.
HIDDEN_DATASET_PARTS: Final = (".", "ix-applications", "ix-apps", ".system")

# Job states reported by TrueNAS.
JOB_FINAL_STATES: Final = {"SUCCESS", "FAILED", "ABORTED"}

# Service names
SERVICE_SERVICE_START: Final = "service_start"
SERVICE_SERVICE_STOP: Final = "service_stop"
SERVICE_SERVICE_RESTART: Final = "service_restart"
SERVICE_APP_START: Final = "app_start"
SERVICE_APP_STOP: Final = "app_stop"
SERVICE_APP_RESTART: Final = "app_restart"
SERVICE_CLOUDSYNC_RUN: Final = "cloudsync_run"
SERVICE_REPLICATION_RUN: Final = "replication_run"
SERVICE_SNAPSHOT_CREATE: Final = "dataset_snapshot_create"
SERVICE_SYSTEM_REBOOT: Final = "system_reboot"
SERVICE_SYSTEM_SHUTDOWN: Final = "system_shutdown"
SERVICE_POOL_IMPORT: Final = "pool_import"
SERVICE_POOL_EXPORT: Final = "pool_export"
SERVICE_POOL_SCRUB: Final = "pool_scrub"
SERVICE_SMART_TEST: Final = "smart_test"
SERVICE_CONFIG_BACKUP: Final = "config_backup"
SERVICE_ALERT_DISMISS: Final = "alert_dismiss"
SERVICE_WAKE: Final = "wake"

# Pools that must never be exported from Home Assistant.
PROTECTED_POOLS: Final = {"boot-pool", "freenas-boot"}

# Upper bounds for jobs we wait on.
REPLICATION_TIMEOUT: Final = 6 * 3600  # seconds
POOL_JOB_TIMEOUT: Final = 15 * 60  # seconds

ATTR_NAME: Final = "name"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_SNAPSHOT_NAME: Final = "snapshot_name"
ATTR_RECURSIVE: Final = "recursive"
ATTR_WAIT: Final = "wait"
ATTR_TYPE: Final = "type"
ATTR_DISKS: Final = "disks"
ATTR_INCLUDE_SECRETS: Final = "include_secrets"
ATTR_ID: Final = "id"
