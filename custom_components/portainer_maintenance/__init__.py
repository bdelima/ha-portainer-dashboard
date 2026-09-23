"""Portainer Maintenance.

On setup this integration:

1. Registers `portainer_maintenance.remove_device`, built on the stable
   `device_registry.async_remove_device()` API.

1b. Registers `portainer_maintenance.perform_update` and
   `portainer_maintenance.update_done` -- native services for actually
   installing an update and posting the "update performed" confirmation.

1c. Registers `portainer_maintenance.restart_stack` -- a stop/start of a
   whole Portainer stack's switch.* entity. Exists for a confirmed,
   unfixed Portainer bug: recreating a single container whose network_mode
   is `service:<other>` / `container:<other>` (a VPN sidecar pattern like
   gluetun) makes Docker's daemon reject the create call over a
   hostname/network_mode conflict -- reproduced identically via
   Portainer's own UI. The pull+recreate still actually completes despite
   the error, but the image tag only reconciles cleanly once the owning
   stack is restarted. `perform_update` detects that case (see
   `_await_recreate_outcome`) without trusting the wrapped exception text,
   by watching the container's own image reference degrade to a bare
   digest.

   (1.3.0) That detection now ALSO runs continuously, independent of any
   particular perform_update call, as part of the broadened
   sensor.portainer_trouble (see sensor.py's PortainerTroubleCoordinator
   and `_find_stuck_containers`) -- so a stack stuck in this state is
   discoverable and actionable (a Restart Stack Now button calling this
   same restart_stack service) from the dashboard's Trouble tab at any
   time, not just in the few minutes right after triggering the update
   that caused it, and it survives an HA/integration restart for free
   (the check is stateless, re-derived from live entity state every poll).
   The phone push this integration sends when it detects the case live
   during perform_update is now purely informational -- it used to carry
   an inline "Restart Stack Now" action, dropped in 1.3.0 in favor of
   sending the user to the Trouble tab, since a stack can have several
   independent per-container update notifications in flight at once and a
   one-tap restart from any one of them made that workflow feel
   disconnected from the others.

1d. Registers `portainer_maintenance.reload_endpoint` (1.3.0) -- reloads
   the core `portainer` config entry that owns a given device, the same
   reload Settings -> Devices & Services -> Portainer -> Reload performs.
   Exists because core's own integration silently drops an endpoint from
   its data the moment it can't reach it -- no error, no dedicated
   "endpoint unavailable" entity anywhere -- which sensor.portainer_trouble
   now surfaces as an actionable item instead of something only noticed by
   accident.

1e. Registers `portainer_maintenance.prune_images` (now optionally scoped
   to specific endpoint(s) via `device_ids`, 1.3.0) and
   `portainer_maintenance.prune_volumes` (1.3.0, new) -- reclaims disk
   space, discovered automatically from the device registry the same way
   as everywhere else in this integration. Both nudge the relevant core
   entities to refresh shortly after acting (see `_refresh_after_action`),
   since core's own `portainer.prune_images` service does not call
   `coordinator.async_request_refresh()` itself the way its button/switch
   entities do.

1f. Registers `portainer_maintenance.hide_update_entities`.

2. Installs its bundled automation blueprint into HA's config dir
   automatically.

3. Registers an iframe sidebar panel pointing at the Portainer actions
   webapp, at a fixed, known path (PANEL_PATH).

4. Computes the notification click-through URL automatically and exposes
   it as a read-only sensor. (1.3.0) A `#trouble` fragment variant is also
   used for the stack-restart-needed push, so tapping it lands the webapp
   directly on its Trouble tab.

5. Forwards to the sensor platform, which defines the tracking sensors
   (updates pending / trouble / stale devices / cleanup, 1.3.0 adds
   cleanup and broadens trouble) as native entities on coordinators.
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import slugify

from .const import CONF_NOTIFY_DEVICES, CONF_WEBAPP_URL, DOMAIN, PANEL_ICON, PANEL_PATH, PANEL_TITLE
from .sensor import (
    _container_image_entity_id,
    _device_name,
    _discover_endpoint_devices,
    _endpoint_images_count_entity,
    _endpoint_reclaimable_entity,
    _endpoint_volume_usage_entity,
    _endpoint_volumes_prune_button,
    _looks_like_bare_digest,
    _portainer_entity_ids,
    _stack_info,
    _walk_to_root,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

STACK_RESTART_SETTLE_SECONDS = 5

RECREATE_WAIT_TIMEOUT_SECONDS = 150
RECREATE_WAIT_POLL_SECONDS = 5

# (1.3.0) How long prune_images/prune_volumes wait after firing the
# underlying action before nudging the relevant entities to refresh, and
# how long they wait after that nudge before returning. homeassistant.
# update_entity blocks until the targeted entity's own coordinator refresh
# completes, so PRE_DELAY exists to give the actual prune a moment to be
# reflected in Portainer/Docker's own state before that refresh is even
# requested; POST_DELAY is pure safety margin on top of an already-blocking
# call, not compensating for an async one.
PRUNE_REFRESH_PRE_DELAY_SECONDS = 3
PRUNE_REFRESH_POST_DELAY_SECONDS = 1

DISMISS_ACTION = {"action": "dismiss", "title": "Dismiss"}

SERVICE_REMOVE_DEVICE = "remove_device"
SERVICE_REMOVE_DEVICE_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})

SERVICE_PRUNE_IMAGES = "prune_images"
SERVICE_PRUNE_IMAGES_SCHEMA = vol.Schema(
    {
        vol.Optional("dangling", default=False): cv.boolean,
        vol.Optional("until_hours"): vol.Coerce(int),
        vol.Optional("device_ids"): [cv.string],
    }
)

SERVICE_PRUNE_VOLUMES = "prune_volumes"
SERVICE_PRUNE_VOLUMES_SCHEMA = vol.Schema({vol.Optional("device_ids"): [cv.string]})

SERVICE_RELOAD_ENDPOINT = "reload_endpoint"
SERVICE_RELOAD_ENDPOINT_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})

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

SERVICE_HIDE_UPDATE_ENTITIES = "hide_update_entities"
SERVICE_HIDE_UPDATE_ENTITIES_SCHEMA = vol.Schema({})

BUNDLED_BLUEPRINTS_DIR = Path(__file__).parent / "bundled_blueprints"

BLUEPRINT_FILES = [
    (
        "automation/portainer_automations.yaml",
        f"blueprints/automation/{DOMAIN}/portainer_automations.yaml",
    ),
]


def _notify_services_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
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
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    _stack_name, switch_entity_id = _stack_info(device_reg, entity_reg, container_device_id)
    return switch_entity_id


async def _refresh_after_action(hass: HomeAssistant, entity_ids: list[str | None]) -> None:
    """Fire-then-nudge shared by prune_images and prune_volumes -- see
    PRUNE_REFRESH_PRE_DELAY_SECONDS/POST_DELAY_SECONDS above for the
    reasoning. Silently does nothing if none of the target entities were
    found (e.g. a suffix-matching assumption in sensor.py didn't hold on
    this particular HA version) -- a missing refresh target should never
    fail the underlying prune action itself."""
    targets = [e for e in entity_ids if e]
    if not targets:
        return
    await asyncio.sleep(PRUNE_REFRESH_PRE_DELAY_SECONDS)
    try:
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": targets}, blocking=True
        )
    except Exception:
        _LOGGER.debug("%s: update_entity refresh nudge failed for %s", DOMAIN, targets, exc_info=True)
    await asyncio.sleep(PRUNE_REFRESH_POST_DELAY_SECONDS)


async def _await_recreate_outcome(
    hass: HomeAssistant,
    image_entity_id: str | None,
    image_before: str | None,
    update_entity: str,
) -> bool:
    """Called only after recreate_container has already raised for a
    container that IS part of a stack (see handle_perform_update) --
    decides whether that's the known network_mode:service:X daemon
    conflict or a genuine failure, WITHOUT trusting the exception text.

    Confirmed in production that HA core's own portainer integration wraps
    every recreate_container failure, regardless of cause, into the same
    generic HomeAssistantError -- the actual Docker/Portainer error text
    never survives to reach this code at all. Instead, this watches what
    actually happens to the container's own image reference: the
    pull+recreate genuinely can complete despite the daemon-level create
    call erroring, and when it does, the image reference degrades to a
    bare content digest instead of a normal tag.
      - the image reference changes to something that looks like a bare
        digest -> this is that known conflict; return True.
      - it changes to anything else (a normal-looking tag) -> the recreate
        apparently completed cleanly despite the earlier exception; return
        False.
      - it never changes at all within RECREATE_WAIT_TIMEOUT_SECONDS ->
        genuinely failed; raise rather than guess.
    """
    if image_entity_id is None:
        _LOGGER.warning(
            "%s.perform_update: recreate_container raised for '%s' but no "
            "sensor.<name>_image entity was found to watch -- assuming the "
            "known network_mode:service:X conflict rather than a genuine "
            "failure, since that's the only case this recovery path exists "
            "for",
            DOMAIN,
            update_entity,
        )
        return True

    elapsed = 0
    while elapsed < RECREATE_WAIT_TIMEOUT_SECONDS:
        await asyncio.sleep(RECREATE_WAIT_POLL_SECONDS)
        elapsed += RECREATE_WAIT_POLL_SECONDS
        state = hass.states.get(image_entity_id)
        current = state.state if state else None
        if current is None or current == image_before:
            continue
        if _looks_like_bare_digest(current):
            _LOGGER.info(
                "%s.perform_update: '%s' image reference degraded to a bare "
                "digest ('%s') %ds after the recreate error -- confirms the "
                "known network_mode:service:X conflict; stack restart needed",
                DOMAIN,
                update_entity,
                current,
                elapsed,
            )
            return True
        _LOGGER.info(
            "%s.perform_update: '%s' image reference changed to '%s' %ds "
            "after the recreate error and looks like a normal tag -- "
            "treating that error as transient, no stack restart needed",
            DOMAIN,
            update_entity,
            current,
            elapsed,
        )
        return False

    _LOGGER.error(
        "%s.perform_update: '%s' image reference never changed from '%s' "
        "within %ds of the recreate error -- this does not look like the "
        "known network_mode:service:X conflict; treating it as a genuine "
        "failure",
        DOMAIN,
        update_entity,
        image_before,
        RECREATE_WAIT_TIMEOUT_SECONDS,
    )
    raise HomeAssistantError(
        f"perform_update: recreate_container failed for {update_entity} and its "
        f"image reference never changed within {RECREATE_WAIT_TIMEOUT_SECONDS}s -- "
        f"this does not look like the known network_mode:service:X conflict"
    )


async def _async_update_done(
    hass: HomeAssistant, entry: ConfigEntry, device_name: str, update_entity: str
) -> None:
    """Shared finishing logic for a completed update: a persistent_notification
    plus a real phone push to every configured notify device."""
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
                "data": {
                    "tag": f"portainer_update_done_{update_entity.replace('.', '_')}",
                    # (1.3.0) every notification this integration sends now
                    # carries an explicit no-op Dismiss action -- tapping
                    # any action clears a notification from the tray, this
                    # just gives an explicit "I saw this, nothing to do"
                    # option alongside whatever real action(s) exist.
                    "actions": [DISMISS_ACTION],
                },
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
    known network_mode:service:X daemon-conflict error -- so the
    container's image tag won't fully reconcile until its stack is
    restarted.

    (1.3.0) This is now purely informational: no inline "Restart Stack
    Now" action. The actual remediation lives on the dashboard's Trouble
    tab (a stack can have several independent per-container update
    notifications in flight at once, and a one-tap restart baked into any
    one of them made that workflow feel disconnected from the others) --
    tapping this notification's action opens the dashboard directly on
    that tab via a `#trouble` URL fragment.
    """
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
        f"another container's network). Restart it from the dashboard's Trouble "
        f"tab whenever convenient."
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

    trouble_url = f"{actions_url}#trouble"
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
                        {"action": "URI", "title": "Open Trouble Tab", "uri": trouble_url},
                        DISMISS_ACTION,
                    ],
                },
            },
        )


def _install_blueprints(hass: HomeAssistant) -> None:
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
        requested_device_ids = call.data.get("device_ids")

        entity_reg = er.async_get(hass)
        device_reg = dr.async_get(hass)

        if requested_device_ids:
            root_ids = {
                rid for rid in (_walk_to_root(device_reg, d) for d in requested_device_ids) if rid
            }
        else:
            root_ids = _discover_endpoint_devices(entity_reg, device_reg)

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
                continue

            await _refresh_after_action(
                hass,
                [
                    _endpoint_images_count_entity(entity_reg, root_id),
                    _endpoint_reclaimable_entity(entity_reg, root_id),
                ],
            )

    if not hass.services.has_service(DOMAIN, SERVICE_PRUNE_IMAGES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_PRUNE_IMAGES,
            handle_prune_images,
            schema=SERVICE_PRUNE_IMAGES_SCHEMA,
        )

    async def handle_prune_volumes(call: ServiceCall) -> None:
        """(1.3.0) Core has no `portainer.prune_volumes` service -- only a
        `button.<endpoint>_volumes_prune` entity (ENDPOINT_BUTTONS in
        core's button.py). button.press is the standard, publicly
        supported way to trigger any button entity, so that's what this
        wraps rather than reaching into core's internals for a service
        that doesn't exist."""
        requested_device_ids = call.data.get("device_ids")

        entity_reg = er.async_get(hass)
        device_reg = dr.async_get(hass)

        if requested_device_ids:
            root_ids = {
                rid for rid in (_walk_to_root(device_reg, d) for d in requested_device_ids) if rid
            }
        else:
            root_ids = _discover_endpoint_devices(entity_reg, device_reg)

        if not root_ids:
            _LOGGER.warning(
                "%s.prune_volumes: no Portainer endpoint devices found -- nothing to prune",
                DOMAIN,
            )
            return

        for root_id in root_ids:
            button_entity = _endpoint_volumes_prune_button(entity_reg, root_id)
            if button_entity is None:
                device = device_reg.async_get(root_id)
                host_name = device.name_by_user or device.name if device else root_id
                _LOGGER.warning(
                    "%s.prune_volumes: no volumes_prune button entity found for host '%s' -- skipping",
                    DOMAIN,
                    host_name,
                )
                continue
            try:
                await hass.services.async_call(
                    "button", "press", {"entity_id": button_entity}, blocking=True
                )
            except Exception:
                device = device_reg.async_get(root_id)
                host_name = device.name_by_user or device.name if device else root_id
                _LOGGER.exception(
                    "%s.prune_volumes: pressing %s failed for host '%s'",
                    DOMAIN,
                    button_entity,
                    host_name,
                )
                continue

            await _refresh_after_action(hass, [_endpoint_volume_usage_entity(entity_reg, root_id)])

    if not hass.services.has_service(DOMAIN, SERVICE_PRUNE_VOLUMES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_PRUNE_VOLUMES,
            handle_prune_volumes,
            schema=SERVICE_PRUNE_VOLUMES_SCHEMA,
        )

    async def handle_reload_endpoint(call: ServiceCall) -> None:
        """(1.3.0) Reloads the core `portainer` config entry that owns the
        given device -- the same reload Settings -> Devices & Services ->
        Portainer -> Reload performs. Core gives no service for this at
        all, only that manual frontend button."""
        device_id = call.data["device_id"]
        device_reg = dr.async_get(hass)
        device = device_reg.async_get(device_id)
        if device is None:
            raise ValueError(f"No device found with id '{device_id}'")

        portainer_entry_id = None
        for config_entry_id in device.config_entries:
            candidate = hass.config_entries.async_get_entry(config_entry_id)
            if candidate is not None and candidate.domain == "portainer":
                portainer_entry_id = config_entry_id
                break

        if portainer_entry_id is None:
            raise ValueError(f"Device '{device_id}' has no owning 'portainer' config entry")

        _LOGGER.info(
            "%s.reload_endpoint: reloading portainer config entry %s for device %s",
            DOMAIN,
            portainer_entry_id,
            device_id,
        )
        await hass.config_entries.async_reload(portainer_entry_id)

    if not hass.services.has_service(DOMAIN, SERVICE_RELOAD_ENDPOINT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RELOAD_ENDPOINT,
            handle_reload_endpoint,
            schema=SERVICE_RELOAD_ENDPOINT_SCHEMA,
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
        container_name = _device_name(device_reg, container_device_id) or (
            state.attributes.get("friendly_name", update_entity) if state else update_entity
        )
        device_name = f"{container_name} ({host_name})"

        switch_entity_id = _stack_switch_entity_id(hass, container_device_id)
        image_entity_id = _container_image_entity_id(hass, entity_reg, container_device_id)
        image_before_state = hass.states.get(image_entity_id) if image_entity_id else None
        image_before = image_before_state.state if image_before_state else None

        needs_stack_restart = False
        try:
            await hass.services.async_call(
                "portainer",
                "recreate_container",
                {"container_device_id": container_device_id, "pull_image": True},
                blocking=True,
            )
        except Exception as err:
            if switch_entity_id is None:
                _LOGGER.error(
                    "%s.perform_update: recreate_container failed for '%s' "
                    "(standalone container, no owning stack to fall back on) "
                    "-- re-raising, this update did not succeed: %s",
                    DOMAIN,
                    update_entity,
                    err,
                )
                raise
            _LOGGER.warning(
                "%s.perform_update: recreate_container raised for '%s' (part "
                "of a stack) -- deferring judgment to what its image "
                "reference actually does over the next %ds, rather than "
                "trusting the exception text: %s",
                DOMAIN,
                update_entity,
                RECREATE_WAIT_TIMEOUT_SECONDS,
                err,
            )
            needs_stack_restart = await _await_recreate_outcome(
                hass, image_entity_id, image_before, update_entity
            )

        coordinators = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("coordinators", {})
        updates_coordinator = coordinators.get("updates")
        if updates_coordinator is not None:
            updates_coordinator.mark_recently_updated(update_entity)
            await updates_coordinator.async_request_refresh()

        # (1.3.0) The broadened Trouble sensor's stuck-container check is
        # stateless (re-derived from live entity state, see sensor.py's
        # _find_stuck_containers), so it doesn't strictly need to be told
        # this happened -- but nudging its own refresh here means the
        # Trouble tab reflects it on the next moment rather than waiting
        # out its own 1-minute poll interval.
        trouble_coordinator = coordinators.get("trouble")
        if needs_stack_restart and trouble_coordinator is not None:
            await trouble_coordinator.async_request_refresh()

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

    async def handle_hide_update_entities(call: ServiceCall) -> dict:
        entity_reg = er.async_get(hass)
        scanned = 0
        hidden = 0
        for entity_id in _portainer_entity_ids(entity_reg):
            if not entity_id.startswith("update."):
                continue
            scanned += 1
            reg_entry = entity_reg.async_get(entity_id)
            if reg_entry is None or reg_entry.hidden_by is not None:
                continue
            entity_reg.async_update_entity(entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION)
            hidden += 1
            _LOGGER.info("%s.hide_update_entities: hid %s (was visible)", DOMAIN, entity_id)

        if hidden:
            _LOGGER.info(
                "%s.hide_update_entities: hid %d of %d update entities scanned",
                DOMAIN,
                hidden,
                scanned,
            )
        return {"scanned": scanned, "hidden": hidden}

    if not hass.services.has_service(DOMAIN, SERVICE_HIDE_UPDATE_ENTITIES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_HIDE_UPDATE_ENTITIES,
            handle_hide_update_entities,
            schema=SERVICE_HIDE_UPDATE_ENTITIES_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    await hass.async_add_executor_job(_install_blueprints, hass)

    webapp_url = entry.data[CONF_WEBAPP_URL]

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
            hass.services.async_remove(DOMAIN, SERVICE_PRUNE_VOLUMES)
            hass.services.async_remove(DOMAIN, SERVICE_RELOAD_ENDPOINT)
            hass.services.async_remove(DOMAIN, SERVICE_PERFORM_UPDATE)
            hass.services.async_remove(DOMAIN, SERVICE_UPDATE_DONE)
            hass.services.async_remove(DOMAIN, SERVICE_RESTART_STACK)
            hass.services.async_remove(DOMAIN, SERVICE_HIDE_UPDATE_ENTITIES)
    return unload_ok
