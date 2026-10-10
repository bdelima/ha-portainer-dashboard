"""Constants for the Portainer Maintenance integration."""

import json
from pathlib import Path

DOMAIN = "portainer_maintenance"

# What the sidecar webapp may rely on from this integration: its services, the
# fields on its sensors, and the sidecar-facing behavior built on them. NOT the
# release number -- a release that only touches the bundled blueprint, say,
# leaves this alone, so the sidecar is never told to update the integration for
# something it does not use. Bump the MAJOR part when a change breaks what an
# existing sidecar relies on, the MINOR part when something new is added that a
# newer sidecar may start to rely on, the PATCH part for fixes behind the same
# contract. The sidecar holds the lowest level it needs
# (REQUIRED_DASHBOARD_API_LEVEL in its main.py) and compares it with this.
API_LEVEL = "1.0.0"


def _running_version() -> str:
    """The release this code belongs to, read once when this module is first
    imported -- i.e. what Home Assistant is actually running, which stays the
    old number after HACS has downloaded a newer one until HA restarts."""
    try:
        manifest = Path(__file__).with_name("manifest.json")
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "unknown")
    except (OSError, ValueError):
        return "unknown"


RUNNING_VERSION = _running_version()

# Fired by perform_update / update_done when an update has finished. The bundled
# blueprint listens for them and builds the phone push and the notification-
# panel entry, so both follow their category's channel and toggles.
EVENT_UPDATE_DONE = f"{DOMAIN}_update_done"
EVENT_STACK_RESTART_NEEDED = f"{DOMAIN}_stack_restart_needed"
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
