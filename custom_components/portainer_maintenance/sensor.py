"""Tracking sensors for Portainer Maintenance.

Native replacements for the trigger-based template sensors that used to
live in templates.yaml, plus a read-only sensor.portainer_actions_url.
These moved here specifically because trigger-based template sensors with
a shared `variables:` block have no Helpers UI editor at all -- as native
integration entities, that constraint disappears entirely.

Each list sensor ports its original Jinja logic into plain Python against
the device/entity registries directly (the same registries `device_attr()`,
`config_entry_attr()`, `device_id()` etc. read from under the hood in
templates) rather than executor-offloaded work, since none of this touches
disk or the network -- registry/state reads are fine directly on the event
loop, same as template rendering itself.

Entity_ids are pinned explicitly (self.entity_id set before add) so the
merged automation blueprint and the webapp's REST calls don't need to
change across releases that only add sensors.
"""
from __future__ import annotations

import json
import logging
import asyncio
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiohttp

import homeassistant.util.dt as dt_util
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import aiohttp_client, device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import (
    API_LEVEL,
    DOMAIN,
    RUNNING_VERSION,
    SENSOR_ACTIONS_URL,
    SENSOR_CLEANUP,
    SENSOR_STALE_DEVICES,
    SENSOR_TROUBLE,
    SENSOR_UPDATES_PENDING,
)
from .dismissals import DismissalStore

_LOGGER = logging.getLogger(__name__)

# How long a change in availability has to stand before anything is reported
# from it. Entity `last_changed` resets on a Home Assistant restart (every
# restored entity is written afresh), so this doubles as the quiet period
# after startup. Used by the Trouble items and by Stale Devices.
SETTLE_SECONDS = 120

# Cleanup: how long after a change to one of a host's source sensors the
# Cleanup numbers are re-read (a burst of changes shares one timer, so the
# three sensors core updates together are read together), and how often they
# are re-read regardless (the backstop that turns a host which is still not
# ready into "unavailable").
CLEANUP_DEBOUNCE_SECONDS = 5
CLEANUP_BACKSTOP_SECONDS = 300
CLEANUP_READY = "ready"
CLEANUP_COMPUTING = "computing"
CLEANUP_UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# Shared registry helpers -- Python equivalents of the Jinja template
# functions the original templates.yaml sensors used.
# ---------------------------------------------------------------------------

def _portainer_entity_ids(entity_reg: er.EntityRegistry) -> list[str]:
    """Equivalent of the Jinja config_entry_id()/config_entry_attr() scan."""
    return [
        entry.entity_id
        for entry in entity_reg.entities.values()
        if entry.platform == "portainer"
    ]


def _walk_to_root(device_reg: dr.DeviceRegistry, device_id: str | None, max_hops: int = 4) -> str | None:
    """Walk via_device_id up to the root Endpoint device. Same bounded-loop
    trick used everywhere else in this design that needs the actual host."""
    current = device_id
    for _ in range(max_hops):
        if current is None:
            break
        device = device_reg.async_get(current)
        if device is None or not device.via_device_id:
            break
        current = device.via_device_id
    return current


def _device_name(device_reg: dr.DeviceRegistry, device_id: str | None) -> str | None:
    if device_id is None:
        return None
    device = device_reg.async_get(device_id)
    if device is None:
        return None
    return device.name_by_user or device.name


def _stack_info(
    device_reg: dr.DeviceRegistry, entity_reg: er.EntityRegistry, container_device_id: str | None
) -> tuple[str | None, str | None]:
    """(stack_name, stack_switch_entity_id) for a container device, or
    (None, None) if it isn't part of a stack.

    The device hierarchy is Endpoint -> Stack -> Container: a container's
    immediate parent (via_device_id) is its stack. But a *standalone*
    container (deployed outside Compose) is parented directly to the
    Endpoint instead, with no Stack device in between -- so the immediate
    parent alone doesn't tell us which case we're in. The distinguishing
    check: a real Stack device has its own via_device_id pointing further
    up to the Endpoint, while the Endpoint itself has none. If the
    immediate parent has no further parent, it IS the Endpoint, and this
    container has no stack.

    Used by the updates-pending sensor (dashboard tree grouping), the
    trouble sensor (stack-restart remediation target), and __init__.py's
    perform_update (to find the switch.* entity to restart on the known
    network_mode:service:X daemon-conflict bug)."""
    if container_device_id is None:
        return None, None
    container_device = device_reg.async_get(container_device_id)
    if container_device is None or container_device.via_device_id is None:
        return None, None

    parent = device_reg.async_get(container_device.via_device_id)
    if parent is None or parent.via_device_id is None:
        # Parent has no parent of its own -> parent IS the root Endpoint,
        # so this container is standalone, not part of a stack.
        return None, None

    stack_name = parent.name_by_user or parent.name
    switch_entity_id = None
    for entity in er.async_entries_for_device(entity_reg, container_device.via_device_id):
        if entity.entity_id.startswith("switch."):
            switch_entity_id = entity.entity_id
            break
    return stack_name, switch_entity_id


def _stack_device_id(device_reg: dr.DeviceRegistry, container_device_id: str | None) -> str | None:
    """The container's owning Stack device_id, or None if standalone. Thin
    counterpart to _stack_info for callers that need the device_id itself
    (tree grouping) rather than its name/switch entity."""
    if container_device_id is None:
        return None
    container_device = device_reg.async_get(container_device_id)
    if container_device is None or container_device.via_device_id is None:
        return None
    parent = device_reg.async_get(container_device.via_device_id)
    if parent is None or parent.via_device_id is None:
        return None
    return container_device.via_device_id


# Matches both "sensor.<name>_image" and an entity-registry-disambiguated
# duplicate like "sensor.<name>_image_2".
_IMAGE_ENTITY_SUFFIX_RE = re.compile(r"_image(_\d+)?$")
_STATE_ENTITY_SUFFIX_RE = re.compile(r"_state(_\d+)?$")


def _container_image_entity_id(
    hass: HomeAssistant, entity_reg: er.EntityRegistry, container_device_id: str | None
) -> str | None:
    """The sensor.<name>_image entity on a container's own device -- core's
    portainer integration creates one per container. Shared by the
    changelog-link lookup below, __init__.py's handle_perform_update
    recreate-outcome check, and this file's own stuck-container scan for
    the Trouble sensor -- all three need "what image reference is this
    container on right now," just for different reasons.

    HA entity IDs are unique GLOBALLY, not per device -- running the same-
    named service on more than one host means both containers' image
    sensors want the same object_id, and the registry auto-suffixes the
    second one (`..._image_2`). Matches either suffix shape so whichever
    host lost that naming race is still found."""
    if container_device_id is None:
        return None
    candidates = [
        entity.entity_id
        for entity in er.async_entries_for_device(entity_reg, container_device_id)
        if entity.entity_id.startswith("sensor.") and _IMAGE_ENTITY_SUFFIX_RE.search(entity.entity_id)
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def _is_live(entity_id: str) -> bool:
        state = hass.states.get(entity_id)
        return state is not None and state.state not in (None, "unavailable", "unknown")

    live_candidates = [c for c in candidates if _is_live(c)]
    pool = live_candidates or candidates  # all unavailable is still a pool, not nothing

    for entity_id in pool:
        state = hass.states.get(entity_id)
        if state and "/" in state.state:
            return entity_id
    return pool[0]


def _container_state_entity_id(entity_reg: er.EntityRegistry, container_device_id: str | None) -> str | None:
    """The sensor.<name>_state entity on a container's own device -- core's
    diagnostic ENUM sensor reporting the raw Docker container state
    (running/exited/dead/paused/restarting/created/removing). Same
    dual-suffix matching as _container_image_entity_id, for the same
    reason (global entity_id uniqueness across hosts)."""
    if container_device_id is None:
        return None
    candidates = [
        entity.entity_id
        for entity in er.async_entries_for_device(entity_reg, container_device_id)
        if entity.entity_id.startswith("sensor.") and _STATE_ENTITY_SUFFIX_RE.search(entity.entity_id)
    ]
    return candidates[0] if candidates else None


def _container_is_running(hass: HomeAssistant, entity_reg: er.EntityRegistry, container_device_id: str | None) -> bool:
    entity_id = _container_state_entity_id(entity_reg, container_device_id)
    if entity_id is None:
        return False
    state = hass.states.get(entity_id)
    return state is not None and state.state == "running"


# A container's image reference degrading to a bare content digest --
# 'sha256:<64 hex chars>' or just the 64 hex chars alone, no repo path, no
# human tag -- confirmed in production (see __init__.py's
# handle_perform_update) as the actual, observable symptom of the known
# network_mode:service:X daemon-conflict bug: the pull+recreate completes,
# but the container's image field doesn't reconcile to the new tag until
# the owning stack is restarted.
_BARE_DIGEST_RE = re.compile(r"^(sha256:)?[0-9a-f]{64}$", re.IGNORECASE)


def _looks_like_bare_digest(image_ref: str | None) -> bool:
    if not image_ref:
        return False
    return bool(_BARE_DIGEST_RE.match(image_ref.strip()))


def _find_stuck_containers(
    hass: HomeAssistant, entity_reg: er.EntityRegistry, device_reg: dr.DeviceRegistry
) -> list[dict]:
    """Every container currently showing the network_mode:service:X
    daemon-conflict symptom: its own image entity has degraded to a bare
    digest, AND the container is actually running (rules out a container
    that's merely mid-recreate or stopped for an unrelated reason, which
    could otherwise transiently read oddly here).

    Deliberately stateless -- both conditions are re-derived fresh from
    live entity state on every call, nothing cached or remembered between
    polls. That means it needs no persisted flag to survive an HA/
    integration restart (the very next poll after restart sees the same
    live state and reaches the same answer), and it clears itself the
    moment the image entity reflects a normal tag again -- no separate
    "auto-clear" logic, no dismiss control, just the same check re-run.

    Used by PortainerTroubleCoordinator (to report the item) and
    PortainerUpdatesCoordinator (to badge the owning stack's row) -- one
    shared detection, not two that could drift."""
    portainer_ids = _portainer_entity_ids(entity_reg)
    image_entities = [
        e for e in portainer_ids if e.startswith("sensor.") and _IMAGE_ENTITY_SUFFIX_RE.search(e)
    ]
    stuck: list[dict] = []
    for image_entity_id in image_entities:
        state = hass.states.get(image_entity_id)
        if state is None or not _looks_like_bare_digest(state.state):
            continue
        reg_entry = entity_reg.async_get(image_entity_id)
        device_id = reg_entry.device_id if reg_entry else None
        if device_id is None or not _container_is_running(hass, entity_reg, device_id):
            continue

        host_id = _walk_to_root(device_reg, device_id)
        host = _device_name(device_reg, host_id) or "unknown host"
        container_name = _device_name(device_reg, device_id) or device_id
        stack_name, switch_entity_id = _stack_info(device_reg, entity_reg, device_id)
        stack_dev_id = _stack_device_id(device_reg, device_id)

        stuck.append(
            {
                "device_id": device_id,
                "container_name": container_name,
                "host": host,
                "host_device_id": host_id,
                "stack_name": stack_name,
                "stack_device_id": stack_dev_id,
                "switch_entity_id": switch_entity_id,
            }
        )
    return stuck


def _stacks_with_open_trouble(hass: HomeAssistant, entity_reg: er.EntityRegistry, device_reg: dr.DeviceRegistry) -> set[str]:
    """Stack device_ids that currently have a stuck container under them --
    used by PortainerUpdatesCoordinator to badge that stack's row, so
    "other pending updates for this stack" don't get installed blind while
    a restart is still owed. Calls the same _find_stuck_containers as the
    Trouble sensor itself rather than a separate check."""
    return {
        item["stack_device_id"]
        for item in _find_stuck_containers(hass, entity_reg, device_reg)
        if item["stack_device_id"]
    }


# ---------------------------------------------------------------------------
# Portainer's own containers: the server and the agent. Portainer performs
# every container recreate on a host through its server (or, for a remote
# host, through that host's agent), so recreating one of those containers
# from inside Portainer stops the process doing the work before the
# replacement is started: it was observed to leave Portainer stopped. Their
# updates therefore can't be installed from the dashboard; they are reported
# as Needs Remediation items instead (see PortainerTroubleCoordinator) and
# perform_update refuses them.
# ---------------------------------------------------------------------------
PORTAINER_SERVER_IMAGE_REPOS = frozenset({"portainer/portainer-ce", "portainer/portainer-ee"})
PORTAINER_AGENT_IMAGE_REPOS = frozenset({"portainer/agent"})
# Only used when the container's image sensor can't say what it runs.
_PORTAINER_FALLBACK_NAMES = {
    "portainer": "server",
    "portainer_agent": "agent",
    "portainer-agent": "agent",
}


def _portainer_component(
    hass: HomeAssistant, entity_reg: er.EntityRegistry, container_device_id: str | None
) -> str | None:
    """"server" or "agent" when the container is one of Portainer's own,
    otherwise None. Matched on the container's image repo; falls back to the
    container's name only when its image sensor can't say (missing, unknown,
    unavailable, or a bare digest)."""
    if container_device_id is None:
        return None
    image_entity_id = _container_image_entity_id(hass, entity_reg, container_device_id)
    state = hass.states.get(image_entity_id) if image_entity_id else None
    image_ref = state.state if state else None
    if image_ref and image_ref not in ("unknown", "unavailable") and not _looks_like_bare_digest(image_ref):
        _host, repo = _split_image_repo(image_ref)
        repo = repo.lower() if repo else None
        if repo in PORTAINER_SERVER_IMAGE_REPOS:
            return "server"
        if repo in PORTAINER_AGENT_IMAGE_REPOS:
            return "agent"
        return None
    device_reg = dr.async_get(hass)
    name = _device_name(device_reg, container_device_id)
    return _PORTAINER_FALLBACK_NAMES.get(name.strip().lower()) if name else None


def _portainer_subject(host: str, component: str) -> str:
    return f"Portainer agent on {host}" if component == "agent" else f"Portainer on {host}"


_PORTAINER_RESTART_NOTE = (
    "Be aware that updating Portainer will make it briefly unavailable, so other update or "
    "cleanup actions will not be possible until Portainer restarts."
)
_PORTAINER_MANUAL_STEPS = (
    "Update must be performed on the host instead -- pull the new image and recreate the "
    "container from the compose file it was deployed with (`docker compose pull && "
    "docker compose up -d` in that file's directory)."
)


def _portainer_self_update_detail(
    host: str, component: str = "server", mode: str = "manual", reason: str | None = None,
    started: str | None = None,
) -> str:
    """The More Info text for a Portainer self-update row. mode is
    "possible" (Update now works), "manual" (the dashboard can't do it),
    "failed" (it was tried and didn't work; `reason` says what was seen) or
    "running" (in progress since `started`). The agent only changes the
    subject."""
    subject = _portainer_subject(host, component)
    if mode == "possible":
        return (
            f"{subject} has a pending update. Update now starts Portainer's updater on that "
            f"host. {_PORTAINER_RESTART_NOTE}"
        )
    if mode == "running":
        return (
            f"{subject} is being updated by Portainer's updater (started {started}). "
            f"Portainer is briefly unavailable while it restarts, so other update or cleanup "
            "actions will not be possible until it is back. This row shows how it is going."
        )
    if mode == "failed":
        return (
            f"Updating {subject} from here did not work: {reason} "
            f"{_PORTAINER_MANUAL_STEPS} {_PORTAINER_RESTART_NOTE}"
        )
    return (
        f"{subject} has a pending update, but is in a state that doesn't allow programmatic "
        f"updates. {_PORTAINER_MANUAL_STEPS} {_PORTAINER_RESTART_NOTE}"
    )


def _dismiss_key(kind: str, identity: str) -> str:
    return f"{kind}:{identity}"


def _self_update_item(
    hass: HomeAssistant,
    entity_reg: er.EntityRegistry,
    device_reg: dr.DeviceRegistry,
    entity_id: str,
    component: str | None = None,
) -> dict | None:
    """The description of a Portainer server/agent update entity, or None when
    the entity isn't one. `component` can be passed when the caller already
    knows it (the image sensor that normally tells can be unavailable while
    Portainer restarts)."""
    state = hass.states.get(entity_id)
    reg_entry = entity_reg.async_get(entity_id)
    device_id = reg_entry.device_id if reg_entry else None
    if component is None:
        component = _portainer_component(hass, entity_reg, device_id)
    if component is None:
        return None
    root_id = _walk_to_root(device_reg, device_id)
    host = _device_name(device_reg, root_id) or "unknown host"
    container_name = _device_name(device_reg, device_id) or (
        state.attributes.get("friendly_name", entity_id) if state else entity_id
    )
    return {
        "entity": entity_id,
        "device_id": device_id,
        "host": host,
        "host_device_id": root_id,
        "container_name": container_name,
        "component": component,
        "stack_name": _stack_info(device_reg, entity_reg, device_id)[0],
        "stack_device_id": _stack_device_id(device_reg, device_id),
    }


def _find_portainer_self_updates(
    hass: HomeAssistant, entity_reg: er.EntityRegistry, device_reg: dr.DeviceRegistry
) -> list[dict]:
    """Pending update.* entities that belong to Portainer's own containers
    (server or agent)."""
    found: list[dict] = []
    for entity_id in _portainer_entity_ids(entity_reg):
        if not entity_id.startswith("update."):
            continue
        state = hass.states.get(entity_id)
        if state is None or state.state != "on":
            continue
        item = _self_update_item(hass, entity_reg, device_reg, entity_id)
        if item is not None:
            found.append(item)
    return found


# ---------------------------------------------------------------------------
# Updating Portainer's own server/agent from the dashboard, and following the
# update. The update itself is started by __init__.py's update_portainer
# service (a short-lived portainer-updater helper container). Portainer is
# unavailable while it runs, so the result is judged here, from what core's
# Portainer integration is seen to report, and never guessed.
# ---------------------------------------------------------------------------
CORE_PORTAINER_DOMAIN = "portainer"
# Reading Portainer's version (to know which updater image fits) must not
# hold up the Trouble refresh when Portainer is slow or down.
SELF_UPDATE_VERSION_TIMEOUT_SECONDS = 5
# Trouble refresh interval while an update is being followed.
SELF_UPDATE_FAST_INTERVAL = timedelta(seconds=10)
TROUBLE_INTERVAL = timedelta(minutes=1)
# How long to wait for a verdict, from the helper's start. Without
# --health-check the updater's own checks last well under a minute; with it the
# updater retries "/portainer --health-check" for up to about 2.75 hours, and
# then spends up to 5 minutes rolling the database back.
SELF_UPDATE_CEILING_SECONDS = 5 * 60
SELF_UPDATE_HEALTH_CHECK_CEILING_SECONDS = 3 * 3600
# After the new container is seen, how long to wait for core's update entity
# to turn off before the row stops claiming to be confirming.
SELF_UPDATE_CONFIRM_SECONDS = 15 * 60
# Consecutive new snapshots of core's data that must all show "updater gone,
# old container still current" before that is called a failure.
SELF_UPDATE_GONE_SNAPSHOTS = 2


def _core_container_view(
    hass: HomeAssistant, device_reg: dr.DeviceRegistry, container_device_id: str | None
) -> tuple[Any, Any, Any] | None:
    """(core coordinator, its endpoint data, its data for the container) for a
    container device, or None when core doesn't currently list it."""
    device = device_reg.async_get(container_device_id) if container_device_id else None
    if device is None:
        return None
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
                    return coordinator, data, container_data
    return None


async def _self_update_possible(
    hass: HomeAssistant, device_reg: dr.DeviceRegistry, entity_reg: er.EntityRegistry, item: dict
) -> bool:
    """Whether update_portainer can work for this item right now: the
    container has a usable image tag, and the running Portainer version
    can be read as a plain x.y.z (one system/status request, cut off after
    SELF_UPDATE_VERSION_TIMEOUT_SECONDS) to pick the matching updater image.
    The agent is judged by the server version core talks to."""
    from . import _PLAIN_VERSION_RE, _updater_target_image  # circular at import time

    image_entity_id = _container_image_entity_id(hass, entity_reg, item["device_id"])
    image_state = hass.states.get(image_entity_id) if image_entity_id else None
    try:
        _updater_target_image(
            image_state.state if image_state else None, item["container_name"], item["host"]
        )
    except Exception:  # noqa: BLE001 - HomeAssistantError: no usable tag
        return False
    view = _core_container_view(hass, device_reg, item["device_id"])
    if view is None:
        return False
    try:
        async with asyncio.timeout(SELF_UPDATE_VERSION_TIMEOUT_SECONDS):
            status = await view[0].portainer.portainer_system_status()
    except Exception:  # noqa: BLE001 - any failure, timeout included, means "can't tell"
        return False
    return bool(_PLAIN_VERSION_RE.match((getattr(status, "version", None) or "").strip()))


@dataclass
class _UpdateRecord:
    """One update_portainer run being followed."""

    entity: str
    device_id: str
    component: str
    container_id: str  # Portainer's container id when the helper started
    helper_id: str
    helper_name: str
    health_check: bool
    started_at: datetime
    started_mono: float
    start_data: Any = None  # core's coordinator data when the helper started
    start_time: Any = None
    last_data: Any = None
    last_time: Any = None
    fresh: int = 0  # core refreshes seen since the start
    helper_seen: bool = False
    gone_streak: int = 0
    replaced_mono: float | None = None
    state: str = "updating"  # updating | confirming | failed
    progress: str = ""
    reason: str = ""


def _fmt_duration(seconds: float) -> str:
    return f"{int(seconds // 3600)} hours" if seconds >= 3600 else f"{int(seconds // 60)} minutes"


# ---------------------------------------------------------------------------
# Endpoint helpers -- shared by the broadened Trouble sensor (endpoint
# dropped connection) and the new Cleanup sensor (per-endpoint counts).
# ---------------------------------------------------------------------------

def _discover_endpoint_devices(entity_reg: er.EntityRegistry, device_reg: dr.DeviceRegistry) -> set[str]:
    """Every root Endpoint device_id this HA instance knows about, found by
    walking every portainer-platform entity up to its root -- the same
    dynamic discovery __init__.py's prune_images already uses, so a newly
    added host is picked up automatically here too."""
    roots: set[str] = set()
    for entity_id in _portainer_entity_ids(entity_reg):
        reg_entry = entity_reg.async_get(entity_id)
        device_id = reg_entry.device_id if reg_entry else None
        root_id = _walk_to_root(device_reg, device_id)
        if root_id:
            roots.add(root_id)
    return roots


def _endpoint_unavailable_since(
    hass: HomeAssistant, entity_reg: er.EntityRegistry, endpoint_device_id: str
) -> datetime | None:
    """None if the endpoint's OWN entities (not a child container's) are
    available; otherwise the earliest last_changed among them, i.e. how
    long it's been down. Core's portainer integration drops an endpoint
    from its coordinator data the moment it can't reach it -- every entity
    on that device (and everything under it) goes `unavailable` with no
    dedicated "endpoint unreachable" signal of its own, which is exactly
    the gap this closes."""
    own_entities = [e.entity_id for e in er.async_entries_for_device(entity_reg, endpoint_device_id)]
    if not own_entities:
        return None
    states = [hass.states.get(e) for e in own_entities]
    if any(s is None for s in states):
        return None
    if not all(s.state == "unavailable" for s in states):
        return None
    return min(s.last_changed for s in states)


def _device_entity_by_suffix(entity_reg: er.EntityRegistry, device_id: str, domain_prefix: str, suffixes: tuple[str, ...]) -> str | None:
    """First entity on a device whose entity_id starts with domain_prefix
    (e.g. "sensor." or "button.") and ends with one of the given suffixes.
    Only the fallback for _device_entity_by_key below: an entity_id is built
    from the entity's display name ("Volume disk usage total size" gives
    `_volume_disk_usage_total_size`, "Prune unused volumes" gives
    `_prune_unused_volumes`), not from core's internal key, and a rename
    changes it."""
    for entity in er.async_entries_for_device(entity_reg, device_id):
        if not entity.entity_id.startswith(domain_prefix):
            continue
        for suffix in suffixes:
            if entity.entity_id.endswith(suffix):
                return entity.entity_id
    return None


def _device_entity_by_key(
    entity_reg: er.EntityRegistry,
    device_id: str,
    domain_prefix: str,
    translation_keys: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> str | None:
    """The entity on a device that core's Portainer integration created with
    one of these translation keys (the `translation_key` in its entity
    descriptions, kept in the entity registry), else the first whose entity_id
    ends in one of the suffixes. The key is what the entity IS, so it keeps
    working when an entity is renamed or core rewords its display name; the
    suffixes are the fallback for a registry entry without one. Matching
    entity_ids alone is why volume usage and the volume prune button were
    never found: the dashboard looked for `_volume_disk_usage_total` and
    `_volumes_prune`, but the entity_ids end `_volume_disk_usage_total_size`
    and `_prune_unused_volumes`."""
    for entity in er.async_entries_for_device(entity_reg, device_id):
        if not entity.entity_id.startswith(domain_prefix):
            continue
        if getattr(entity, "translation_key", None) in translation_keys:
            return entity.entity_id
    return _device_entity_by_suffix(entity_reg, device_id, domain_prefix, suffixes)


def _endpoint_images_count_entity(entity_reg: er.EntityRegistry, device_id: str) -> str | None:
    return _device_entity_by_key(entity_reg, device_id, "sensor.", ("images_count",), ("_images_count", "_image_count"))


def _endpoint_containers_count_entity(entity_reg: er.EntityRegistry, device_id: str) -> str | None:
    return _device_entity_by_key(entity_reg, device_id, "sensor.", ("containers_count",), ("_containers_count", "_container_count"))


def _endpoint_reclaimable_entity(entity_reg: er.EntityRegistry, device_id: str) -> str | None:
    return _device_entity_by_key(entity_reg, device_id, "sensor.", ("image_disk_usage_reclaimable",), ("_image_disk_usage_reclaimable",))


def _endpoint_volume_usage_entity(entity_reg: er.EntityRegistry, device_id: str) -> str | None:
    return _device_entity_by_key(
        entity_reg,
        device_id,
        "sensor.",
        ("volume_disk_usage_total_size",),
        ("_volume_disk_usage_total_size", "_volume_disk_usage_total"),
    )


def _endpoint_volumes_prune_button(entity_reg: er.EntityRegistry, device_id: str) -> str | None:
    return _device_entity_by_key(entity_reg, device_id, "button.", ("volumes_prune",), ("_prune_unused_volumes", "_volumes_prune"))


def _numeric_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """A sensor's numeric value, or None for missing/unknown/unavailable.
    Used for the image and container counts, where None means "can't tell"
    (see _size_state for the disk-usage sensors, where Unknown means 0)."""
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (None, "unknown", "unavailable"):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _size_state(hass: HomeAssistant, entity_id: str | None) -> tuple[float | None, bool]:
    """(value, unavailable) for one of core's disk-usage sensors.

    Core reports Unknown for these when there is nothing to report: the image
    "reclaimable" figure when no image is unused, the volume total on a host
    with no volumes. That is 0, so Unknown gives 0.0. Unavailable means the
    state is not known (Portainer unreachable, the entity not loaded), which
    is not 0: it gives (None, True), so a caller can refuse to offer an action
    on it. No such entity (disabled or never created) gives (None, False), and
    the caller decides what that means. A state that isn't a number gives
    (None, False)."""
    if entity_id is None:
        return None, False
    state = hass.states.get(entity_id)
    if state is None:
        return None, False
    if state.state == "unavailable":
        return None, True
    if state.state in (None, "unknown"):
        return 0.0, False
    try:
        return float(state.state), False
    except (TypeError, ValueError):
        return None, False


# ---------------------------------------------------------------------------
# Changelog links -- a hand-curated override table, backed by automatic
# discovery for anything not in it.
# ---------------------------------------------------------------------------
_KNOWN_CHANGELOG_URLS: dict[str, str] = {
    "homeassistant/home-assistant": "https://github.com/home-assistant/core/releases",
    "qmcgaw/gluetun": "https://github.com/qdm12/gluetun/releases",
    "portainer/portainer-ce": "https://github.com/portainer/portainer/releases",
}

CHANGELOG_OVERRIDES_FILENAME = "portainer_maintenance_changelog_overrides.json"


def _load_changelog_overrides_sync(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        _LOGGER.debug("%s: no changelog overrides file at %s", DOMAIN, path)
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        _LOGGER.warning("%s: couldn't read changelog overrides file %s: %s", DOMAIN, path, exc)
        return {}

    if not isinstance(data, dict):
        _LOGGER.warning(
            "%s: changelog overrides file %s must be a JSON object of "
            '"image/repo": "url" entries -- ignoring the whole file',
            DOMAIN, path,
        )
        return {}

    overrides: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and key and isinstance(value, str) and value:
            overrides[key] = value
        else:
            _LOGGER.warning(
                "%s: ignoring invalid entry in changelog overrides file (%r -> %r) -- "
                "both the image/repo key and the URL value must be non-empty strings",
                DOMAIN, key, value,
            )
    return overrides


async def _load_changelog_overrides(hass: HomeAssistant) -> dict[str, str]:
    path = hass.config.path(CHANGELOG_OVERRIDES_FILENAME)
    return await hass.async_add_executor_job(_load_changelog_overrides_sync, path)


_GITHUB_NON_REPO_OWNERS = {
    "sponsors", "apps", "marketplace", "orgs", "settings", "about",
    "features", "pricing", "topics", "collections", "trending", "explore",
    "login", "join", "search",
}
_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+)"
)


def _split_image_repo(image_ref: str) -> tuple[str | None, str | None]:
    if not image_ref:
        return None, None
    ref = image_ref.split("@", 1)[0]  # drop a digest, if present

    parts = ref.split("/")
    host = None
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        host = parts[0]
        parts = parts[1:]  # drop the registry host segment
    ref = "/".join(parts)

    if ":" in ref.rsplit("/", 1)[-1]:
        ref = ref.rsplit(":", 1)[0]

    return host, (ref or None)


def _guess_repo_url_from_path(repo: str) -> str | None:
    parts = repo.split("/")
    if len(parts) < 2:
        return None
    return f"https://github.com/{parts[0]}/{parts[1]}"


def _first_github_repo_url(text: str) -> str | None:
    if not text:
        return None
    for match in _GITHUB_URL_RE.finditer(text):
        owner, name = match.group(1), match.group(2)
        if owner.lower() in _GITHUB_NON_REPO_OWNERS:
            continue
        name = name.rstrip(").,]>\"'")
        if not name:
            continue
        return f"https://github.com/{owner}/{name}"
    return None


async def _fetch_dockerhub_github_url(hass: HomeAssistant, repo: str) -> str | None:
    parts = repo.split("/")
    namespace, name = ("library", parts[0]) if len(parts) == 1 else (parts[0], parts[1])
    session = aiohttp_client.async_get_clientsession(hass)
    api_url = f"https://hub.docker.com/v2/repositories/{namespace}/{name}/"
    try:
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    text = (data.get("full_description") or data.get("description") or "") if isinstance(data, dict) else ""
    return _first_github_repo_url(text)


async def _verify_github_url(hass: HomeAssistant, url: str) -> bool:
    session = aiohttp_client.async_get_clientsession(hass)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=True) as resp:
            return resp.status == 200
    except (aiohttp.ClientError, TimeoutError):
        return False


# (1.3.2) "owner/repo", pulled back out of a resolved github.com
# changelog URL -- the sidecar uses this to fetch structured release notes
# via GitHub's own API and render them in-app, instead of linking out to
# this URL, which was never usable from Home Assistant's companion app
# (its embedded webview won't open a new window, and a same-window
# fallback can only clobber this app's own panel -- see the sidecar's
# README for the two failed attempts at working around that). A
# hand-curated override that isn't a github.com URL (allowed, though every
# current entry is one) just yields None here, and the sidecar falls back
# to a plain external link for it, same as before this existed.
_CHANGELOG_GITHUB_REPO_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+?)(?:/releases.*)?$")


def _github_repo_slug(changelog_url: str | None) -> str | None:
    if not changelog_url:
        return None
    match = _CHANGELOG_GITHUB_REPO_RE.match(changelog_url)
    return match.group(1) if match else None


_REGISTRY_MANIFEST_ENDPOINTS: dict[str | None, tuple[str, str, str]] = {
    None: ("https://registry-1.docker.io", "https://auth.docker.io/token", "registry.docker.io"),
    "docker.io": ("https://registry-1.docker.io", "https://auth.docker.io/token", "registry.docker.io"),
    "index.docker.io": ("https://registry-1.docker.io", "https://auth.docker.io/token", "registry.docker.io"),
    "registry-1.docker.io": ("https://registry-1.docker.io", "https://auth.docker.io/token", "registry.docker.io"),
    "ghcr.io": ("https://ghcr.io", "https://ghcr.io/token", "ghcr.io"),
}

_MANIFEST_LIST_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}
_MANIFEST_ACCEPT_HEADER = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)


def _extract_reference(image_ref: str) -> str:
    if not image_ref:
        return "latest"
    if "@" in image_ref:
        return image_ref.split("@", 1)[1]
    last_segment = image_ref.rsplit("/", 1)[-1]
    if ":" in last_segment:
        return last_segment.rsplit(":", 1)[-1]
    return "latest"


async def _registry_anon_token(hass: HomeAssistant, auth_url: str, service: str, repo: str) -> str | None:
    session = aiohttp_client.async_get_clientsession(hass)
    params = {"service": service, "scope": f"repository:{repo}:pull"}
    try:
        async with session.get(auth_url, params=params, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    return data.get("token") or data.get("access_token") if isinstance(data, dict) else None


async def _fetch_registry_json(
    hass: HomeAssistant, url: str, token: str | None, accept: str | None = None
) -> dict | None:
    session = aiohttp_client.async_get_clientsession(hass)
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


async def _fetch_oci_source_label(hass: HomeAssistant, host: str | None, repo: str, reference: str) -> str | None:
    registry_info = _REGISTRY_MANIFEST_ENDPOINTS.get(host)
    if registry_info is None:
        return None
    registry_base, auth_url, service = registry_info

    token = await _registry_anon_token(hass, auth_url, service, repo)
    manifest = await _fetch_registry_json(
        hass, f"{registry_base}/v2/{repo}/manifests/{reference}", token, _MANIFEST_ACCEPT_HEADER
    )
    if manifest is None and token is not None:
        manifest = await _fetch_registry_json(
            hass, f"{registry_base}/v2/{repo}/manifests/{reference}", None, _MANIFEST_ACCEPT_HEADER
        )
    if not isinstance(manifest, dict):
        return None

    if manifest.get("mediaType") in _MANIFEST_LIST_MEDIA_TYPES or (
        "manifests" in manifest and "config" not in manifest
    ):
        entries = manifest.get("manifests") or []
        if not entries or not isinstance(entries[0], dict):
            return None
        child_digest = entries[0].get("digest")
        if not child_digest:
            return None
        manifest = await _fetch_registry_json(
            hass, f"{registry_base}/v2/{repo}/manifests/{child_digest}", token, _MANIFEST_ACCEPT_HEADER
        )
        if not isinstance(manifest, dict):
            return None

    config_digest = (manifest.get("config") or {}).get("digest") if isinstance(manifest.get("config"), dict) else None
    if not config_digest:
        return None
    config = await _fetch_registry_json(hass, f"{registry_base}/v2/{repo}/blobs/{config_digest}", token)
    if not isinstance(config, dict):
        return None

    labels = (config.get("config") or {}).get("Labels") if isinstance(config.get("config"), dict) else None
    if not isinstance(labels, dict):
        return None
    source = labels.get("org.opencontainers.image.source")
    if isinstance(source, str) and source.startswith("https://github.com/"):
        return source.rstrip("/")
    return None


# ---------------------------------------------------------------------------
# Coordinators -- one per sensor, matching the original recompute cadence.
# ---------------------------------------------------------------------------

class PortainerUpdatesCoordinator(DataUpdateCoordinator[list[dict]]):
    """Ports the original 5-minute update_items template."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=SENSOR_UPDATES_PENDING, update_interval=timedelta(minutes=5))
        self._changelog_cache: dict[str, str | None] = {}

    async def _resolve_changelog_url(
        self, entity_reg: er.EntityRegistry, container_device_id: str | None
    ) -> str | None:
        if container_device_id is None:
            return None
        image_entity_id = _container_image_entity_id(self.hass, entity_reg, container_device_id)
        if image_entity_id is None:
            return None
        state = self.hass.states.get(image_entity_id)
        image_ref = state.state if state else None
        if not image_ref:
            return None

        host, repo = _split_image_repo(image_ref)
        if not repo:
            return None

        overrides = await _load_changelog_overrides(self.hass)
        override = overrides.get(repo)
        if override:
            return override

        known = _KNOWN_CHANGELOG_URLS.get(repo)
        if known:
            return known

        if repo in self._changelog_cache:
            return self._changelog_cache[repo]

        discovered: str | None = None
        try:
            reference = _extract_reference(image_ref)
            label_source = await _fetch_oci_source_label(self.hass, host, repo, reference)
            if label_source:
                label_releases = f"{label_source}/releases"
                if await _verify_github_url(self.hass, label_releases):
                    discovered = label_releases

            if discovered is not None:
                pass
            elif host == "ghcr.io":
                guess = _guess_repo_url_from_path(repo)
                if guess:
                    guess_releases = f"{guess}/releases"
                    if await _verify_github_url(self.hass, guess_releases):
                        discovered = guess_releases
            elif host is None or host in (
                "docker.io", "index.docker.io", "registry-1.docker.io", "lscr.io",
            ):
                guess = _guess_repo_url_from_path(repo)
                if guess:
                    guess_releases = f"{guess}/releases"
                    if await _verify_github_url(self.hass, guess_releases):
                        discovered = guess_releases
                if discovered is None:
                    candidate = await _fetch_dockerhub_github_url(self.hass, repo)
                    if candidate:
                        candidate_releases = f"{candidate}/releases"
                        if await _verify_github_url(self.hass, candidate_releases):
                            discovered = candidate_releases
        except Exception:  # never let a discovery hiccup break a poll cycle
            _LOGGER.debug("%s: changelog auto-discovery failed for %s", DOMAIN, repo, exc_info=True)
            discovered = None

        self._changelog_cache[repo] = discovered
        if discovered:
            _LOGGER.info("%s: auto-discovered changelog URL for %s -> %s", DOMAIN, repo, discovered)
        return discovered

    async def _async_update_data(self) -> list[dict]:
        entity_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        found: list[dict] = []

        # Computed once per poll, not once per item -- see
        # _stacks_with_open_trouble's docstring. Cheap (registry/state
        # reads only), so no reason to cache it further.
        stuck_stacks = _stacks_with_open_trouble(self.hass, entity_reg, device_reg)

        for entity_id in _portainer_entity_ids(entity_reg):
            if not entity_id.startswith("update."):
                continue
            state = self.hass.states.get(entity_id)
            # An update is listed exactly while the core Portainer update
            # entity says "on". There is deliberately no local "recently
            # installed" hold: on HA 2026.10+ core re-checks a recreated
            # container's image straight away, so the entity goes "off" by
            # itself once the install has really worked, and stays "on" if
            # it has not -- which is the honest answer.
            if state is None or state.state != "on":
                continue

            reg_entry = entity_reg.async_get(entity_id)
            device_id = reg_entry.device_id if reg_entry else None
            # Portainer's own updates (server, agent) can't be installed from
            # here -- they are reported on the Trouble sensor instead (see
            # _find_portainer_self_updates).
            if _portainer_component(self.hass, entity_reg, device_id) is not None:
                continue
            root_id = _walk_to_root(device_reg, device_id)
            host = _device_name(device_reg, root_id) or "unknown host"
            container_name = _device_name(device_reg, device_id) or state.attributes.get(
                "friendly_name", entity_id
            )
            stack_name, stack_switch_entity_id = _stack_info(device_reg, entity_reg, device_id)
            stack_dev_id = _stack_device_id(device_reg, device_id)
            changelog_url = await self._resolve_changelog_url(entity_reg, device_id)

            found.append(
                {
                    "entity": entity_id,
                    "name": f"{container_name} ({host})",
                    "secondary_info": "Update available",
                    "host": host,
                    "host_device_id": root_id,
                    "stack_name": stack_name,
                    "stack_device_id": stack_dev_id,
                    "stack_switch_entity_id": stack_switch_entity_id,
                    "changelog_url": changelog_url,
                    # (1.3.2) See _github_repo_slug above.
                    "changelog_repo": _github_repo_slug(changelog_url),
                    # (1.3.0) True when this container's stack has an open
                    # "needs a restart" Trouble item -- the webapp badges
                    # the stack's row with this so a fresh install doesn't
                    # get triggered blind while a restart is still owed.
                    "stack_has_open_trouble": bool(stack_dev_id and stack_dev_id in stuck_stacks),
                }
            )

        return found


class PortainerTroubleCoordinator(DataUpdateCoordinator[list[dict]]):
    """Broadened (1.3.0) past individual containers to also cover:

      - an Endpoint that's dropped out of core's own coordinator data
        entirely (kind="endpoint") -- core gives no dedicated signal for
        this; every entity on the device just goes unavailable. Settled
        the same SETTLE_SECONDS as container issues, to avoid
        flapping on a brief poll hiccup. Carries the endpoint's own
        device_id so the webapp's Reload Endpoint button can call
        portainer_maintenance.reload_endpoint directly.

      - a container stuck on the known network_mode:service:X daemon-
        conflict bug (see _find_stuck_containers): kind="stack_restart_needed"
        when it's part of a real Portainer stack (carries switch_entity_id
        so the webapp's Restart Stack Now button can call the existing
        portainer_maintenance.restart_stack service directly), or
        kind="unstacked_recreate" when it isn't (no remediation possible
        from here -- info-only, with a fuller "detail" string for the
        webapp's More Info dialog). Deliberately NOT settled -- see
        _find_stuck_containers's docstring for why this is stateless and
        needs no settle window to avoid flapping.

      - A pending update for one of Portainer's own containers, the server
        or the agent (kind="portainer_self_update", with component="server"
        or "agent"): carries `update_now` (true when update_portainer can
        run: a usable image tag, and a Portainer version read from
        system/status within a few seconds), and a `detail` string for the
        More Info dialog. After update_portainer starts, the item is followed
        (see _observe_update): `update_state` is "updating" with the observed
        progress in `secondary_info`, or "failed" with the reason there and
        the manual steps in `detail`. The Trouble sensor refreshes every 10 s
        while an update is being followed.

    Items the user can't act on from the dashboard carry a `dismiss_key`
    (container_exited, container_unhealthy, unstacked_recreate,
    portainer_self_update). One that has been dismissed (see dismissals.py)
    is left out of the list, and so out of the sensor's count too.
    endpoint and stack_restart_needed items have real actions and are
    never dismissible.
    """

    def __init__(self, hass: HomeAssistant, dismissals: DismissalStore | None = None) -> None:
        super().__init__(hass, _LOGGER, name=SENSOR_TROUBLE, update_interval=TROUBLE_INTERVAL)
        self._dismissals = dismissals
        # update_portainer runs being followed, by update entity id. In
        # memory only: after a Home Assistant restart a row shows its plain
        # "Update available" state again.
        self.updates: dict[str, _UpdateRecord] = {}
        # Items the last update left out because they are dismissed, so
        # sensor.portainer_trouble can show what is currently hidden (its
        # `dismissed_items` attribute). A dismissed item whose condition has
        # since cleared is not in this list: it only lists what would
        # otherwise be showing right now.
        self.suppressed: list[dict] = []

    def start_update_tracking(
        self,
        *,
        entity: str,
        device_id: str,
        component: str,
        container_id: str,
        helper_id: str,
        helper_name: str,
        health_check: bool,
        core_coordinator: Any = None,
    ) -> None:
        """Called by update_portainer once the helper container is running:
        follow the update from here, and refresh quickly while doing so."""
        rec = _UpdateRecord(
            entity=entity,
            device_id=device_id,
            component=component,
            container_id=container_id,
            helper_id=helper_id,
            helper_name=helper_name,
            health_check=health_check,
            started_at=dt_util.utcnow(),
            started_mono=time.monotonic(),
            start_data=getattr(core_coordinator, "data", None),
            start_time=getattr(core_coordinator, "last_update_success_time", None),
        )
        rec.last_data, rec.last_time = rec.start_data, rec.start_time
        rec.progress = f"Updater started {dt_util.as_local(rec.started_at).strftime('%H:%M')}"
        self.updates[entity] = rec
        self.update_interval = SELF_UPDATE_FAST_INTERVAL

    def is_update_tracked(self, entity: str) -> bool:
        rec = self.updates.get(entity)
        return rec is not None and rec.state in ("updating", "confirming")

    def _fail_update(self, rec: _UpdateRecord, reason: str) -> None:
        rec.state, rec.reason = "failed", reason
        _LOGGER.warning(
            "%s: update of %s (updater %s) failed: %s", SENSOR_TROUBLE, rec.entity,
            rec.helper_name, reason,
        )

    def _observe_update(self, rec: _UpdateRecord, device_reg: dr.DeviceRegistry) -> bool:
        """Look at what core's Portainer integration reports now and move the
        record along. False when the record is finished and should be dropped.

        Only observations count: the new container id, the helper container
        gone from core's list, Portainer reachable or not, how much time has
        passed. A start with --health-check is not a success until the helper
        is gone, because the helper is what runs that check."""
        now = time.monotonic()
        entity_state = self.hass.states.get(rec.entity)
        ent = entity_state.state if entity_state is not None else None

        view = _core_container_view(self.hass, device_reg, rec.device_id)
        reachable = False
        container_now = None
        helper_present = False
        new_snapshot = False
        if view is not None:
            core, endpoint_data, container_data = view
            ok = bool(getattr(core, "last_update_success", False))
            reachable = ok and ent not in (None, "unavailable")
            container_now = container_data.container.id
            if ok:
                data_now = core.data
                time_now = getattr(core, "last_update_success_time", None)
                if data_now is not rec.last_data or time_now != rec.last_time:
                    rec.last_data, rec.last_time = data_now, time_now
                    if data_now is not rec.start_data or time_now != rec.start_time:
                        rec.fresh += 1
                        new_snapshot = True
            for name, other in endpoint_data.containers.items():
                other_id = getattr(getattr(other, "container", None), "id", None)
                if name == rec.helper_name or other_id == rec.helper_id:
                    helper_present = True
                    break
        if helper_present:
            rec.helper_seen = True
        helper_gone = (
            view is not None and reachable and not helper_present
            and (rec.helper_seen or rec.fresh >= 2)
        )
        replaced = reachable and (
            (container_now is not None and container_now != rec.container_id) or ent == "off"
        )

        if rec.state == "confirming":
            if ent == "off" or now - (rec.replaced_mono or now) > SELF_UPDATE_CONFIRM_SECONDS:
                return False
            return True
        if rec.state == "failed":
            return not (replaced or ent == "off")

        if replaced and (not rec.health_check or helper_gone):
            if ent == "off":
                return False
            rec.state, rec.replaced_mono = "confirming", now
            rec.progress = "Updated, waiting for Home Assistant to confirm"
            return True

        if replaced:
            rec.progress = "New Portainer is running, the updater is still checking it"
        elif not reachable:
            rec.progress = "Portainer is restarting"
        else:
            rec.progress = f"Updater started {dt_util.as_local(rec.started_at).strftime('%H:%M')}"

        if helper_gone and not replaced:
            if new_snapshot:
                rec.gone_streak += 1
            if rec.gone_streak >= SELF_UPDATE_GONE_SNAPSHOTS:
                self._fail_update(
                    rec, "The updater has finished, but Portainer is still running its old container."
                )
                return True
        else:
            rec.gone_streak = 0

        ceiling = (
            SELF_UPDATE_HEALTH_CHECK_CEILING_SECONDS if rec.health_check
            else SELF_UPDATE_CEILING_SECONDS
        )
        if now - rec.started_mono > ceiling:
            wait = _fmt_duration(ceiling)
            self._fail_update(
                rec,
                f"Portainer has not come back {wait} after the updater started -- check the host."
                if not reachable
                else f"The update had not finished {wait} after the updater started.",
            )
        return True

    async def _async_update_data(self) -> list[dict]:
        entity_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        now = dt_util.utcnow()
        portainer_ids = _portainer_entity_ids(entity_reg)
        found: list[dict] = []

        def _host_and_name(device_id: str | None) -> tuple[str, str | None]:
            root_id = _walk_to_root(device_reg, device_id)
            host = _device_name(device_reg, root_id) or "unknown host"
            name = _device_name(device_reg, device_id)
            return host, name

        # -- Endpoint unreachable --------------------------------------
        for endpoint_device_id in _discover_endpoint_devices(entity_reg, device_reg):
            since = _endpoint_unavailable_since(self.hass, entity_reg, endpoint_device_id)
            if since is None:
                continue
            if (now - since).total_seconds() < SETTLE_SECONDS:
                continue
            host = _device_name(device_reg, endpoint_device_id) or "unknown host"
            found.append(
                {
                    "kind": "endpoint",
                    "device_id": endpoint_device_id,
                    "host": host,
                    "host_device_id": endpoint_device_id,
                    "name": host,
                    "secondary_info": "Unreachable — reload the endpoint to reconnect",
                }
            )

        # -- Container exited / unhealthy (unchanged from pre-1.3.0) ---
        for entity_id in portainer_ids:
            if not entity_id.endswith("_state"):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state not in ("exited", "dead"):
                continue
            if (now - state.last_changed).total_seconds() < SETTLE_SECONDS:
                continue

            reg_entry = entity_reg.async_get(entity_id)
            device_id = reg_entry.device_id if reg_entry else None
            host, name = _host_and_name(device_id)
            display_name = name or state.attributes.get("friendly_name", entity_id)
            found.append(
                {
                    "kind": "container_exited",
                    "entity": entity_id,
                    "host": host,
                    "host_device_id": _walk_to_root(device_reg, device_id),
                    "stack_name": _stack_info(device_reg, entity_reg, device_id)[0],
                    "name": f"{display_name} ({host})",
                    "secondary_info": state.state,
                    "dismiss_key": _dismiss_key("container_exited", entity_id),
                }
            )

        for entity_id in portainer_ids:
            if not entity_id.endswith("_health"):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state != "unhealthy":
                continue
            if (now - state.last_changed).total_seconds() < SETTLE_SECONDS:
                continue

            reg_entry = entity_reg.async_get(entity_id)
            device_id = reg_entry.device_id if reg_entry else None
            host, name = _host_and_name(device_id)
            display_name = name or state.attributes.get("friendly_name", entity_id)
            found.append(
                {
                    "kind": "container_unhealthy",
                    "entity": entity_id,
                    "host": host,
                    "host_device_id": _walk_to_root(device_reg, device_id),
                    "stack_name": _stack_info(device_reg, entity_reg, device_id)[0],
                    "name": f"{display_name} ({host})",
                    "secondary_info": "unhealthy",
                    "dismiss_key": _dismiss_key("container_unhealthy", entity_id),
                }
            )

        # -- network_mode:service:X daemon-conflict, stuck containers ---
        for item in _find_stuck_containers(self.hass, entity_reg, device_reg):
            if item["stack_name"]:
                found.append(
                    {
                        "kind": "stack_restart_needed",
                        "device_id": item["device_id"],
                        "host": item["host"],
                        "host_device_id": item["host_device_id"],
                        "stack_name": item["stack_name"],
                        "stack_device_id": item["stack_device_id"],
                        "switch_entity_id": item["switch_entity_id"],
                        "name": item["container_name"],
                        "secondary_info": "Image updated — stack restart needed",
                    }
                )
            else:
                found.append(
                    {
                        "kind": "unstacked_recreate",
                        "device_id": item["device_id"],
                        "host": item["host"],
                        "host_device_id": item["host_device_id"],
                        "name": item["container_name"],
                        "secondary_info": "Image updated, tag stale",
                        "dismiss_key": _dismiss_key("unstacked_recreate", item["device_id"]),
                        "detail": (
                            f"{item['container_name']}'s image was pulled successfully, but the "
                            "container itself couldn't be recreated cleanly -- a known Portainer/"
                            "Docker limitation for containers sharing another container's network "
                            "(network_mode: service:<other> or container:<other>, e.g. a VPN sidecar "
                            "setup). Since this container isn't managed as a Portainer stack, it "
                            "can't be restarted automatically from here. It most likely lives in a "
                            "Docker Compose project that Portainer doesn't manage -- recreate it "
                            "manually (`docker compose up -d` on its host, or via Portainer's own "
                            "UI) to finish applying the update."
                        ),
                    }
                )

        # -- Portainer's own pending update --------------------------------
        discovered = {
            i["entity"]: i for i in _find_portainer_self_updates(self.hass, entity_reg, device_reg)
        }
        for entity_id, rec in list(self.updates.items()):
            if not self._observe_update(rec, device_reg):
                del self.updates[entity_id]
        for entity_id in list(discovered) + [e for e in self.updates if e not in discovered]:
            rec = self.updates.get(entity_id)
            item = discovered.get(entity_id) or _self_update_item(
                self.hass, entity_reg, device_reg, entity_id, rec.component if rec else None
            )
            if item is None:
                continue
            row = {
                "kind": "portainer_self_update",
                "entity": item["entity"],
                "device_id": item["device_id"],
                "host": item["host"],
                "host_device_id": item["host_device_id"],
                "stack_name": item["stack_name"],
                "stack_device_id": item["stack_device_id"],
                "name": f"{item['container_name']} ({item['host']})",
                "component": item["component"],
            }
            if rec is not None and rec.state == "failed":
                row.update(
                    secondary_info=rec.reason,
                    update_state="failed",
                    update_now=False,
                    dismiss_key=_dismiss_key("portainer_self_update", item["entity"]),
                    detail=_portainer_self_update_detail(
                        item["host"], item["component"], "failed", rec.reason
                    ),
                )
            elif rec is not None:
                row.update(
                    secondary_info=rec.progress,
                    update_state="updating",
                    update_now=False,
                    detail=_portainer_self_update_detail(
                        item["host"], item["component"], "running",
                        started=dt_util.as_local(rec.started_at).strftime("%H:%M"),
                    ),
                )
            else:
                possible = await _self_update_possible(self.hass, device_reg, entity_reg, item)
                row.update(
                    secondary_info="Update available — apply it on the host",
                    # Tells a front end that portainer_maintenance.update_portainer
                    # exists in this version of the integration, and works now.
                    update_now=possible,
                    dismiss_key=_dismiss_key("portainer_self_update", item["entity"]),
                    detail=_portainer_self_update_detail(
                        item["host"], item["component"], "possible" if possible else "manual"
                    ),
                )
            found.append(row)
        self.update_interval = (
            SELF_UPDATE_FAST_INTERVAL
            if any(r.state in ("updating", "confirming") for r in self.updates.values())
            else TROUBLE_INTERVAL
        )

        suppressed: list[dict] = []
        if self._dismissals is not None:
            visible: list[dict] = []
            for i in found:
                key = i.get("dismiss_key")
                if key and self._dismissals.is_dismissed(key):
                    stamp = self._dismissals.dismissed_at(key)
                    suppressed.append(
                        {
                            "kind": i.get("kind"),
                            "name": i.get("name"),
                            "host": i.get("host"),
                            "dismiss_key": key,
                            "dismissed_at": stamp.isoformat() if stamp else None,
                        }
                    )
                else:
                    visible.append(i)
            found = visible
        self.suppressed = suppressed
        return found


def _cleanup_source_ready(hass: HomeAssistant, entity_id: str | None, *, unknown_is_value: bool) -> bool:
    """Whether one of a host's Cleanup source sensors has a usable state yet.

    No entity at all (never created, disabled) is ready: there is nothing to
    wait for, and the Cleanup item just carries nulls for it as before. An
    entity with no state yet, or Unavailable, is not ready. Unknown is not
    ready for the image and container counts; for the reclaimable figure
    (`unknown_is_value`) core reports Unknown when nothing can be reclaimed,
    which is the value 0 (see _size_state), so it is ready."""
    if entity_id is None:
        return True
    state = hass.states.get(entity_id)
    if state is None or state.state in (None, STATE_UNAVAILABLE):
        return False
    if state.state == "unknown":
        return unknown_is_value
    return True


class PortainerStaleCoordinator(DataUpdateCoordinator[list[dict]]):
    """Devices Portainer no longer reports, found by walking the device tree
    (endpoint -> stack -> container) instead of by how long they have been
    unavailable.

    Core's portainer integration marks a container, stack or volume entity
    unavailable the moment Portainer stops listing it, and never removes the
    device. A device is *gone* when every enabled entity on it is
    unavailable, *live* when at least one is not, and is left out of the
    walk when it has no enabled entities or one of them has no state yet.

    Walking down from each healthy endpoint:

      - a gone child with no live device anywhere beneath it is stale,
        together with everything beneath it. That is an unavailable stack
        whose containers are all unavailable, or a lone container directly
        under the endpoint;
      - a live child (a stack, running or stopped) is walked into the same
        way, so a gone container under a live stack is stale. A stopped
        stack lists no containers in Portainer either, so its containers
        are stale by this rule too: core re-creates the device and entities
        of any container name it has not seen the moment the stack starts
        again, so removing them is safe;
      - a gone child that still has a live device beneath it is left alone.

    A group is only reported once the newest change among its entities is
    SETTLE_SECONDS old, so a restart (which rewrites every restored state),
    a stack redeploy or a poll hiccup can't produce a stale report.

    Event driven, no polling. A Portainer entity flipping into or out of
    `unavailable` starts one SETTLE_SECONDS timer (further flips inside that
    window don't extend it), after which the walk runs. Device and entity
    registry changes re-run it straight away, so deleting a stale device
    clears it from the list at once. If a pass skipped a group only because
    it was still inside the settle window, it schedules itself again for
    when that window ends.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=SENSOR_STALE_DEVICES, update_interval=None)
        self._unsubs: list[CALLBACK_TYPE] = []
        self._timer: CALLBACK_TYPE | None = None
        self._timer_due = 0.0

    # -- event wiring ------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start re-scanning on changes. Call once, after the first refresh."""
        bus = self.hass.bus
        self._unsubs = [
            bus.async_listen(
                EVENT_STATE_CHANGED,
                self._handle_availability_flip,
                event_filter=self._is_availability_flip,
            ),
            bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._handle_registry_updated),
            bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._handle_registry_updated),
        ]

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self._timer is not None:
            self._timer()
            self._timer = None

    @callback
    def _is_availability_flip(self, event_or_data: Any) -> bool:
        """Cheap pre-filter: only a Portainer entity going to or from
        `unavailable` (or appearing/disappearing that way) can change the
        answer. Takes the event or its data, whichever this Home Assistant
        version passes to an event filter."""
        data: Mapping[str, Any] = getattr(event_or_data, "data", event_or_data)
        old, new = data.get("old_state"), data.get("new_state")
        was_gone = old is not None and old.state == STATE_UNAVAILABLE
        is_gone = new is not None and new.state == STATE_UNAVAILABLE
        if was_gone == is_gone:
            return False
        entry = er.async_get(self.hass).async_get(data.get("entity_id"))
        return entry is not None and entry.platform == "portainer"

    @callback
    def _handle_availability_flip(self, _event: Any) -> None:
        self._schedule_scan(SETTLE_SECONDS + 1)

    @callback
    def _handle_registry_updated(self, _event: Any) -> None:
        # Not gated: a registry change doesn't need to settle, and a group
        # that is still inside its settle window is skipped by the scan
        # itself. The coordinator's own debouncer coalesces bursts.
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _schedule_scan(self, delay: float) -> None:
        due = self.hass.loop.time() + delay
        if self._timer is not None:
            if self._timer_due <= due:
                return
            self._timer()
        self._timer_due = due
        self._timer = async_call_later(self.hass, delay, self._timer_fired)

    @callback
    def _timer_fired(self, _now: datetime) -> None:
        self._timer = None
        self.hass.async_create_task(self.async_refresh())

    # -- the walk ----------------------------------------------------------

    async def _async_update_data(self) -> list[dict]:
        entity_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        now = dt_util.utcnow()

        entities_by_device: dict[str, list[str]] = {}
        for entry in entity_reg.entities.values():
            if entry.platform != "portainer" or not entry.device_id or entry.disabled_by is not None:
                continue
            entities_by_device.setdefault(entry.device_id, []).append(entry.entity_id)

        children: dict[str, list[str]] = {}
        for device_id in entities_by_device:
            device = device_reg.async_get(device_id)
            if device is not None and device.via_device_id:
                children.setdefault(device.via_device_id, []).append(device_id)

        def states_of(device_id: str) -> list:
            return [self.hass.states.get(e) for e in entities_by_device.get(device_id, ())]

        def status(device_id: str) -> str:
            """'gone' (all unavailable), 'live', or 'unknown' (nothing to judge by)."""
            states = states_of(device_id)
            if not states or any(s is None for s in states):
                return "unknown"
            if all(s.state == STATE_UNAVAILABLE for s in states):
                return "gone"
            return "live"

        def descendants(device_id: str, seen: set[str]) -> list[str]:
            out: list[str] = []
            for child in children.get(device_id, ()):
                if child in seen:
                    continue
                seen.add(child)
                out.append(child)
                out.extend(descendants(child, seen))
            return out

        # device_id -> (group root device_id, is_root)
        stale: dict[str, tuple[str, bool]] = {}
        wait_for: float | None = None

        def walk(node: str, host_root: str, visited: set[str]) -> None:
            nonlocal wait_for
            for child in children.get(node, ()):
                if child in visited:
                    continue
                visited.add(child)
                if status(child) == "gone":
                    below = descendants(child, set(visited))
                    if any(status(d) == "live" for d in below):
                        continue
                    group = [child, *below]
                    newest = min(
                        (now - s.last_changed).total_seconds()
                        for d in group
                        for s in states_of(d)
                        if s is not None
                    )
                    if newest < SETTLE_SECONDS:
                        remaining = SETTLE_SECONDS - newest
                        wait_for = remaining if wait_for is None else max(wait_for, remaining)
                        continue
                    for d in group:
                        stale[d] = (host_root, d == child)
                    visited.update(below)
                else:
                    walk(child, host_root, visited)

        for root_id in sorted(_discover_endpoint_devices(entity_reg, device_reg)):
            # An endpoint that is itself down says nothing about its children:
            # core drops the whole host from its data, so everything under it
            # reads unavailable. That is the Trouble tab's job, not this one's.
            own_states = states_of(root_id)
            if any(s is None or s.state == STATE_UNAVAILABLE for s in own_states):
                continue
            walk(root_id, root_id, {root_id})

        if wait_for is not None:
            self._schedule_scan(wait_for + 1)

        def stack_run_state(stack_device_id: str) -> str | None:
            """'running' or 'stopped' for a live stack, from its stack switch
            (on = Portainer status Active, off = Inactive); None when the
            switch is missing or has no usable state."""
            for entity_id in entities_by_device.get(stack_device_id, ()):
                if not entity_id.startswith("switch."):
                    continue
                state = self.hass.states.get(entity_id)
                if state is not None and state.state in ("on", "off"):
                    return "running" if state.state == "on" else "stopped"
            return None

        def describe(device_id: str) -> str:
            """What the Stale tab says under a device's name. The type comes
            from the device's own `model` (core sets Endpoint, Stack,
            Container or Volume); the host is left out because the item's
            name already ends with it."""
            device = device_reg.async_get(device_id)
            model = (getattr(device, "model", None) or "").strip()
            if model == "Container":
                parent_id = device.via_device_id
                parent = device_reg.async_get(parent_id) if parent_id else None
                if parent is not None and getattr(parent, "model", None) == "Stack":
                    stack_name = parent.name_by_user or parent.name
                    # A stack that is itself on this list is "stale"; one that
                    # is not still exists, so say whether it is running or
                    # stopped (or neither, if its switch can't be read).
                    kind = "stale" if parent_id in stale else stack_run_state(parent_id)
                    prefix = f"{kind} " if kind else ""
                    return f"Container in {prefix}stack {stack_name} no longer exists in Portainer"
                # Standalone, or its parent can't be told apart from the endpoint.
                return "Container no longer exists in Portainer"
            return f"{model or 'Device'} no longer exists in Portainer"

        found: list[dict] = []
        for device_id, (root_id, _is_root) in stale.items():
            host_name = _device_name(device_reg, root_id) or "unknown host"
            name = _device_name(device_reg, device_id) or device_id
            found.append(
                {
                    "name": f"{name} ({host_name})",
                    "secondary_info": describe(device_id),
                    "device_id": device_id,
                    "host": host_name,
                    "host_device_id": root_id,
                    "navigation_path": f"/config/devices/device/{device_id}",
                }
            )
        found.sort(key=lambda item: (item["host"], item["name"], item["device_id"]))
        return found


class PortainerCleanupCoordinator(DataUpdateCoordinator[list[dict]]):
    """(1.3.0, new) One item per Portainer endpoint, backing the webapp's
    Cleanup tab. There's no accurate per-endpoint dangling-image count
    anywhere in HA's own entities -- core's image_disk_usage_reclaimable
    sensor gives a byte-accurate total across ALL unused images together,
    dangling or not, with no way to split it. `unused_estimate` is
    `images_count - containers_count` from core's own per-endpoint
    diagnostic sensors instead -- a rough "how many images exist beyond
    what's running" figure, good enough to seed a badge, not a precise
    dangling count. reclaimable_mib is the real byte-accurate figure. Core
    reports Unknown for it when nothing can be reclaimed, so Unknown is sent as
    0 (see _size_state); None means the sensor doesn't exist or isn't a
    number, and `reclaimable_unavailable` is true when it exists but is
    Unavailable (the state isn't known, so the webapp offers no image prune).
    volume_usage_mib follows the same rule. `refreshed_at` is when this
    refresh read the sensors (the same for every endpoint in it): the webapp
    refetches every 15 s but this only refreshes every few minutes, so it needs
    a stamp to tell a new reading from the same one again. images_count is core's
    own total image count for the endpoint, dangling ones included, passed
    through as-is: the webapp uses images_count == 0 to disable both image
    prune actions (unused_estimate can be 0 while images still exist, e.g.
    when several containers share an image, so it can't tell "no images"
    from "nothing beyond what's running"). None when unknown.

    Readiness, per host. Each item carries `status`:

      - "ready": the host's image count, container count and reclaimable-space
        sensors all have a usable state (Unknown reclaimable is a usable 0).
        Only ready hosts add their `unused_estimate` to the sensor's value.
      - "computing": at least one of them has no usable state yet, which is
        what every host looks like for a short while after a restart. The host
        adds 0 to the sensor, so nothing is reported (no bell notification, no
        phone push) from numbers that are about to change.
      - "unavailable": it was still not ready when the backstop refresh ran
        (every CLEANUP_BACKSTOP_SECONDS), so it is not just slow to start. Also
        adds 0, and stays unavailable until it is ready.

    Hosts are independent: one host that is down never holds back another.

    Event driven, with a backstop. A change to any of a host's three source
    sensors re-reads everything after CLEANUP_DEBOUNCE_SECONDS (one timer for a
    burst, not extended by later changes), so a host becomes ready, and the
    numbers follow a prune, as soon as core reports it rather than at the next
    poll. An entity registry change (a host or sensor added or removed)
    does the same. The backstop re-reads every CLEANUP_BACKSTOP_SECONDS anyway.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=SENSOR_CLEANUP, update_interval=None)
        self._unsubs: list[CALLBACK_TYPE] = []
        self._timer: CALLBACK_TYPE | None = None
        self._timer_due = 0.0
        self._backstop_timer: CALLBACK_TYPE | None = None
        self._backstop_pending = False
        self._watched: set[str] = set()
        self._unavailable_hosts: set[str] = set()

    # -- event wiring ------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start listening and arm the backstop. Call once, after the first refresh."""
        self._unsubs = [
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED,
                self._handle_source_change,
                event_filter=self._is_source_change,
            ),
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._handle_registry_updated),
        ]
        self._arm_backstop()

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self._timer is not None:
            self._timer()
            self._timer = None
        if self._backstop_timer is not None:
            self._backstop_timer()
            self._backstop_timer = None

    @callback
    def _is_source_change(self, event_or_data: Any) -> bool:
        """Cheap pre-filter: a real change of state of one of the sensors the
        Cleanup numbers are read from. Takes the event or its data, whichever
        this Home Assistant version passes to an event filter."""
        data: Mapping[str, Any] = getattr(event_or_data, "data", event_or_data)
        if data.get("entity_id") not in self._watched:
            return False
        old, new = data.get("old_state"), data.get("new_state")
        return (old.state if old is not None else None) != (new.state if new is not None else None)

    @callback
    def _handle_source_change(self, _event: Any) -> None:
        self._schedule_scan(CLEANUP_DEBOUNCE_SECONDS)

    @callback
    def _handle_registry_updated(self, event: Any) -> None:
        data: Mapping[str, Any] = getattr(event, "data", event)
        if data.get("action") != "remove":
            entry = er.async_get(self.hass).async_get(data.get("entity_id"))
            if entry is None or entry.platform != "portainer":
                return
        self._schedule_scan(CLEANUP_DEBOUNCE_SECONDS)

    @callback
    def _schedule_scan(self, delay: float) -> None:
        due = self.hass.loop.time() + delay
        if self._timer is not None:
            if self._timer_due <= due:
                return
            self._timer()
        self._timer_due = due
        self._timer = async_call_later(self.hass, delay, self._timer_fired)

    @callback
    def _timer_fired(self, _now: datetime) -> None:
        self._timer = None
        self.hass.async_create_task(self.async_refresh())

    @callback
    def _arm_backstop(self) -> None:
        self._backstop_timer = async_call_later(self.hass, CLEANUP_BACKSTOP_SECONDS, self._backstop_fired)

    @callback
    def _backstop_fired(self, _now: datetime) -> None:
        self._backstop_timer = None
        self._arm_backstop()
        self.hass.async_create_task(self._backstop_refresh())

    async def _backstop_refresh(self) -> None:
        self._backstop_pending = True
        await self.async_refresh()

    # -- the read ----------------------------------------------------------

    async def _async_update_data(self) -> list[dict]:
        entity_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        found: list[dict] = []
        refreshed_at = dt_util.utcnow().isoformat(timespec="seconds")
        backstop = self._backstop_pending
        self._backstop_pending = False
        watched: set[str] = set()
        hosts_seen: set[str] = set()

        for endpoint_device_id in _discover_endpoint_devices(entity_reg, device_reg):
            host = _device_name(device_reg, endpoint_device_id) or "unknown host"
            hosts_seen.add(endpoint_device_id)

            images_entity = _endpoint_images_count_entity(entity_reg, endpoint_device_id)
            containers_entity = _endpoint_containers_count_entity(entity_reg, endpoint_device_id)
            reclaimable_entity = _endpoint_reclaimable_entity(entity_reg, endpoint_device_id)
            watched.update(e for e in (images_entity, containers_entity, reclaimable_entity) if e)

            images = _numeric_state(self.hass, images_entity)
            containers = _numeric_state(self.hass, containers_entity)
            unused_estimate = max(int(images) - int(containers), 0) if images is not None and containers is not None else None
            images_count = int(images) if images is not None else None

            reclaimable_mib, reclaimable_unavailable = _size_state(self.hass, reclaimable_entity)
            volume_usage_mib, _volume_unavailable = _size_state(
                self.hass, _endpoint_volume_usage_entity(entity_reg, endpoint_device_id)
            )
            volumes_prune_button = _endpoint_volumes_prune_button(entity_reg, endpoint_device_id)

            ready = (
                _cleanup_source_ready(self.hass, images_entity, unknown_is_value=False)
                and _cleanup_source_ready(self.hass, containers_entity, unknown_is_value=False)
                and _cleanup_source_ready(self.hass, reclaimable_entity, unknown_is_value=True)
            )
            if ready:
                status = CLEANUP_READY
                self._unavailable_hosts.discard(endpoint_device_id)
            elif backstop or endpoint_device_id in self._unavailable_hosts:
                status = CLEANUP_UNAVAILABLE
                self._unavailable_hosts.add(endpoint_device_id)
            else:
                status = CLEANUP_COMPUTING

            found.append(
                {
                    "host": host,
                    "device_id": endpoint_device_id,
                    "status": status,
                    "images_count": images_count,
                    "unused_estimate": unused_estimate,
                    "reclaimable_mib": reclaimable_mib,
                    "reclaimable_unavailable": reclaimable_unavailable,
                    "volume_usage_mib": volume_usage_mib,
                    "volumes_prune_button": volumes_prune_button,
                    "refreshed_at": refreshed_at,
                }
            )

        self._watched = watched
        self._unavailable_hosts &= hosts_seen
        return found


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

class _PortainerListSensor(CoordinatorEntity[DataUpdateCoordinator], SensorEntity):
    """A count + items-list sensor backed by one of the coordinators above."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        name: str,
        object_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{object_id}"
        self.entity_id = f"sensor.{object_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Portainer Maintenance",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data or [])

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {"items": self.coordinator.data or []}
        # Only the trouble coordinator has anything suppressed to report.
        suppressed = getattr(self.coordinator, "suppressed", None)
        if suppressed is not None:
            attrs["dismissed_items"] = suppressed
        return attrs


class _PortainerCleanupSensor(CoordinatorEntity[DataUpdateCoordinator], SensorEntity):
    """Same shape as _PortainerListSensor, but its native_value is the sum
    of each ready endpoint's unused_estimate (running total across all ready
    hosts), not len(items) -- one entry per endpoint here, not one per
    issue."""

    _attr_has_entity_name = False

    def __init__(self, coordinator: DataUpdateCoordinator, name: str, object_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{object_id}"
        self.entity_id = f"sensor.{object_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Portainer Maintenance",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> int:
        # Only hosts whose numbers are ready count: a host that is still
        # computing (or unavailable) adds 0, so nothing is reported from
        # numbers that are about to change. Hosts are independent.
        items = self.coordinator.data or []
        return sum(
            item.get("unused_estimate") or 0
            for item in items
            if item.get("status", CLEANUP_READY) == CLEANUP_READY
        )

    @property
    def extra_state_attributes(self) -> dict:
        return {"items": self.coordinator.data or []}


class PortainerActionsUrlSensor(SensorEntity):
    """Read-only, auto-computed click-through URL for phone notifications."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:link"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, url: str) -> None:
        self._attr_name = "Portainer actions URL"
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_ACTIONS_URL}"
        self.entity_id = f"sensor.{SENSOR_ACTIONS_URL}"
        self._attr_native_value = url
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Portainer Maintenance",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def extra_state_attributes(self) -> dict:
        """What the sidecar reads to check it can work with this integration:
        the API level (see const.API_LEVEL) and the release this code is
        running (const.RUNNING_VERSION -- unchanged by a HACS download until
        Home Assistant restarts). An integration older than this feature has
        neither attribute, which the sidecar treats as "too old"."""
        return {"api_level": API_LEVEL, "version": RUNNING_VERSION}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    updates_coordinator = PortainerUpdatesCoordinator(hass)
    trouble_coordinator = PortainerTroubleCoordinator(
        hass, hass.data[DOMAIN][entry.entry_id].get("dismissals")
    )
    stale_coordinator = PortainerStaleCoordinator(hass)
    cleanup_coordinator = PortainerCleanupCoordinator(hass)

    await updates_coordinator.async_config_entry_first_refresh()
    await trouble_coordinator.async_config_entry_first_refresh()
    await stale_coordinator.async_config_entry_first_refresh()
    await cleanup_coordinator.async_config_entry_first_refresh()

    # Stale Devices isn't polled: it re-scans when a Portainer entity goes to
    # or from unavailable, or a device/entity registry entry changes.
    stale_coordinator.async_start()
    entry.async_on_unload(stale_coordinator.async_stop)

    # Cleanup isn't polled either: it re-reads when one of a host's source
    # sensors changes, with a backstop every few minutes (see the coordinator).
    cleanup_coordinator.async_start()
    entry.async_on_unload(cleanup_coordinator.async_stop)

    hass.data[DOMAIN][entry.entry_id]["coordinators"] = {
        "updates": updates_coordinator,
        "trouble": trouble_coordinator,
        "stale": stale_coordinator,
        "cleanup": cleanup_coordinator,
    }

    actions_url = hass.data[DOMAIN][entry.entry_id].get("actions_url", "")

    async_add_entities(
        [
            _PortainerListSensor(updates_coordinator, "Portainer updates pending", SENSOR_UPDATES_PENDING, entry),
            _PortainerListSensor(trouble_coordinator, "Portainer trouble", SENSOR_TROUBLE, entry),
            _PortainerListSensor(stale_coordinator, "Portainer stale devices", SENSOR_STALE_DEVICES, entry),
            _PortainerCleanupSensor(cleanup_coordinator, "Portainer cleanup", SENSOR_CLEANUP, entry),
            PortainerActionsUrlSensor(entry, actions_url),
        ]
    )
