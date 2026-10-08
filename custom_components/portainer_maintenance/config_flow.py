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

admin_only (default on) registers the sidebar panel as administrator-only,
the way HACS does for its own. It lives here because Home Assistant's
Settings -> Dashboards page doesn't list an integration's panel, so there is
no other place in the UI to flip it.

The sidecar has its own sign-in (AUTH_USERNAME / AUTH_PASSWORD /
AUTH_ALLOW_ANONYMOUS in its environment; with none of them set it is open).
sidecar_anonymous (default on) says it is running without one; otherwise a
second screen collects the username and password so the sidebar panel can
sign in by itself (see sidecar_auth.py). Home Assistant's forms cannot hide
or grey fields based on another field, so the credentials are a separate step
that is only shown when anonymous is off.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_ADMIN_ONLY,
    CONF_NOTIFY_DEVICES,
    CONF_SIDECAR_ANONYMOUS,
    CONF_SIDECAR_PASSWORD,
    CONF_SIDECAR_USERNAME,
    CONF_WEBAPP_URL,
    DEFAULT_ADMIN_ONLY,
    DEFAULT_SIDECAR_ANONYMOUS,
    DOMAIN,
)


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
            vol.Optional(
                CONF_ADMIN_ONLY, default=defaults.get(CONF_ADMIN_ONLY, DEFAULT_ADMIN_ONLY)
            ): bool,
            vol.Optional(
                CONF_SIDECAR_ANONYMOUS,
                default=defaults.get(CONF_SIDECAR_ANONYMOUS, DEFAULT_SIDECAR_ANONYMOUS),
            ): bool,
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


def _login_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_SIDECAR_USERNAME, default=defaults.get(CONF_SIDECAR_USERNAME, "")
            ): selector.selector({"text": {}}),
            vol.Required(
                CONF_SIDECAR_PASSWORD, default=defaults.get(CONF_SIDECAR_PASSWORD, "")
            ): selector.selector({"text": {"type": "password"}}),
        }
    )


def _credentials_missing(user_input: dict[str, Any]) -> bool:
    return not str(user_input.get(CONF_SIDECAR_USERNAME, "")).strip() or not user_input.get(
        CONF_SIDECAR_PASSWORD, ""
    )


class PortainerMaintenanceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Portainer Maintenance."""

    VERSION = 1

    def __init__(self) -> None:
        # First-screen values, held while the credentials screen is shown.
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            self._data = dict(user_input)
            if user_input.get(CONF_SIDECAR_ANONYMOUS, DEFAULT_SIDECAR_ANONYMOUS):
                return await self._async_create()
            return await self.async_step_sidecar_login()

        return self.async_show_form(step_id="user", data_schema=_schema())

    async def async_step_sidecar_login(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Second setup screen: the sidecar's username and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if _credentials_missing(user_input):
                errors["base"] = "credentials_required"
            else:
                self._data.update(user_input)
                return await self._async_create()

        return self.async_show_form(
            step_id="sidecar_login",
            data_schema=_login_schema(user_input),
            errors=errors,
        )

    async def _async_create(self) -> config_entries.ConfigFlowResult:
        # There's no supported way for a config flow to hand the browser
        # off to the automation editor on completion: the frontend passes
        # "create from this blueprint" to the editor through in-memory
        # state (showAutomationEditor()'s initialAutomationEditorData,
        # see src/data/automation.ts), not a URL or query param, so no
        # link can open that screen directly. The closest available
        # nudge is a one-time persistent notification linking to the
        # Blueprints page, where clicking the "Portainer Maintenance"
        # row opens the editor already pre-filled with this blueprint.
        #
        # Route note: the frontend's config routes are singular
        # (/config/blueprint, /config/automation). An earlier version of
        # this link used the plural /config/automations/dashboard, which
        # isn't a route and opened a blank page.
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "notification_id": "portainer_maintenance_setup_next_step",
                "title": "Portainer Maintenance: one more step",
                "message": (
                    "Setup is complete. To get update/trouble/stale-device "
                    "notifications, create an automation from the bundled "
                    "blueprint: open [Blueprints](/config/blueprint/dashboard) "
                    'and click **"Portainer Maintenance"**.'
                ),
            },
        )

        return self.async_create_entry(title="Portainer Maintenance", data=self._data)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Let an existing install add/change notify_devices (added after the
        initial 1.0.x release) without deleting and re-adding the whole
        integration -- Settings -> Devices & Services -> Portainer
        Maintenance -> ... -> Reconfigure."""
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            self._data = dict(user_input)
            if user_input.get(CONF_SIDECAR_ANONYMOUS, DEFAULT_SIDECAR_ANONYMOUS):
                # Anonymous: drop any stored credentials rather than keeping
                # a password around that nothing uses.
                data = {
                    k: v
                    for k, v in {**entry.data, **user_input}.items()
                    if k not in (CONF_SIDECAR_USERNAME, CONF_SIDECAR_PASSWORD)
                }
                return self.async_update_reload_and_abort(entry, data=data)
            return await self.async_step_reconfigure_login()

        return self.async_show_form(step_id="reconfigure", data_schema=_schema(entry.data))

    async def async_step_reconfigure_login(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Second Reconfigure screen: the sidecar's username and password."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if _credentials_missing(user_input):
                errors["base"] = "credentials_required"
            else:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, **self._data, **user_input}
                )

        return self.async_show_form(
            step_id="reconfigure_login",
            data_schema=_login_schema(user_input or entry.data),
            errors=errors,
        )
