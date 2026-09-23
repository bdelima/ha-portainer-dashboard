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
import re
from datetime import datetime, timedelta

import aiohttp

import homeassistant.util.dt as dt_util
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import (
    DOMAIN,
    SENSOR_ACTIONS_URL,
    SENSOR_CLEANUP,
    SENSOR_STALE_DEVICES,
    SENSOR_TROUBLE,
    SENSOR_UPDATES_PENDING,
)

_LOGGER = logging.getLogger(__name__)

TROUBLE_SETTLE_SECONDS = 120
STALE_FLOOR_SECONDS = 43200  # 12 hours


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
    Suffix-matching, same pragmatic approach _container_image_entity_id
    and _container_state_entity_id already use, since object_ids can shift
    slightly across pyportainer/core releases (e.g. "_images_count" vs
    "_image_count") -- worth confirming the exact suffix against a live
    instance if a Cleanup badge ever reads consistently empty."""
    for entity in er.async_entries_for_device(entity_reg, device_id):
        if not entity.entity_id.startswith(domain_prefix):
            continue
        for suffix in suffixes:
            if entity.entity_id.endswith(suffix):
                return entity.entity_id
    return None


def _endpoint_images_count_entity(entity_reg: er.EntityRegistry, device_id: str) -> str | None:
    return _device_entity_by_suffix(entity_reg, device_id, "sensor.", ("_images_count", "_image_count"))


def _endpoint_containers_count_entity(entity_reg: er.EntityRegistry, device_id: str) -> str | None:
    return _device_entity_by_suffix(entity_reg, device_id, "sensor.", ("_containers_count", "_container_count"))


def _endpoint_reclaimable_entity(entity_reg: er.EntityRegistry, device_id: str) -> str | None:
    return _device_entity_by_suffix(entity_reg, device_id, "sensor.", ("_image_disk_usage_reclaimable",))


def _endpoint_volume_usage_entity(entity_reg: er.EntityRegistry, device_id: str) -> str | None:
    return _device_entity_by_suffix(entity_reg, device_id, "sensor.", ("_volume_disk_usage_total",))


def _endpoint_volumes_prune_button(entity_reg: er.EntityRegistry, device_id: str) -> str | None:
    return _device_entity_by_suffix(entity_reg, device_id, "button.", ("_volumes_prune",))


def _numeric_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """A sensor's numeric value, or None for missing/unknown/unavailable --
    disk-usage sensors (backed by a separate coordinator from the main
    endpoint data) have been observed inconsistently `unknown` on some
    hosts, so every caller of this must treat None as "no number to show,"
    never math on it or format it as 0."""
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (None, "unknown", "unavailable"):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


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

RECENTLY_CONFIRMED_GRACE = timedelta(hours=25)


class PortainerUpdatesCoordinator(DataUpdateCoordinator[list[dict]]):
    """Ports the original 5-minute update_items template."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=SENSOR_UPDATES_PENDING, update_interval=timedelta(minutes=5))
        self._recently_confirmed: dict[str, datetime] = {}
        self._changelog_cache: dict[str, str | None] = {}

    def mark_recently_updated(self, update_entity: str) -> None:
        self._recently_confirmed[update_entity] = dt_util.utcnow()

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
        now = dt_util.utcnow()

        # Computed once per poll, not once per item -- see
        # _stacks_with_open_trouble's docstring. Cheap (registry/state
        # reads only), so no reason to cache it further.
        stuck_stacks = _stacks_with_open_trouble(self.hass, entity_reg, device_reg)

        for entity_id in _portainer_entity_ids(entity_reg):
            if not entity_id.startswith("update."):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state == "off":
                self._recently_confirmed.pop(entity_id, None)
                continue
            if state.state != "on":
                continue

            confirmed_at = self._recently_confirmed.get(entity_id)
            if confirmed_at is not None:
                if now - confirmed_at < RECENTLY_CONFIRMED_GRACE:
                    continue
                del self._recently_confirmed[entity_id]

            reg_entry = entity_reg.async_get(entity_id)
            device_id = reg_entry.device_id if reg_entry else None
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
        the same TROUBLE_SETTLE_SECONDS as container issues, to avoid
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
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=SENSOR_TROUBLE, update_interval=timedelta(minutes=1))

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
            if (now - since).total_seconds() < TROUBLE_SETTLE_SECONDS:
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
            if (now - state.last_changed).total_seconds() < TROUBLE_SETTLE_SECONDS:
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
                }
            )

        for entity_id in portainer_ids:
            if not entity_id.endswith("_health"):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state != "unhealthy":
                continue
            if (now - state.last_changed).total_seconds() < TROUBLE_SETTLE_SECONDS:
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

        return found


class PortainerStaleCoordinator(DataUpdateCoordinator[list[dict]]):
    """Ports the original hourly stale_items template (12h floor)."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=SENSOR_STALE_DEVICES, update_interval=timedelta(hours=1))

    async def _async_update_data(self) -> list[dict]:
        entity_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        now = dt_util.utcnow()
        portainer_ids = set(_portainer_entity_ids(entity_reg))

        devices_seen: set[str] = set()
        for entity_id in portainer_ids:
            reg_entry = entity_reg.async_get(entity_id)
            if reg_entry and reg_entry.device_id:
                devices_seen.add(reg_entry.device_id)

        found: list[dict] = []

        for device_id in devices_seen:
            root_id = _walk_to_root(device_reg, device_id)

            if root_id == device_id:
                continue

            dev_entities = [
                e.entity_id
                for e in entity_reg.entities.values()
                if e.device_id == device_id and e.entity_id in portainer_ids
            ]
            if not dev_entities:
                continue

            states = [self.hass.states.get(e) for e in dev_entities]
            if any(s is None for s in states):
                continue
            if not all(s.state == "unavailable" for s in states):
                continue

            host_name = _device_name(device_reg, root_id) or "unknown host"

            if root_id and root_id != device_id:
                endpoint_entities = [
                    e.entity_id
                    for e in entity_reg.entities.values()
                    if e.device_id == root_id and e.entity_id in portainer_ids
                ]
                if endpoint_entities:
                    endpoint_states = [self.hass.states.get(e) for e in endpoint_entities]
                    endpoint_healthy = all(
                        s is not None and s.state != "unavailable" for s in endpoint_states
                    )
                else:
                    endpoint_healthy = True
            else:
                endpoint_healthy = True

            if not endpoint_healthy:
                continue

            min_age = min((now - s.last_changed).total_seconds() for s in states)
            if min_age < STALE_FLOOR_SECONDS:
                continue

            name = _device_name(device_reg, device_id) or device_id
            found.append(
                {
                    "name": f"{name} ({host_name})",
                    "secondary_info": "Stale — 12h+ unavailable, host healthy",
                    "device_id": device_id,
                    "host": host_name,
                    "host_device_id": root_id,
                    "navigation_path": f"/config/devices/device/{device_id}",
                }
            )

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
    dangling count. reclaimable_mib is the real byte-accurate figure
    (None when that sensor reads unknown/unavailable -- see
    _numeric_state, never treat None as 0 here)."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=SENSOR_CLEANUP, update_interval=timedelta(minutes=5))

    async def _async_update_data(self) -> list[dict]:
        entity_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        found: list[dict] = []

        for endpoint_device_id in _discover_endpoint_devices(entity_reg, device_reg):
            host = _device_name(device_reg, endpoint_device_id) or "unknown host"

            images = _numeric_state(self.hass, _endpoint_images_count_entity(entity_reg, endpoint_device_id))
            containers = _numeric_state(self.hass, _endpoint_containers_count_entity(entity_reg, endpoint_device_id))
            unused_estimate = max(int(images) - int(containers), 0) if images is not None and containers is not None else None

            reclaimable_mib = _numeric_state(self.hass, _endpoint_reclaimable_entity(entity_reg, endpoint_device_id))
            volume_usage_mib = _numeric_state(self.hass, _endpoint_volume_usage_entity(entity_reg, endpoint_device_id))
            volumes_prune_button = _endpoint_volumes_prune_button(entity_reg, endpoint_device_id)

            found.append(
                {
                    "host": host,
                    "device_id": endpoint_device_id,
                    "unused_estimate": unused_estimate,
                    "reclaimable_mib": reclaimable_mib,
                    "volume_usage_mib": volume_usage_mib,
                    "volumes_prune_button": volumes_prune_button,
                }
            )

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
        return {"items": self.coordinator.data or []}


class _PortainerCleanupSensor(CoordinatorEntity[DataUpdateCoordinator], SensorEntity):
    """Same shape as _PortainerListSensor, but its native_value is the sum
    of each endpoint's unused_estimate (running total across all hosts),
    not len(items) -- one entry per endpoint here, not one per issue."""

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
        items = self.coordinator.data or []
        return sum(item.get("unused_estimate") or 0 for item in items)

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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    updates_coordinator = PortainerUpdatesCoordinator(hass)
    trouble_coordinator = PortainerTroubleCoordinator(hass)
    stale_coordinator = PortainerStaleCoordinator(hass)
    cleanup_coordinator = PortainerCleanupCoordinator(hass)

    await updates_coordinator.async_config_entry_first_refresh()
    await trouble_coordinator.async_config_entry_first_refresh()
    await stale_coordinator.async_config_entry_first_refresh()
    await cleanup_coordinator.async_config_entry_first_refresh()

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
