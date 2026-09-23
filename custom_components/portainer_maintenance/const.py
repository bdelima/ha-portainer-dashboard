"""Constants for the Portainer Maintenance integration."""

DOMAIN = "portainer_maintenance"
CONF_WEBAPP_URL = "webapp_url"
CONF_NOTIFY_DEVICES = "notify_devices"

PANEL_PATH = "portainer-actions"
PANEL_TITLE = "Portainer Maintenance"
PANEL_ICON = "mdi:docker"

SENSOR_UPDATES_PENDING = "portainer_updates_pending"
# (1.3.0, breaking rename) was SENSOR_CONTAINER_TROUBLE = "portainer_container_trouble".
# Broadened past individual containers to also cover an endpoint that's
# dropped out of core's own portainer coordinator entirely (see
# PortainerTroubleCoordinator in sensor.py), so the old name no longer fit.
SENSOR_TROUBLE = "portainer_trouble"
SENSOR_STALE_DEVICES = "portainer_stale_devices"
SENSOR_CLEANUP = "portainer_cleanup"
SENSOR_ACTIONS_URL = "portainer_actions_url"
