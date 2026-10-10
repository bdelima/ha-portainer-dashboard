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

1g. Registers `portainer_maintenance.dismiss_trouble_item` -- hides a
   Needs Remediation item the user can't act on from the dashboard (see
   dismissals.py for how long a dismissal lasts). `perform_update` also
   refuses Portainer's own containers, the server and the agent: recreating
   one from inside Portainer stops it before the replacement starts (see
   sensor.py's `_portainer_component`); their updates are listed on the
   trouble sensor instead.

1h. Registers `portainer_maintenance.update_portainer` -- updates Portainer's
   own server or agent by starting Portainer's `portainer-updater` helper
   container on the same host (see `handle_update_portainer`), the one
   route that does not depend on the container being updated. It is only
   ever started on request; nothing here calls it automatically.

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
import re
import shutil
import time
import uuid
from datetime import timedelta
from pathlib import Path

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import slugify

from .const import (
    CONF_ADMIN_ONLY,
    CONF_NOTIFY_DEVICES,
    CONF_WEBAPP_URL,
    DEFAULT_ADMIN_ONLY,
    DOMAIN,
    EVENT_STACK_RESTART_NEEDED,
    EVENT_UPDATE_DONE,
    PANEL_ICON,
    PANEL_PATH,
    PANEL_TITLE,
)
from .dismissals import DismissalStore
from .sensor import (
    _container_image_entity_id,
    _device_name,
    _discover_endpoint_devices,
    _endpoint_images_count_entity,
    _endpoint_reclaimable_entity,
    _endpoint_volume_usage_entity,
    _endpoint_volumes_prune_button,
    _looks_like_bare_digest,
    _portainer_component,
    _portainer_entity_ids,
    _PORTAINER_MANUAL_STEPS,
    _portainer_subject,
    _stack_info,
    _walk_to_root,
)
from .sidecar_auth import panel_url

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

SERVICE_DISMISS_TROUBLE_ITEM = "dismiss_trouble_item"
SERVICE_DISMISS_TROUBLE_ITEM_SCHEMA = vol.Schema({vol.Required("dismiss_key"): cv.string})

SERVICE_UPDATE_PORTAINER = "update_portainer"
SERVICE_UPDATE_PORTAINER_SCHEMA = vol.Schema(
    {
        vol.Required("update_entity"): cv.entity_id,
        vol.Optional("dry_run", default=False): cv.boolean,
    }
)

# Core Home Assistant's own Portainer integration (not this one).
CORE_PORTAINER_DOMAIN = "portainer"

# Portainer's helper image for updating Portainer in place. Its source is
# github.com/portainer/portainer-updater; the command lines below are the
# ones its README documents ("portainer --image=<ref>" for the server,
# "agent <schedule-id> <image>" for the agent).
#
# The helper is pulled at the same version as the Portainer container being
# updated, never ":latest": its Docker Hub "latest" tag stopped moving in
# August 2024, so it is a build that predates flags the current helper has
# (--health-check was added in September 2025), and a helper that does not
# know a flag rejects it at argument parsing and exits at once. Its versioned
# tags are published alongside Portainer's own releases. The version comes
# from the Portainer API's system/status (the Portainer image sets no
# org.opencontainers.image.version label, so core has no "image version"
# sensor for it).
PORTAINER_UPDATER_REPO = "portainer/portainer-updater"
PORTAINER_UPDATER_SOCKET_BIND = "/var/run/docker.sock:/var/run/docker.sock"
PORTAINER_UPDATER_PULL_TIMEOUT = timedelta(minutes=5)
# A second start for the same Portainer update entity inside this window is
# refused, so a double click or a retried call can't run two helpers against
# the same container at once. In memory only, and it hides nothing: the
# update stays listed for as long as core's update entity says it is on.
PORTAINER_UPDATER_REPEAT_GUARD_SECONDS = 600

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


# ---------------------------------------------------------------------------
# Updating Portainer's own server and agent
#
# perform_update refuses these (recreating them from inside Portainer stops
# the process doing the recreate). Portainer's own answer is a short-lived
# helper container, `portainer-updater`, that runs on the same Docker host
# with the Docker socket mounted, so it is not affected when the container
# it replaces is stopped. update_portainer starts that helper through the
# Portainer API and then returns; it does not wait for the update, because
# the thing it would be waiting on is the Portainer connection that goes away
# while the update runs.
# ---------------------------------------------------------------------------
def _updater_target_image(image_ref: str | None, container_name: str, host: str) -> str:
    """The image reference to hand the updater: the container's own current
    reference (repo and tag), which the updater pulls afresh. Refuses what
    can't be updated that way."""
    ref = (image_ref or "").strip()
    where = f"{container_name} on {host}"
    if not ref or ref in ("unknown", "unavailable") or _looks_like_bare_digest(ref):
        raise HomeAssistantError(
            f"Can't tell which image {where} runs (its image sensor reports "
            f"'{ref or 'nothing'}'), so there is nothing to tell the updater to pull. "
            "Update it manually instead."
        )
    if "@" in ref:
        raise HomeAssistantError(
            f"{where} is pinned to a digest ({ref}), which never changes, so an update "
            "to it means changing the reference itself. Update it manually instead."
        )
    if ":" not in ref.rsplit("/", 1)[-1]:
        raise HomeAssistantError(
            f"{where} runs '{ref}' with no tag. Recreate it with an explicit tag "
            "(for example :lts or a version) and update it manually this once."
        )
    return ref


def _updater_command(
    component: str, image: str, schedule_id: str, health_check: bool = False
) -> list[str]:
    """The portainer-updater command line (its entrypoint is the updater
    binary, so this is the container's Cmd).

    health_check adds --health-check to the server command. With it, after
    starting the new Portainer the updater runs the new image's own
    "/portainer --health-check" until it passes, and if it never does (or the
    new container fails to start) it also rolls the Portainer database back to
    its pre-update state, then removes the new container and restarts the
    old one. Without it the updater still restarts the old container on a
    failed start or failed Docker health check, but does not roll the
    database back, which matters when the new version has already migrated
    it. The updater retries that check for up to a few hours, so a check
    that can never pass leaves the update hanging and ends in a rollback of
    an update that worked: that happened with a Portainer EE image whose
    "--health-check" crashed on a FIPS initialisation error. The flag is
    therefore only passed for a container that has a Docker health status of
    its own (see _container_has_health_status), and a helper that predates the
    flag rejects it at parse time, which is why the helper is pinned to the
    running version rather than "latest". The agent command takes no such
    flag (the updater checks a new agent on its own), so none is passed
    there."""
    if component == "agent":
        return ["agent", schedule_id, image]
    command = ["portainer", f"--image={image}"]
    if health_check:
        command.append("--health-check")
    return command


_PLAIN_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")


def _updater_image(
    running_version: str | None,
    container_name: str,
    host: str,
    target_image: str,
) -> str:
    """The portainer-updater image to run: the updater tagged with the version
    Portainer reports it is running now. Refuses rather than guess when that
    version can't be read cleanly, because the fallback (":latest") is a build
    from 2024; the message is then the manual instruction for the user."""
    version = (running_version or "").strip()
    match = _PLAIN_VERSION_RE.match(version)
    if not match:
        raise HomeAssistantError(
            f"Couldn't read a clean Portainer version from the Portainer API "
            f"(it reported '{version or 'nothing'}'), so there is no matching "
            f"portainer-updater image to use and nothing was started. Update "
            f"{container_name} on {host} by hand: use the update prompt in "
            f"Portainer's own web UI, or pull {target_image} on {host} and "
            "recreate the container with its existing settings."
        )
    return f"{PORTAINER_UPDATER_REPO}:{match.group(1)}"


async def _running_portainer_version(coordinator: object) -> str | None:
    """The version of the Portainer server core talks to, from its
    system/status endpoint (one cheap request; no GitHub lookup). The agent's
    own version isn't reported there; agents are meant to match the server.
    None when the request fails or reports no version."""
    try:
        status = await coordinator.portainer.portainer_system_status()
    except Exception as err:  # noqa: BLE001 - any failure means "can't tell"
        _LOGGER.warning(
            "%s.update_portainer: couldn't read the Portainer version from "
            "system/status: %s: %s",
            DOMAIN, type(err).__name__, err,
        )
        return None
    return getattr(status, "version", None)


def _container_has_health_status(container_data: object) -> bool:
    """True when Docker reports a health status for the container (it has a
    HEALTHCHECK: core then shows a health sensor on its device and the
    container is "healthy" rather than just "running")."""
    inspect = getattr(container_data, "container_inspect", None)
    state = getattr(inspect, "state", None)
    return getattr(state, "health", None) is not None


def _core_container_data(
    hass: HomeAssistant, device_reg: dr.DeviceRegistry, container_device_id: str
) -> tuple[object, int, object]:
    """(core coordinator, endpoint id, core's data for the container) for a
    container device, resolved the way core's own portainer.recreate_container
    service does."""
    device = device_reg.async_get(container_device_id)
    if device is not None:
        for core_entry in hass.config_entries.async_entries(CORE_PORTAINER_DOMAIN):
            coordinator = getattr(core_entry, "runtime_data", None)
            if coordinator is None or not getattr(coordinator, "data", None):
                continue
            for data in coordinator.data.values():
                for container_name, container_data in data.containers.items():
                    identifier = (
                        CORE_PORTAINER_DOMAIN,
                        f"{core_entry.entry_id}_{data.endpoint.id}_{container_name}",
                    )
                    if identifier in device.identifiers:
                        return coordinator, data.endpoint.id, container_data
    raise HomeAssistantError(
        "Couldn't match this container to one of core Portainer's current containers "
        "(is the Portainer integration loaded, and is the host reachable?)."
    )


def _core_container_target(
    hass: HomeAssistant, device_reg: dr.DeviceRegistry, container_device_id: str
) -> tuple[object, int, str]:
    """(core coordinator, endpoint id, container id) for a container device."""
    coordinator, endpoint_id, container_data = _core_container_data(
        hass, device_reg, container_device_id
    )
    return coordinator, endpoint_id, container_data.container.id


async def _refresh_after_action(
    hass: HomeAssistant,
    entity_ids: list[str | None],
    also_refresh: DataUpdateCoordinator | None = None,
) -> None:
    """Fire-then-nudge shared by prune_images and prune_volumes -- see
    PRUNE_REFRESH_PRE_DELAY_SECONDS/POST_DELAY_SECONDS above for the
    reasoning. Silently does nothing if none of the target entities were
    found (e.g. a suffix-matching assumption in sensor.py didn't hold on
    this particular HA version) -- a missing refresh target should never
    fail the underlying prune action itself.

    (1.3.1) The update_entity nudge only refreshes core's own Portainer
    diagnostic sensors (images_count, reclaimable, volume_usage) from
    Portainer's API -- it says nothing to OUR OWN PortainerCleanupCoordinator,
    which is what the sidecar's Cleanup tab actually reads, and which
    otherwise only recomputes on its own 5-minute poll (see
    PortainerCleanupCoordinator in sensor.py). Without also_refresh, a prune
    action could nudge the upstream sensors successfully and still leave the
    Cleanup tab showing a stale, unchanged count for up to 5 minutes --
    exactly the "button reactivated way before the count updated" report
    this parameter exists to fix. also_refresh is awaited, so the sidecar's
    blocking HTTP call doesn't return (and the button re-enable with it)
    until the tab's own numbers are actually caught up.
    """
    targets = [e for e in entity_ids if e]
    if targets:
        await asyncio.sleep(PRUNE_REFRESH_PRE_DELAY_SECONDS)
        try:
            await hass.services.async_call(
                "homeassistant", "update_entity", {"entity_id": targets}, blocking=True
            )
        except Exception:
            _LOGGER.debug("%s: update_entity refresh nudge failed for %s", DOMAIN, targets, exc_info=True)
    if also_refresh is not None:
        try:
            await also_refresh.async_request_refresh()
        except Exception:
            _LOGGER.debug("%s: coordinator refresh nudge failed", DOMAIN, exc_info=True)
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
    """Shared finishing logic for a completed update: fire
    EVENT_UPDATE_DONE. The bundled blueprint turns it into the phone push and
    the notification-panel entry, so both follow the Updates category's
    channel and toggles (they used to be sent from here, on the phone's
    default channel and with no toggles). Without the automation installed
    nothing is sent."""
    slug = update_entity.replace(".", "_")
    hass.bus.async_fire(
        EVENT_UPDATE_DONE,
        {
            "device_name": device_name,
            "update_entity": update_entity,
            "finished_at": dt_util.now().strftime("%Y-%m-%d %H:%M"),
            "notification_id": f"portainer_update_{slug}",
            "tag": f"portainer_update_done_{slug}",
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

    (1.3.0) This is purely informational: no inline "Restart Stack Now"
    action. The actual remediation lives on the dashboard's Needs
    Remediation tab (a stack can have several independent per-container
    update notifications in flight at once, and a one-tap restart baked
    into any one of them made that workflow feel disconnected from the
    others).

    Fires EVENT_STACK_RESTART_NEEDED; the bundled blueprint builds the push
    (whose action opens that tab) and the notification-panel entry under the
    Trouble category's channel and toggles. `actions_url` is kept for the
    callers' sake; the blueprint reads the URL from sensor.portainer_actions_url.
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

    slug = update_entity.replace(".", "_")
    now_str = dt_util.now().strftime("%Y-%m-%d %H:%M")
    message = (
        f"{device_name} was updated on {now_str}, but its stack needs a restart "
        f"to finish cleanly (known Portainer limitation for containers sharing "
        f"another container's network). Restart it from the dashboard's Needs "
        f"Remediation tab whenever convenient."
    )
    hass.bus.async_fire(
        EVENT_STACK_RESTART_NEEDED,
        {
            "device_name": device_name,
            "update_entity": update_entity,
            "finished_at": now_str,
            "message": message,
            "notification_id": f"portainer_update_{slug}",
            "tag": f"portainer_update_done_{slug}",
        },
    )


def _install_blueprints(hass: HomeAssistant) -> None:
    for src_rel, dest_rel in BLUEPRINT_FILES:
        src = BUNDLED_BLUEPRINTS_DIR / src_rel
        dest = Path(hass.config.path(dest_rel))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
        _LOGGER.debug("Installed blueprint %s -> %s", src, dest)


def _register_panel(hass: HomeAssistant, webapp_url: str, require_admin: bool = DEFAULT_ADMIN_ONLY) -> None:
    """Register the sidebar iframe panel.

    require_admin hides the sidebar entry (and the panel itself) from
    non-administrator Home Assistant users -- the same flag HACS registers
    its own panel with. It does not protect the webapp's URL, which stays
    reachable by anyone who can reach it directly. Home Assistant's
    Settings -> Dashboards page only lists its built-in panels and
    user-created dashboards, so this flag can't be flipped there for an
    integration's panel; it's set from this integration's own
    Reconfigure form instead (CONF_ADMIN_ONLY).
    """
    kwargs = {
        "component_name": "iframe",
        "sidebar_title": PANEL_TITLE,
        "sidebar_icon": PANEL_ICON,
        "frontend_url_path": PANEL_PATH,
        "config": {"url": webapp_url},
        "require_admin": require_admin,
    }
    try:
        frontend.async_register_built_in_panel(hass, **kwargs)
    except ValueError:
        frontend.async_remove_panel(hass, PANEL_PATH)
        frontend.async_register_built_in_panel(hass, **kwargs)


# Unique ids of sensors this integration once provided and no longer does. Each
# is {entry_id}_{object_id}; the registry entry outlives the sensor, so Home
# Assistant keeps listing it as "no longer being provided by the
# portainer_maintenance integration" until it is deleted by hand.
LEGACY_SENSOR_OBJECT_IDS = (
    # (1.3.0, breaking rename) became SENSOR_TROUBLE.
    "portainer_container_trouble",
)


def _async_remove_legacy_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the registry entries of sensors this integration used to provide
    (see LEGACY_SENSOR_OBJECT_IDS). Matched on the exact platform and unique
    id, so only this config entry's own leftover is touched; nothing that is
    currently provided can match, since no live sensor uses those ids."""
    entity_reg = er.async_get(hass)
    for object_id in LEGACY_SENSOR_OBJECT_IDS:
        entity_id = entity_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{object_id}")
        if entity_id is None:
            continue
        entity_reg.async_remove(entity_id)
        _LOGGER.info("Removed the leftover entity %s (this integration no longer provides it)", entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Portainer Maintenance from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    _async_remove_legacy_entities(hass, entry)

    # A dismissal outlives restarts, but is only cleared by a Home Assistant
    # start (not by reloading this integration at runtime) once it is old
    # enough -- see dismissals.py.
    dismissals = DismissalStore(hass)
    await dismissals.async_load(ha_starting=hass.state is not CoreState.running)
    hass.data[DOMAIN][entry.entry_id]["dismissals"] = dismissals

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
        cleanup_coordinator = (
            hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("coordinators", {}).get("cleanup")
        )

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
                also_refresh=cleanup_coordinator,
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
        cleanup_coordinator = (
            hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("coordinators", {}).get("cleanup")
        )

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

            await _refresh_after_action(
                hass,
                [_endpoint_volume_usage_entity(entity_reg, root_id)],
                also_refresh=cleanup_coordinator,
            )

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

        # Portainer's server and agent can't recreate themselves (see
        # sensor.py's _portainer_component): refuse rather than leave them
        # stopped. Applies to every caller -- the dashboard, the blueprint's
        # notification action, scripts.
        portainer_component = _portainer_component(hass, entity_reg, container_device_id)
        if portainer_component is not None:
            raise HomeAssistantError(
                f"{_portainer_subject(host_name, portainer_component)} can't be updated with "
                f"{DOMAIN}.{SERVICE_PERFORM_UPDATE}: recreating it from inside Portainer stops "
                f"the process doing the recreate and leaves it stopped. Use "
                f"{DOMAIN}.{SERVICE_UPDATE_PORTAINER} instead. {_PORTAINER_MANUAL_STEPS}"
            )

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

    async def handle_update_portainer(call: ServiceCall) -> dict:
        update_entity = call.data["update_entity"]
        dry_run = call.data.get("dry_run", False)
        entity_reg = er.async_get(hass)
        device_reg = dr.async_get(hass)

        reg_entry = entity_reg.async_get(update_entity)
        container_device_id = reg_entry.device_id if reg_entry else None
        if container_device_id is None:
            raise ValueError(f"No device found for entity '{update_entity}'")

        component = _portainer_component(hass, entity_reg, container_device_id)
        if component is None:
            raise HomeAssistantError(
                f"'{update_entity}' is not Portainer's own server or agent. "
                f"Use {DOMAIN}.{SERVICE_PERFORM_UPDATE} for other containers."
            )

        host_id = _walk_to_root(device_reg, container_device_id)
        host_name = _device_name(device_reg, host_id) or "unknown host"
        state = hass.states.get(update_entity)
        container_name = _device_name(device_reg, container_device_id) or (
            state.attributes.get("friendly_name", update_entity) if state else update_entity
        )
        update_pending = state is not None and state.state == "on"
        if not update_pending and not dry_run:
            raise HomeAssistantError(
                f"No update is pending for {container_name} on {host_name} "
                f"({update_entity} is '{state.state if state else 'missing'}')."
            )

        image_entity_id = _container_image_entity_id(hass, entity_reg, container_device_id)
        image_state = hass.states.get(image_entity_id) if image_entity_id else None
        target_image = _updater_target_image(
            image_state.state if image_state else None, container_name, host_name
        )
        coordinator, endpoint_id, container_data = _core_container_data(
            hass, device_reg, container_device_id
        )
        container_id = container_data.container.id
        updater_image = _updater_image(
            await _running_portainer_version(coordinator),
            container_name,
            host_name,
            target_image,
        )
        health_check = _container_has_health_status(container_data)

        schedule_id = str(int(time.time()))
        command = _updater_command(component, target_image, schedule_id, health_check)
        updater_name = f"portainer-maintenance-updater-{uuid.uuid4().hex[:8]}"
        plan = {
            "component": component,
            "container": container_name,
            "host": host_name,
            "endpoint_id": endpoint_id,
            "container_id": container_id,
            "target_image": target_image,
            "updater_image": updater_image,
            "updater_name": updater_name,
            "command": command,
            "health_check": health_check,
            "update_pending": update_pending,
        }
        if dry_run:
            return {**plan, "dry_run": True, "started": False}

        # Keyed on the update entity: the container id changes when the update
        # works, the entity does not.
        started = hass.data[DOMAIN][entry.entry_id].setdefault("updater_started", {})
        trouble = hass.data[DOMAIN][entry.entry_id].get("coordinators", {}).get("trouble")
        now = time.monotonic()
        last = started.get(update_entity)
        if trouble is not None and trouble.is_update_tracked(update_entity):
            raise HomeAssistantError(
                f"An update of {container_name} on {host_name} is already being followed "
                "(see the Updates row on the Needs Remediation tab)."
            )
        if last is not None and now - last < PORTAINER_UPDATER_REPEAT_GUARD_SECONDS:
            raise HomeAssistantError(
                f"An update of {container_name} on {host_name} was started "
                f"{int(now - last)} s ago and may still be running. Wait a few minutes "
                "and check the host before starting another."
            )
        # Claimed before the first await so two overlapping calls can't both start.
        started[update_entity] = now

        portainer = coordinator.portainer
        updater_container_id: str | None = None
        step = f"pulling the updater image {updater_image}"
        try:
            await portainer.image_recreate(
                endpoint_id=endpoint_id,
                image_id=updater_image,
                timeout=PORTAINER_UPDATER_PULL_TIMEOUT,
            )
            step = "creating the updater container"
            created = await portainer.container_create(
                endpoint_id=endpoint_id,
                name=updater_name,
                image=updater_image,
                config={
                    "Cmd": command,
                    "HostConfig": {
                        "Binds": [PORTAINER_UPDATER_SOCKET_BIND],
                        "AutoRemove": True,
                    },
                },
            )
            updater_container_id = created.id
            step = "starting the updater container"
            await portainer.start_container(
                endpoint_id=endpoint_id, container_id=updater_container_id
            )
        except Exception as err:
            started.pop(update_entity, None)
            _LOGGER.error(
                "%s.update_portainer: failed while %s for '%s' on %s: %s",
                DOMAIN, step, container_name, host_name, err,
            )
            if updater_container_id is not None:
                # Created but never started, so AutoRemove won't clear it.
                try:
                    await portainer.delete_container(
                        endpoint_id=endpoint_id, container_id=updater_container_id, force=True
                    )
                except Exception as cleanup_err:  # noqa: BLE001 - best effort
                    _LOGGER.warning(
                        "%s.update_portainer: couldn't remove the unstarted updater %s: %s",
                        DOMAIN, updater_name, cleanup_err,
                    )
            raise HomeAssistantError(
                f"Updating {container_name} on {host_name} failed while {step}: {err}"
            ) from err

        _LOGGER.warning(
            "%s.update_portainer: started %s (%s, image %s, command %s) on %s to update "
            "'%s' to %s. Portainer will be unavailable while it restarts.",
            DOMAIN, updater_name, updater_container_id, updater_image, " ".join(command),
            host_name, container_name, target_image,
        )
        if trouble is not None:
            trouble.start_update_tracking(
                entity=update_entity,
                device_id=container_device_id,
                component=component,
                container_id=container_id,
                helper_id=updater_container_id,
                helper_name=updater_name,
                health_check=health_check,
                core_coordinator=coordinator,
            )
            await trouble.async_request_refresh()
        return {
            **plan,
            "dry_run": False,
            "started": True,
            "updater_container_id": updater_container_id,
        }

    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_PORTAINER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_PORTAINER,
            handle_update_portainer,
            schema=SERVICE_UPDATE_PORTAINER_SCHEMA,
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

    async def handle_dismiss_trouble_item(call: ServiceCall) -> None:
        key = call.data["dismiss_key"].strip()
        if ":" not in key:
            raise ValueError(f"'{key}' is not a trouble item dismiss_key")
        _LOGGER.info("%s.dismiss_trouble_item: dismissing %s", DOMAIN, key)
        await dismissals.async_dismiss(key)
        trouble_coordinator = (
            hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("coordinators", {}).get("trouble")
        )
        if trouble_coordinator is not None:
            await trouble_coordinator.async_request_refresh()

    if not hass.services.has_service(DOMAIN, SERVICE_DISMISS_TROUBLE_ITEM):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DISMISS_TROUBLE_ITEM,
            handle_dismiss_trouble_item,
            schema=SERVICE_DISMISS_TROUBLE_ITEM_SCHEMA,
        )

    await hass.async_add_executor_job(_install_blueprints, hass)

    webapp_url = entry.data[CONF_WEBAPP_URL]

    actions_url = f"/{PANEL_PATH}"
    hass.data[DOMAIN][entry.entry_id]["actions_url"] = actions_url

    # Entries created before CONF_ADMIN_ONLY existed don't carry the key; they
    # get the default (administrators only) until reconfigured.
    # The sidecar's sign-in token rides in the panel URL (see sidecar_auth.py);
    # nothing here logs that URL.
    _register_panel(
        hass,
        panel_url(webapp_url, entry.data),
        bool(entry.data.get(CONF_ADMIN_ONLY, DEFAULT_ADMIN_ONLY)),
    )

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
            hass.services.async_remove(DOMAIN, SERVICE_DISMISS_TROUBLE_ITEM)
            hass.services.async_remove(DOMAIN, SERVICE_UPDATE_PORTAINER)
    return unload_ok
