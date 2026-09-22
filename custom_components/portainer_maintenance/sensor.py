"""Tracking sensors for Portainer Maintenance.

Native replacements for the three trigger-based template sensors that used
to live in templates.yaml (sensor.portainer_updates_pending,
sensor.portainer_container_trouble, sensor.portainer_stale_devices), plus a
new read-only sensor.portainer_actions_url. These moved here specifically
because trigger-based template sensors with a shared `variables:` block
have no Helpers UI editor at all -- as native integration entities, that
constraint disappears entirely.

Each of the three list sensors ports its original Jinja logic into plain
Python against the device/entity registries directly (the same registries
`device_attr()`, `config_entry_attr()`, `device_id()` etc. read from under
the hood in templates) rather than executor-offloaded work, since none of
this touches disk or the network -- registry/state reads are fine directly
on the event loop, same as template rendering itself.

Entity_ids are pinned explicitly (self.entity_id set before add) to the
exact values templates.yaml used to produce, so the merged automation
blueprint and the webapp's REST calls don't need to change. This only
lands cleanly if the old templates.yaml-based sensors are removed BEFORE
this integration's sensors are set up -- otherwise HA's registry will
auto-suffix these as _2 to avoid colliding with the old ones.
"""
from __future__ import annotations

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
    SENSOR_CONTAINER_TROUBLE,
    SENSOR_STALE_DEVICES,
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

    The device hierarchy is Endpoint -> Stack -> Container (confirmed via
    HA's own device list, not assumed): a container's immediate parent
    (via_device_id) is its stack. But a *standalone* container (deployed
    outside Compose) is parented directly to the Endpoint instead, with no
    Stack device in between -- so the immediate parent alone doesn't tell
    us which case we're in. The distinguishing check: a real Stack device
    has its own via_device_id pointing further up to the Endpoint, while
    the Endpoint itself has none (same root-detection trick used
    elsewhere in this file). If the immediate parent has no further
    parent, it IS the Endpoint, and this container has no stack.

    Used both by the updates-pending sensor (to group the dashboard's
    tree view) and by __init__.py's perform_update (to find the switch.*
    entity to offer restarting when a recreate hits the known
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


def _container_image_entity_id(entity_reg: er.EntityRegistry, container_device_id: str | None) -> str | None:
    """The sensor.<name>_image entity on a container's own device, if any
    -- core's portainer integration creates one per container. Shared by
    the changelog-link lookup below and, in __init__.py, by
    handle_perform_update's recreate-outcome check -- both need "what
    image reference is this container on right now," just for different
    reasons."""
    if container_device_id is None:
        return None
    for entity in er.async_entries_for_device(entity_reg, container_device_id):
        if entity.entity_id.startswith("sensor.") and entity.entity_id.endswith("_image"):
            return entity.entity_id
    return None


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


# ---------------------------------------------------------------------------
# Changelog links -- a hand-curated override table, backed by automatic
# discovery for anything not in it.
#
# The core portainer integration's update.* entities carry no changelog
# data at all (confirmed: they're pure digest comparisons -- installed/
# latest_version are raw SHA256 hashes, not semantic tags, and neither
# release_summary nor release_url is ever set). Reading it from the
# image's own OCI labels (org.opencontainers.image.source) was considered
# and rejected: it needs its own Portainer API credentials (a new,
# separate setup field this integration doesn't otherwise require), and
# even then, coverage is inconsistent -- confirmed present on some
# GHCR/GitHub-Actions-built images (e.g. gluetun), confirmed ABSENT on
# linuxserver.io images (a large share of a typical homelab), which set
# only build_version/maintainer labels, no OCI annotations at all.
#
# Instead of reading labels, discovery reads the same two public places a
# web search on an image tag tends to surface a GitHub link from -- just
# directly, without going through a search engine (which would mean
# scraping search-result HTML with no sanctioned API, and realistically
# getting rate-limited/CAPTCHA'd by a home server hitting it repeatedly):
#   - ghcr.io images: the image path *is* a GitHub owner/repo path, so the
#     URL is a direct guess (verified with a live request before use, not
#     assumed correct).
#   - Docker Hub images (this is what covers LSIO): Docker Hub's own public
#     repository API returns the README text, which conventionally links
#     back to the upstream GitHub repo; the first plausible match is
#     extracted and, again, verified live before use.
# Both lookups happen at most once per distinct image repo path -- the
# result (including "nothing found") is cached on the coordinator for the
# life of the integration, so this never runs on every 5-minute poll, only
# the first time a given image is seen with a pending update.
#
# The table below is an override, checked first and always wins over
# whatever discovery would find -- useful when an image's Docker
# Hub/registry path doesn't match its real upstream repo (Home Assistant's
# own image is kept here for exactly that reason) or when discovery simply
# can't reach a conclusion (a private/self-hosted registry, or a Docker
# Hub README with no usable link). Nothing needs to be added here anymore
# for the common case -- add an entry only to correct or guarantee a
# specific mapping.
#
# Keys are the image reference's repository path ONLY -- no registry
# host, no tag, no digest (see _split_image_repo). A few projects publish
# multiple image variants (different base OS/arch) under different repo
# paths for the same upstream project; add one entry per variant actually
# in use rather than trying to pattern-match them.
#
# Plex is deliberately not here, and discovery will never find it either:
# Plex Media Server is closed-source, so there is no public GitHub
# releases page to link to at all -- not a gap in this table or in
# discovery, an inherent limit of the upstream project.
_KNOWN_CHANGELOG_URLS: dict[str, str] = {
    "homeassistant/home-assistant": "https://github.com/home-assistant/core/releases",
    "qmcgaw/gluetun": "https://github.com/qdm12/gluetun/releases",
    "portainer/portainer-ce": "https://github.com/portainer/portainer/releases",
}

# Docker Hub README links that are never the project's own repo -- GitHub
# path segments that happen to look like an "owner" but are actually a
# platform feature.
_GITHUB_NON_REPO_OWNERS = {
    "sponsors", "apps", "marketplace", "orgs", "settings", "about",
    "features", "pricing", "topics", "collections", "trending", "explore",
    "login", "join", "search",
}
_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+)"
)


def _split_image_repo(image_ref: str) -> tuple[str | None, str | None]:
    """'lscr.io/linuxserver/plex:1.32.5' -> ('lscr.io', 'linuxserver/plex').
    Strips a digest (@sha256:...), a tag (:latest), and a leading registry
    host (anything before the first '/' that looks like a host -- contains
    a '.' or ':', or is exactly 'localhost' -- since a bare Docker Hub
    image has no host segment at all, e.g. 'homeassistant/home-assistant'
    with no leading docker.io/). host is None when the ref has no explicit
    registry host (i.e. it's Docker Hub). Returns (None, None) for an
    empty/unparseable ref."""
    if not image_ref:
        return None, None
    ref = image_ref.split("@", 1)[0]  # drop a digest, if present

    parts = ref.split("/")
    host = None
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        host = parts[0]
        parts = parts[1:]  # drop the registry host segment
    ref = "/".join(parts)

    # Drop a trailing :tag -- but only the last segment's ':', since a
    # registry host earlier in the ref (already stripped above, but just
    # in case) could itself contain one.
    if ":" in ref.rsplit("/", 1)[-1]:
        ref = ref.rsplit(":", 1)[0]

    return host, (ref or None)


def _normalize_image_repo(image_ref: str) -> str | None:
    """Repo-path-only convenience wrapper around _split_image_repo, kept
    for callers that only care about the table-lookup key."""
    return _split_image_repo(image_ref)[1]


def _guess_ghcr_repo_url(repo: str) -> str | None:
    """A ghcr.io image path IS a GitHub owner/repo path -- e.g.
    ghcr.io/immich-app/immich-server maps to github.com/immich-app/immich-server.
    This is a guess, not a certainty (a project can publish an image under a
    path segment that isn't its exact repo name), which is why the caller
    always verifies it with a live request before trusting it."""
    parts = repo.split("/")
    if len(parts) < 2:
        return None
    return f"https://github.com/{parts[0]}/{parts[1]}"


def _first_github_repo_url(text: str) -> str | None:
    """Pull the first plausible github.com/<owner>/<repo> URL out of free
    text (a Docker Hub README), skipping GitHub path segments that are
    platform features rather than a user/org (github.com/sponsors/...)."""
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
    """Reads the repo's public Docker Hub page data (no auth needed for a
    public repo) and extracts a GitHub link from its README text, the same
    text a web search on the image tag tends to surface a GitHub result
    from in the first place -- just read directly instead of through a
    search engine. 'redis' (a Docker Official Image, no namespace) lives
    under the 'library' namespace on Docker Hub's API."""
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
    """A guessed/scraped URL is only trusted once it's confirmed live --
    this is what turns "probably right" into "actually resolves right
    now", at the cost of one HTTPS round trip, done once per repo and
    cached after that (see PortainerUpdatesCoordinator._changelog_cache)."""
    session = aiohttp_client.async_get_clientsession(hass)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=True) as resp:
            return resp.status == 200
    except (aiohttp.ClientError, TimeoutError):
        return False


# ---------------------------------------------------------------------------
# Coordinators -- one per sensor, matching the original recompute cadence.
# ---------------------------------------------------------------------------

# Confirmed, unfixed, unmerged HA core bug (home-assistant/core#182584): a
# portainer update.* entity's own internal watcher cache is keyed to the
# container's OLD id, so after perform_update actually recreates it, this
# entity's state can keep reporting "on" (update available) for as long as
# 24h -- its own next full rescan -- even though the update genuinely
# completed. We can't fix that cache from here; instead, once
# perform_update tells us a given update_entity's recreate went through
# (see __init__.py), we hide that entity from this list ourselves for a
# while, rather than showing the user a "pending update" we already know
# is stale. Set past the full 24h the core bug can persist, rather than
# stopping short of it -- a suppression that expires early just means the
# blueprint treats the stale "on" reappearing as a *newly appeared* update
# and sends a fresh push about it, which is worse than the original
# problem. A second *real* update landing for the same container within
# 25h of the last one is effectively never going to happen in practice.
RECENTLY_CONFIRMED_GRACE = timedelta(hours=25)


class PortainerUpdatesCoordinator(DataUpdateCoordinator[list[dict]]):
    """Ports the original 5-minute update_items template."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=SENSOR_UPDATES_PENDING, update_interval=timedelta(minutes=5))
        self._recently_confirmed: dict[str, datetime] = {}
        # Changelog-URL discovery result per normalized image repo path,
        # including a cached None for "looked, found nothing" -- so a
        # never-mapped image (or one whose registry/README yields nothing
        # useful) is only ever attempted once per HA restart, not every
        # 5-minute poll.
        self._changelog_cache: dict[str, str | None] = {}

    def mark_recently_updated(self, update_entity: str) -> None:
        """Called by __init__.py's perform_update once a recreate for this
        entity has actually gone through (with or without hitting the
        network_mode:service:X daemon-conflict case) -- see
        RECENTLY_CONFIRMED_GRACE above for why this exists."""
        self._recently_confirmed[update_entity] = dt_util.utcnow()

    async def _resolve_changelog_url(
        self, entity_reg: er.EntityRegistry, container_device_id: str | None
    ) -> str | None:
        """The changelog/releases URL for a container's current image:
        the manual override table if it's listed there, otherwise an
        auto-discovery attempt (cached after the first try, successful or
        not). Reads the sibling sensor.<name>_image entity core's
        portainer integration already creates on the same device -- same
        "look at what's already on this device" pattern _stack_info uses
        for a stack's switch entity, just on the container's own device
        instead of its parent."""
        if container_device_id is None:
            return None
        image_entity_id = _container_image_entity_id(entity_reg, container_device_id)
        if image_entity_id is None:
            return None
        state = self.hass.states.get(image_entity_id)
        image_ref = state.state if state else None
        if not image_ref:
            return None

        host, repo = _split_image_repo(image_ref)
        if not repo:
            return None

        known = _KNOWN_CHANGELOG_URLS.get(repo)
        if known:
            return known

        if repo in self._changelog_cache:
            return self._changelog_cache[repo]

        discovered: str | None = None
        try:
            if host == "ghcr.io":
                guess = _guess_ghcr_repo_url(repo)
                if guess and await _verify_github_url(self.hass, guess):
                    discovered = guess
            elif host is None or host in (
                "docker.io", "index.docker.io", "registry-1.docker.io",
                # lscr.io is linuxserver.io's own pull-through mirror of
                # their Docker Hub images, at identical namespace/repo
                # paths (documented by linuxserver.io itself) -- so it's
                # the exact same lookup as a real Docker Hub image, just
                # pulled through a different hostname. This is what makes
                # discovery actually cover LSIO, the whole point of it.
                "lscr.io",
            ):
                candidate = await _fetch_dockerhub_github_url(self.hass, repo)
                if candidate and await _verify_github_url(self.hass, candidate):
                    discovered = candidate
            # Any other registry host (private/self-hosted) -- no generic,
            # credential-free way to discover from here; discovered stays
            # None, same as an unmapped image before this feature existed.
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

        for entity_id in _portainer_entity_ids(entity_reg):
            if not entity_id.startswith("update."):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state == "off":
                # Gone, or genuinely confirmed no-update-pending -- either
                # way trustworthy on its own, so drop any suppression for
                # it immediately rather than waiting out the grace window,
                # so a real subsequent update is never masked by a stale
                # entry.
                self._recently_confirmed.pop(entity_id, None)
                continue
            if state.state != "on":
                # Something else -- most likely "unavailable", which the
                # entity can go through transiently while its container is
                # mid-recreate, or during an unrelated core-integration
                # polling hiccup. That's not a trustworthy "no update"
                # signal the way "off" is, so leave any existing
                # suppression alone rather than let a flicker resurface
                # the known-stale "on" the moment it comes back.
                continue

            confirmed_at = self._recently_confirmed.get(entity_id)
            if confirmed_at is not None:
                if now - confirmed_at < RECENTLY_CONFIRMED_GRACE:
                    continue
                # Grace window elapsed and core still reports "on" -- the
                # 24h cache is the likely explanation, but we've done what
                # we reasonably can; let it reappear rather than hide it
                # forever on the strength of one confirmation.
                del self._recently_confirmed[entity_id]

            reg_entry = entity_reg.async_get(entity_id)
            device_id = reg_entry.device_id if reg_entry else None
            root_id = _walk_to_root(device_reg, device_id)
            host = _device_name(device_reg, root_id) or "unknown host"
            friendly_name = state.attributes.get("friendly_name", entity_id)
            stack_name, stack_switch_entity_id = _stack_info(device_reg, entity_reg, device_id)
            changelog_url = await self._resolve_changelog_url(entity_reg, device_id)

            found.append(
                {
                    "entity": entity_id,
                    "name": f"{friendly_name} ({host})",
                    "secondary_info": "Update available",
                    # None/None for a standalone container not part of a
                    # stack -- the dashboard's tree view groups those under
                    # a flat "Standalone" bucket instead of a named stack.
                    "stack_name": stack_name,
                    "stack_switch_entity_id": stack_switch_entity_id,
                    # None when there's no override AND discovery couldn't
                    # verify a link (or the registry isn't one it knows how
                    # to read) -- the dashboard just omits the link.
                    "changelog_url": changelog_url,
                }
            )

        return found


class PortainerTroubleCoordinator(DataUpdateCoordinator[list[dict]]):
    """Ports the original 1-minute trouble_items template (120s settle)."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=SENSOR_CONTAINER_TROUBLE, update_interval=timedelta(minutes=1))

    async def _async_update_data(self) -> list[dict]:
        entity_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        now = dt_util.utcnow()
        portainer_ids = _portainer_entity_ids(entity_reg)
        found: list[dict] = []

        def _host_and_name(entity_id: str, device_id: str | None) -> tuple[str, str | None]:
            root_id = _walk_to_root(device_reg, device_id)
            host = _device_name(device_reg, root_id) or "unknown host"
            name = _device_name(device_reg, device_id)
            return host, name

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
            host, name = _host_and_name(entity_id, device_id)
            display_name = name or state.attributes.get("friendly_name", entity_id)
            found.append({"entity": entity_id, "name": f"{display_name} ({host})", "secondary_info": state.state})

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
            host, name = _host_and_name(entity_id, device_id)
            display_name = name or state.attributes.get("friendly_name", entity_id)
            found.append({"entity": entity_id, "name": f"{display_name} ({host})", "secondary_info": "unhealthy"})

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

            # A device with no via_device_id IS a root Endpoint -- never a
            # candidate for "stale child device" itself. Without this guard,
            # an endpoint whose own entities go unavailable for 12h+ (the
            # whole host down, not a removed container) would misleadingly
            # show up in the stale list, since it has no separate "parent"
            # to check health against. Found via the logic port's own test
            # suite -- this edge case was latent in the original Jinja too.
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
                    "navigation_path": f"/config/devices/device/{device_id}",
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


class PortainerActionsUrlSensor(SensorEntity):
    """Read-only, auto-computed click-through URL for phone notifications.

    Nothing to configure -- it's derived from this HA instance's own
    external/internal URL plus the fixed sidebar panel path registered in
    __init__.py, so there's no value here that can be entered wrong.
    """

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

    await updates_coordinator.async_config_entry_first_refresh()
    await trouble_coordinator.async_config_entry_first_refresh()
    await stale_coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id]["coordinators"] = {
        "updates": updates_coordinator,
        "trouble": trouble_coordinator,
        "stale": stale_coordinator,
    }

    actions_url = hass.data[DOMAIN][entry.entry_id].get("actions_url", "")

    async_add_entities(
        [
            _PortainerListSensor(updates_coordinator, "Portainer updates pending", SENSOR_UPDATES_PENDING, entry),
            _PortainerListSensor(trouble_coordinator, "Portainer container trouble", SENSOR_CONTAINER_TROUBLE, entry),
            _PortainerListSensor(stale_coordinator, "Portainer stale devices", SENSOR_STALE_DEVICES, entry),
            PortainerActionsUrlSensor(entry, actions_url),
        ]
    )
