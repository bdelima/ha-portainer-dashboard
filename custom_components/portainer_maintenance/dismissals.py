"""Persistent dismissals for Needs Remediation (trouble) items.

Some trouble items have no remediation this integration can perform
(a container that exited, an unhealthy container, Portainer's own update
waiting to be applied on its host, ...): the user has to act on the host,
or has decided the item is expected. Those items can be dismissed from the
dashboard so they stop showing up (and stop counting toward
sensor.portainer_trouble and its notifications).

Lifetime of a dismissal: it is stored in HA's .storage, so it survives
restarts, but it is only cleared when Home Assistant starts AND at least
DISMISSAL_MIN_AGE has passed since it was dismissed. A restart sooner than
that keeps it dismissed. A key is whatever the trouble coordinator puts in
an item's `dismiss_key` ("<kind>:<entity or device id>").
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.dismissals"
DISMISSAL_MIN_AGE = timedelta(hours=24)


class DismissalStore:
    """In-memory set of dismissed keys (with when each was dismissed),
    persisted to HA's .storage."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._dismissed: dict[str, datetime] = {}

    async def async_load(self, *, ha_starting: bool) -> None:
        """Load saved dismissals. When `ha_starting` (this setup is part of
        Home Assistant starting, not an integration reload at runtime),
        drop every dismissal that is at least DISMISSAL_MIN_AGE old."""
        data = await self._store.async_load()
        raw = data.get("dismissed", {}) if isinstance(data, dict) else {}
        loaded: dict[str, datetime] = {}
        if isinstance(raw, dict):
            for key, stamp in raw.items():
                try:
                    parsed = dt_util.parse_datetime(stamp) if isinstance(stamp, str) else None
                except ValueError:
                    parsed = None
                if parsed is not None and parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if isinstance(key, str) and key and parsed is not None:
                    loaded[key] = parsed

        pruned = 0
        if ha_starting:
            now = dt_util.utcnow()
            for key, dismissed_at in list(loaded.items()):
                if now - dismissed_at >= DISMISSAL_MIN_AGE:
                    del loaded[key]
                    pruned += 1

        self._dismissed = loaded
        if pruned:
            _LOGGER.info(
                "%s: cleared %d dismissal(s) older than %s on startup",
                DOMAIN, pruned, DISMISSAL_MIN_AGE,
            )
            await self._async_save()

    def is_dismissed(self, key: str) -> bool:
        return key in self._dismissed

    def dismissed_at(self, key: str) -> datetime | None:
        """When `key` was dismissed, or None if it isn't dismissed."""
        return self._dismissed.get(key)

    async def async_dismiss(self, key: str) -> None:
        self._dismissed[key] = dt_util.utcnow()
        await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"dismissed": {key: stamp.isoformat() for key, stamp in self._dismissed.items()}}
        )
