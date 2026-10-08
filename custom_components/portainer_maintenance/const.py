"""Constants for the Portainer Maintenance integration."""

DOMAIN = "portainer_maintenance"
CONF_WEBAPP_URL = "webapp_url"
CONF_NOTIFY_DEVICES = "notify_devices"
CONF_ADMIN_ONLY = "admin_only"
# On by default: the panel is hidden from non-administrator HA users unless
# someone turns this off. Also what an entry created before the option
# existed gets.
DEFAULT_ADMIN_ONLY = True

# Sign-in for the sidecar webapp (its AUTH_USERNAME / AUTH_PASSWORD /
# AUTH_ALLOW_ANONYMOUS). On by default, to match the sidecar, which is open until
# it is given credentials.
CONF_SIDECAR_ANONYMOUS = "sidecar_anonymous"
CONF_SIDECAR_USERNAME = "sidecar_username"
CONF_SIDECAR_PASSWORD = "sidecar_password"
DEFAULT_SIDECAR_ANONYMOUS = True

PANEL_PATH = "portainer-actions"
# What the sidebar entry is called (the integration itself is still named
# "Portainer Maintenance").
PANEL_TITLE = "Portainer"
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
