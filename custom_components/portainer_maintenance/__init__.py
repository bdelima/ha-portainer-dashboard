"""Portainer Maintenance.

Formerly "Portainer Cleanup" -- renamed and expanded once the design grew
past "expose one service" into a real maintenance layer on top of the core
`portainer` integration. On setup this integration:

1. Registers `portainer_maintenance.remove_device`, built on the stable
   `device_registry.async_remove_device()` API -- the webapp's stale-device
   delete calls this, because the native Settings -> Devices page's own
   Delete button calls an internal frontend WebSocket command, not a
   documented service.

1b. Registers `portainer_maintenance.perform_update` and
   `portainer_maintenance.update_done` -- native services, not blueprint
   scripts. These used to be `bundled_blueprints/script/*.yaml`, each
   requiring the user to create a script instance from the blueprint and
   then manually override its auto-generated Entity ID to match the exact
   literal string (`portainer_perform_update` / `portainer_update_done`)
   the merged automation blueprint calls by name -- an easy step to miss
   or get wrong, and when missed, HA reports it as an opaque "automation
   uses an unknown action" repair with no obvious link back to that
   missed step. A native service has no user-assigned entity_id to get
   wrong in the first place: it's registered under this fixed domain the
   moment the integration loads, same as remove_device/prune_images
   above. The two scripts read who to notify from this config entry's
   notify_devices (see config_flow.py) instead of a blueprint input,
   since a plain service call has no blueprint inputs to read from.

1c. Registers `portainer_maintenance.restart_stack` -- a stop/start of a
   whole Portainer stack's switch.* entity (HA core's portainer
   integration already provides one per stack). This exists for a
   confirmed, unfixed Portainer bug: recreating a single container whose
   network_mode is `service:<other>` / `container:<other>` (a VPN sidecar
   pattern like gluetun) makes Docker's daemon reject the create call
   over a hostname/network_mode conflict -- reproduced identically via
   Portainer's own UI, nothing to do with HA or this integration. The
   pull+recreate still actually completes despite the error, but the
   image tag only reconciles cleanly once the owning stack is restarted.
   `perform_update` below swallows that specific error (instead of
   aborting) and offers the phone notification's "Restart Stack Now"
   action as a follow-up, rather than restarting automatically -- a full
   stack restart bounces every other container in it too, which
   shouldn't happen silently.

2. Installs its bundled automation blueprint into HA's config dir
   automatically (see bundled_blueprints/) -- no more separate SSH deploy
   step for that. Re-copied on every load, so treat the deployed copy as
   generated, not hand-editable.

3. Registers an iframe sidebar panel pointing at the Portainer actions
   webapp, at a fixed, known path (PANEL_PATH) -- via the same
   `frontend.async_register_built_in_panel` primitive the legacy
   `panel_iframe` YAML integration used, just invoked from a config-flow
   integration instead of static YAML. This removes the old manual
   "Add Dashboard -> Webpage -> read the random URL from the address bar"
   step entirely.

4. Computes the notification click-through URL automatically and exposes
   it as a read-only sensor (see sensor.py) -- no typing a URL into a
   text helper or config field. It's a bare relative path (/PANEL_PATH),
   which the HA companion app treats as "navigate within the server I'm
   already connected to" -- so tapping a notification always opens the
   sidebar panel in-app, with no dependency on HA's own external/internal
   URL (Settings -> System -> Network) being configured at all.

5. Forwards to the sensor platform, which defines the three tracking
   sensors (updates pending / container trouble / stale devices) as native
   entities on coordinators, instead of YAML template sensors in
   templates.yaml. Their entity_ids are pinned explicitly to match what
   templates.yaml used to produce (sensor.portainer_updates_pending, etc.)
   so the merged automation blueprint and the webapp don't need to change.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import slugify

from .const import CONF_NOTIFY_DEVICES, CONF_WEBAPP_URL, DOMAIN, PANEL_ICON, PANEL_PATH, PANEL_TITLE
from .sensor import _device_name, _portainer_entity_ids, _stack_info, _walk_to_root

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

# How long to let a stack settle between the stop and start halves of
# restart_stack -- long enough for Docker to actually tear down the
# network-owning container's namespace before anything tries to rejoin it.
STACK_RESTART_SETTLE_SECONDS = 5

SERVICE_REMOVE_DEVICE = "remove_device"
SERVICE_REMOVE_DEVICE_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})

SERVICE_PRUNE_IMAGES = "prune_images"
SERVICE_PRUNE_IMAGES_SCHEMA = vol.Schema(
    {
        vol.Optional("dangling", default=False): cv.boolean,
        vol.Optional("until_hours"): vol.Coerce(int),
    }
)

SERVICE_PERFORM_UPDATE = "perform_update"
SERVICE_PERFORM_UPDATE_SCHEMA = vol.Schema({vol.Required("update_entity"): cv.entity_id})

SERVICE_UPDATE_DONE = "update_done"
SERVICE_UPDATE_DONE_SCHEMA = vol.Schema(
    {
        vol.Required("device_name"): cv.string,
        vol.Required("update_entity"): cv.entity_id,
    }
)

SERVICE_RESTART_STACK = "restart_stack"
SERVICE_RESTART_STACK_SCHEMA = vol.Schema({vol.Required("switch_entity_id"): cv.entity_id})

BUNDLED_BLUEPRINTS_DIR = Path(__file__).parent / "bundled_blueprints"

# (bundled source, relative to BUNDLED_BLUEPRINTS_DIR) -> (dest, relative to config dir)
# perform_update/update_done used to be here as script blueprints -- see
# the module docstring (1b.) for why they're native services now instead.
BLUEPRINT_FILES = [
    (
        "automation/portainer_automations.yaml",
        f"blueprints/automation/{DOMAIN}/portainer_automations.yaml",
    ),
]


def _notify_services_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """notify.mobile_app_<slug> for each device_id in this entry's
    notify_devices -- the Python equivalent of the Jinja
    `map('device_attr', 'name') | map('slugify') | ...` chain the
    automation blueprint uses for its own notify_device input."""
    device_reg = dr.async_get(hass)
    services = []
    for device_id in entry.data.get(CONF_NOTIFY_DEVICES, []):
        device = device_reg.async_get(device_id)
        if device is None:
            continue
        name = device.name_by_user or device.name
        if name:
            services.append(f"notify.mobile_app_{slugify(name)}")
    return services


def _stack_switch_entity_id(hass: HomeAssistant, container_device_id: str) -> str | None:
    """Thin wrapper around sensor.py's _stack_info -- same helper the
    updates-pending sensor uses to group its items by stack, reused here
    so the two never drift on what counts as "this container's stack"."""
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    _stack_name, switch_entity_id = _stack_info(device_reg, entity_reg, container_device_id)
    return switch_entity_id


async def _async_update_done(
    hass: HomeAssistant, entry: ConfigEntry, device_name: str, update_entity: str
) -> None:
    """Shared finishing logic for a completed update: a persistent_notification
    plus a real phone push to every configured notify device. Used both by
    the perform_update service and directly as its own service (for parity
    with the old update_done.yaml script, in case anything else calls it)."""
    notif_id = f"portainer_update_{update_entity.replace('.', '_')}"
    now_str = dt_util.now().strftime("%Y-%m-%d %H:%M")

    await hass.services.async_call(
        "persistent_notification", "dismiss", {"notification_id": notif_id}
    )
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "notification_id": notif_id,
            "title": f"Update performed: {device_name}",
            "message": f"Updated on {now_str}",
        },
    )

    for service in _notify_services_for_entry(hass, entry):
        domain, service_name = service.split(".", 1)
        await hass.services.async_call(
            domain,
            service_name,
            {
                "title": "Update performed",
                "message": f"{device_name} updated on {now_str}.",
                "data": {"tag": f"portainer_update_done_{update_entity.replace('.', '_')}"},
            },
        )


async def _async_notify_stack_restart_needed(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_name: str,
    update_entity: str,
    switch_entity_id: str | None,
    actions_url: str,
) -> None:
    """The update itself went through, but recreate_container hit the
    known network_mode:service:X daemon-conflict error (see the module
    docstring, 1c.) -- so the container's image tag won't fully reconcile
    until its stack is restarted. Notify instead of silently restarting:
    a full stack restart bounces every other container in it too, and
    that shouldn't happen without a tap.

    handle_perform_update only calls this once it has already confirmed
    switch_entity_id is not None (a standalone-container failure is
    re-raised there instead, so it can't reach here reporting a false
    success) -- the None-check below is just a defensive fallback for any
    future/direct caller of this helper, not an expected path today."""
    if switch_entity_id is None:
        _LOGGER.warning(
            "%s.perform_update: asked to notify a stack-restart-needed case for '%s' "
            "with no owning stack switch entity -- falling back to the normal "
            "update-performed notification",
            DOMAIN,
            update_entity,
        )
        await _async_update_done(hass, entry, device_name, update_entity)
        return

    notif_id = f"portainer_update_{update_entity.replace('.', '_')}"
    now_str = dt_util.now().strftime("%Y-%m-%d %H:%M")
    message = (
        f"{device_name} was updated on {now_str}, but its stack needs a restart "
        f"to finish cleanly (known Portainer limitation for containers sharing "
        f"another container's network)."
    )

    await hass.services.async_call(
        "persistent_notification", "dismiss", {"notification_id": notif_id}
    )
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "notification_id": notif_id,
            "title": f"Stack restart needed: {device_name}",
            "message": message,
        },
    )

    for service in _notify_services_for_entry(hass, entry):
        domain, service_name = service.split(".", 1)
        await hass.services.async_call(
            domain,
            service_name,
            {
                "title": "Stack restart needed",
                "message": message,
                "data": {
                    "tag": f"portainer_update_done_{update_entity.replace('.', '_')}",
                    "actions": [
                        {"action": "URI", "title": "Open Dashboard", "uri": actions_url},
                        {
                            "action": f"RESTART_STACK_{switch_entity_id}",
                            "title": "Restart Stack Now",
                        },
                    ],
                },
            },
        )


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

    async def handle_prune_images(call: ServiceCall) -> None:
        dangling = call.data.get("dangling", False)
        until_hours = call.data.get("until_hours")

        # Discover every Portainer endpoint (host) device the same way the
        # tracking sensors do -- walk every portainer-platform entity up to
        # its root device -- so a newly-added host is picked up automatically
        # and this never needs a static list of device_ids configured
        # anywhere. See sensor.py's module docstring for why this reuses
        # that logic instead of duplicating it.
        entity_reg = er.async_get(hass)
        device_reg = dr.async_get(hass)

        root_ids: set[str] = set()
        for entity_id in _portainer_entity_ids(entity_reg):
            reg_entry = entity_reg.async_get(entity_id)
            device_id = reg_entry.device_id if reg_entry else None
            root_id = _walk_to_root(device_reg, device_id)
            if root_id:
                root_ids.add(root_id)

        if not root_ids:
            _LOGGER.warning(
                "%s.prune_images: no Portainer endpoint devices found -- nothing to prune",
                DOMAIN,
            )
            return

        service_data: dict = {"dangling": dangling}
        if until_hours is not None:
            service_data["until"] = {"hours": until_hours}

        for root_id in root_ids:
            data = dict(service_data)
            data["device_id"] = root_id
            try:
                await hass.services.async_call(
                    "portainer", "prune_images", data, blocking=True
                )
            except Exception:
                device = device_reg.async_get(root_id)
                host_name = device.name_by_user or device.name if device else root_id
                _LOGGER.exception(
                    "%s.prune_images: portainer.prune_images failed for host '%s' (%s)",
                    DOMAIN,
                    host_name,
                    root_id,
                )

    if not hass.services.has_service(DOMAIN, SERVICE_PRUNE_IMAGES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_PRUNE_IMAGES,
            handle_prune_images,
            schema=SERVICE_PRUNE_IMAGES_SCHEMA,
        )

    async def handle_perform_update(call: ServiceCall) -> dict:
        update_entity = call.data["update_entity"]
        entity_reg = er.async_get(hass)
        device_reg = dr.async_get(hass)

        reg_entry = entity_reg.async_get(update_entity)
        container_device_id = reg_entry.device_id if reg_entry else None
        if container_device_id is None:
            raise ValueError(f"No device found for entity '{update_entity}'")

        host_id = _walk_to_root(device_reg, container_device_id)
        host_name = _device_name(device_reg, host_id) or "unknown host"
        state = hass.states.get(update_entity)
        friendly_name = (
            state.attributes.get("friendly_name", update_entity) if state else update_entity
        )
        device_name = f"{friendly_name} ({host_name})"

        # Containers whose network_mode is service:<other>/container:<other>
        # (a VPN sidecar like gluetun) hit a confirmed, unfixed Portainer bug
        # here: Docker's daemon rejects the create call over a
        # hostname/network_mode conflict, even though the pull+recreate
        # still actually completes -- reproduced identically via Portainer's
        # own UI, independent of HA or this integration (the daemon's own
        # message is "conflicting options: hostname and the network mode").
        # Swallow *that specific* error rather than aborting the whole
        # update, and follow up with a "restart the stack" notification
        # instead of the normal "update performed" one -- see module
        # docstring, 1c. Any other failure -- including this same error for
        # a standalone container, which the known bug doesn't apply to --
        # is re-raised and fails the update normally, same as before this
        # feature existed. This container's stack (if any) is resolved
        # up front so that decision doesn't depend on interpreting the
        # exception text alone.
        switch_entity_id = _stack_switch_entity_id(hass, container_device_id)
        needs_stack_restart = False
        try:
            await hass.services.async_call(
                "portainer",
                "recreate_container",
                {"container_device_id": container_device_id, "pull_image": True},
                blocking=True,
            )
        except Exception as err:
            is_known_conflict = "conflicting options" in str(err).lower() and "network mode" in str(err).lower()
            if switch_entity_id is None or not is_known_conflict:
                _LOGGER.error(
                    "%s.perform_update: recreate_container failed for '%s' (part of a "
                    "stack: %s; matches the known network_mode:service:X daemon-conflict "
                    "text: %s) -- re-raising, this update did not succeed",
                    DOMAIN,
                    update_entity,
                    switch_entity_id is not None,
                    is_known_conflict,
                )
                raise
            _LOGGER.warning(
                "%s.perform_update: recreate_container hit the known "
                "network_mode:service:X daemon-conflict case for '%s' -- the pull/recreate "
                "itself completed, but the stack needs a restart to fully reconcile",
                DOMAIN,
                update_entity,
            )
            needs_stack_restart = True

        for service in _notify_services_for_entry(hass, entry):
            domain, service_name = service.split(".", 1)
            await hass.services.async_call(
                domain,
                service_name,
                {"message": "clear_notification", "data": {"tag": update_entity}},
            )

        if needs_stack_restart:
            actions_url = hass.data[DOMAIN][entry.entry_id].get("actions_url", f"/{PANEL_PATH}")
            await _async_notify_stack_restart_needed(
                hass, entry, device_name, update_entity, switch_entity_id, actions_url
            )
        else:
            await _async_update_done(hass, entry, device_name, update_entity)

        # Returned via HA's action-response-data feature (supports_response
        # below) so a caller sitting at the webapp -- not just a phone
        # getting the push above -- can react immediately: show its own
        # "this stack needs a restart" prompt instead of waiting on a tap
        # from a notification it never sees. The phone push still happens
        # either way; this is additive, not a replacement for it.
        return {
            "needs_stack_restart": needs_stack_restart,
            "stack_switch_entity_id": switch_entity_id,
        }

    if not hass.services.has_service(DOMAIN, SERVICE_PERFORM_UPDATE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_PERFORM_UPDATE,
            handle_perform_update,
            schema=SERVICE_PERFORM_UPDATE_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    async def handle_update_done(call: ServiceCall) -> None:
        await _async_update_done(hass, entry, call.data["device_name"], call.data["update_entity"])

    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_DONE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_DONE,
            handle_update_done,
            schema=SERVICE_UPDATE_DONE_SCHEMA,
        )

    async def handle_restart_stack(call: ServiceCall) -> None:
        switch_entity_id = call.data["switch_entity_id"]
        _LOGGER.info("%s.restart_stack: restarting stack via %s", DOMAIN, switch_entity_id)
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": switch_entity_id}, blocking=True
        )
        await asyncio.sleep(STACK_RESTART_SETTLE_SECONDS)
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch_entity_id}, blocking=True
        )

    if not hass.services.has_service(DOMAIN, SERVICE_RESTART_STACK):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RESTART_STACK,
            handle_restart_stack,
            schema=SERVICE_RESTART_STACK_SCHEMA,
        )

    await hass.async_add_executor_job(_install_blueprints, hass)

    webapp_url = entry.data[CONF_WEBAPP_URL]

    # The notification click-through URL is a bare RELATIVE path
    # (/PANEL_PATH), not a full URL. The HA companion app treats a
    # relative path as "navigate within the server I'm already connected
    # to" -- so it always opens the sidebar panel in-app, with zero
    # dependency on hass.config.external_url/internal_url (Settings ->
    # System -> Network) and no need to know this instance's own address
    # at all. Earlier versions tried to build an absolute URL from either
    # HA's own configured network URL or the webapp's own URL -- both
    # unnecessary detours around a feature the companion app already
    # provides for exactly this case, and the second one is why tapping a
    # notification opened an external browser instead of the app.
    actions_url = f"/{PANEL_PATH}"
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
            hass.services.async_remove(DOMAIN, SERVICE_PRUNE_IMAGES)
            hass.services.async_remove(DOMAIN, SERVICE_PERFORM_UPDATE)
            hass.services.async_remove(DOMAIN, SERVICE_UPDATE_DONE)
            hass.services.async_remove(DOMAIN, SERVICE_RESTART_STACK)
    return unload_ok
