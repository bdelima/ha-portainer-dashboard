"""Config flow for Portainer Maintenance.

One-time setup: asks for the Portainer actions webapp's own URL (e.g.
https://actions.o.pumapants.cc) and the mobile_app device(s) that get the
"update performed" confirmation push -- everything else (the sidebar
panel, the notification click-through URL, the blueprints, the tracking
sensors) is derived or installed automatically from those two values. Only
one instance is needed.

notify_devices exists here (rather than only as a blueprint input, the
way the merged automation's own notify devices work) because it backs the
native `perform_update`/`update_done` services -- see __init__.py -- and
a native service has no per-instance blueprint input to read from. Reuses
the same values you'd pick for the automation blueprint; it's fine if
they're the same devices in both places.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import CONF_NOTIFY_DEVICES, CONF_WEBAPP_URL, DOMAIN


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    webapp_key = (
        vol.Required(CONF_WEBAPP_URL, default=defaults[CONF_WEBAPP_URL])
        if CONF_WEBAPP_URL in defaults
        else vol.Required(CONF_WEBAPP_URL)
    )
    notify_key = (
        vol.Required(CONF_NOTIFY_DEVICES, default=defaults[CONF_NOTIFY_DEVICES])
        if CONF_NOTIFY_DEVICES in defaults
        else vol.Required(CONF_NOTIFY_DEVICES)
    )
    return vol.Schema(
        {
            webapp_key: str,
            notify_key: selector.selector(
                {
                    "device": {
                        "filter": {"integration": "mobile_app"},
                        "multiple": True,
                    }
                }
            ),
        }
    )


class PortainerMaintenanceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Portainer Maintenance."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Portainer Maintenance", data=user_input)

        return self.async_show_form(step_id="user", data_schema=_schema())

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Let an existing install add/change notify_devices (added after the
        initial 1.0.x release) without deleting and re-adding the whole
        integration -- Settings -> Devices & Services -> Portainer
        Maintenance -> ... -> Reconfigure."""
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data_updates=user_input)

        return self.async_show_form(step_id="reconfigure", data_schema=_schema(entry.data))
