"""Config flow for Portainer Maintenance.

One-time setup: asks only for the Portainer actions webapp's own URL (e.g.
https://actions.o.pumapants.cc). Everything else -- the sidebar panel, the
notification click-through URL, the blueprints, the tracking sensors -- is
derived or installed automatically from that one value plus this Home
Assistant instance's own configured external/internal URL. Only one
instance is needed.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries

from .const import CONF_WEBAPP_URL, DOMAIN

STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required(CONF_WEBAPP_URL): str})


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

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA)
