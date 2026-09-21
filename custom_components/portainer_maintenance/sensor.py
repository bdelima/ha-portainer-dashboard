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
from datetime import timedelta

import homeassistant.util.dt as dt_util
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
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


# ---------------------------------------------------------------------------
# Coordinators -- one per sensor, matching the original recompute cadence.
# ---------------------------------------------------------------------------

class PortainerUpdatesCoordinator(DataUpdateCoordinator[list[dict]]):
    """Ports the original 5-minute update_items template."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=SENSOR_UPDATES_PENDING, update_interval=timedelta(minutes=5))

    async def _async_update_data(self) -> list[dict]:
        entity_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        found: list[dict] = []

        for entity_id in _portainer_entity_ids(entity_reg):
            if not entity_id.startswith("update."):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state != "on":
                continue

            reg_entry = entity_reg.async_get(entity_id)
            device_id = reg_entry.device_id if reg_entry else None
            root_id = _walk_to_root(device_reg, device_id)
            host = _device_name(device_reg, root_id) or "unknown host"
            friendly_name = state.attributes.get("friendly_name", entity_id)
            stack_name, stack_switch_entity_id = _stack_info(device_reg, entity_reg, device_id)

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
