"""Portainer Maintenance.

Formerly "Portainer Cleanup" -- renamed and expanded once the design grew
past "expose one service" into a real maintenance layer on top of the core
`portainer` integration. On setup this integration:

1. Registers `portainer_maintenance.remove_device`, built on the stable
   `device_registry.async_remove_device()` API -- the webapp's stale-device
   delete calls this, because the native Settings -> Devices page's own
   Delete button calls an internal frontend WebSocket command, not a
   documented service.

2. Installs its bundled automation/script blueprints into HA's config dir
   automatically (see bundled_blueprints/) -- no more separate SSH deploy
   step for those. Re-copied on every load, so treat the deployed copies
   as generated, not hand-editable.

3. Registers an iframe sidebar panel pointing at the Portainer actions
   webapp, at a fixed, known path (PANEL_PATH) -- via the same
   `frontend.async_register_built_in_panel` primitive the legacy
   `panel_iframe` YAML integration used, just invoked from a config-flow
   integration instead of static YAML. This removes the old manual
   "Add Dashboard -> Webpage -> read the random URL from the address bar"
   step entirely.

4. Computes the notification click-through URL automatically from this HA
   instance's own configured external/internal URL plus the fixed panel
   path, and exposes it as a read-only sensor (see sensor.py) -- no more
   typing a URL into a text helper or a config field.

5. Forwards to the sensor platform, which defines the three tracking
   sensors (updates pending / container trouble / stale devices) as native
   entities on coordinators, instead of YAML template sensors in
   templates.yaml. Their entity_ids are pinned explicitly to match what
   templates.yaml used to produce (sensor.portainer_updates_pending, etc.)
   so the merged automation blueprint and the webapp don't need to change.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr

from .const import CONF_WEBAPP_URL, DOMAIN, PANEL_ICON, PANEL_PATH, PANEL_TITLE

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

SERVICE_REMOVE_DEVICE = "remove_device"
SERVICE_REMOVE_DEVICE_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})

BUNDLED_BLUEPRINTS_DIR = Path(__file__).parent / "bundled_blueprints"

# (bundled source, relative to BUNDLED_BLUEPRINTS_DIR) -> (dest, relative to config dir)
BLUEPRINT_FILES = [
    (
        "automation/portainer_automations.yaml",
        f"blueprints/automation/{DOMAIN}/portainer_automations.yaml",
    ),
    (
        "script/perform_update.yaml",
        f"blueprints/script/{DOMAIN}/perform_update.yaml",
    ),
    (
        "script/update_done.yaml",
        f"blueprints/script/{DOMAIN}/update_done.yaml",
    ),
]


def _install_blueprints(hass: HomeAssistant) -> None:
    """Copy the bundled blueprint files into HA's config dir. Blocking I/O -- run in the executor."""
    for src_rel, dest_rel in BLUEPRINT_FILES:
        src = BUNDLED_BLUEPRINTS_DIR / src_rel
        dest = Path(hass.config.path(dest_rel))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
        _LOGGER.debug("Installed blueprint %s -> %s", src, dest)


def _register_panel(hass: HomeAssistant, webapp_url: str) -> None:
    try:
        frontend.async_register_built_in_panel(
            hass,
            component_name="iframe",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            frontend_url_path=PANEL_PATH,
            config={"url": webapp_url},
            require_admin=False,
        )
    except ValueError:
        # Already registered (e.g. a config entry reload) -- replace it so
        # a changed webapp_url actually takes effect.
        frontend.async_remove_panel(hass, PANEL_PATH)
        frontend.async_register_built_in_panel(
            hass,
            component_name="iframe",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            frontend_url_path=PANEL_PATH,
            config={"url": webapp_url},
            require_admin=False,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Portainer Maintenance from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    async def handle_remove_device(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        registry = dr.async_get(hass)
        device = registry.async_get(device_id)

        if device is None:
            raise ValueError(f"No device found with id '{device_id}'")

        _LOGGER.info(
            "Removing device '%s' (%s) via %s.remove_device",
            device.name_by_user or device.name,
            device_id,
            DOMAIN,
        )
        registry.async_remove_device(device_id)

    if not hass.services.has_service(DOMAIN, SERVICE_REMOVE_DEVICE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REMOVE_DEVICE,
            handle_remove_device,
            schema=SERVICE_REMOVE_DEVICE_SCHEMA,
        )

    await hass.async_add_executor_job(_install_blueprints, hass)

    webapp_url = entry.data[CONF_WEBAPP_URL]

    base_url = hass.config.external_url or hass.config.internal_url or ""
    if base_url:
        actions_url = f"{base_url.rstrip('/')}/{PANEL_PATH}"
    else:
        actions_url = ""
        _LOGGER.warning(
            "No external_url or internal_url configured in Home Assistant "
            "(Settings -> System -> Network) -- the Portainer actions URL "
            "sensor will be empty until one is set."
        )
    hass.data[DOMAIN][entry.entry_id]["actions_url"] = actions_url

    _register_panel(hass, webapp_url)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Portainer Maintenance config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        frontend.async_remove_panel(hass, PANEL_PATH)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_REMOVE_DEVICE)
    return unload_ok
