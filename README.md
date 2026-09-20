# Portainer Maintenance

A Home Assistant custom integration that turns the core [`portainer`](https://www.home-assistant.io/integrations/portainer/) integration into a full review-and-approve maintenance workflow for container updates, container health, and stale-device cleanup — built on top of a companion notification/automation design. See the [full design doc](https://github.com/bdelima/ha-portainer-maintenance/blob/main/docs/portainer-ha-container-management-design.md) for the whole system this integration is one piece of.

On setup, this integration:

- Registers `portainer_maintenance.remove_device` — a real service for deleting stale Portainer devices/stacks that HA's own UI can't delete via any documented service (built on the stable `device_registry.async_remove_device()` API).
- Installs its own bundled automation and script blueprints into your Home Assistant config automatically — no manual file copying.
- Registers a sidebar panel (iframe) pointing at your own Portainer-actions webapp, at a fixed path — no dashboard-title guesswork.
- Computes the notification click-through URL automatically from your Home Assistant instance's own configured external/internal URL.
- Exposes three native tracking sensors — `sensor.portainer_updates_pending`, `sensor.portainer_container_trouble`, and `sensor.portainer_stale_devices` — replacing what used to be hand-maintained YAML template sensors.

## Requirements

- Home Assistant with the core `portainer` integration already configured against at least one Portainer endpoint.
- A reachable webapp URL for the sidebar panel to point at (see the design doc's "Webpage dashboard" section) — this integration doesn't provide that webapp itself.

## Installation via HACS

1. HACS → the "⋮" menu (top right) → **Custom repositories**.
2. Repository: `https://github.com/bdelima/ha-portainer-maintenance`, Category: **Integration**.
3. Find **Portainer Maintenance** in HACS → Integrations → **+ Explore & Download Repositories**, install it.
4. **Restart Home Assistant** — new `custom_components` are only loaded at startup.
5. Settings → Devices & Services → **Add Integration** → search "Portainer Maintenance" → enter your webapp's URL.
6. Settings → Automations & Scenes → Create Automation → **Use Blueprint** → "Portainer Maintenance: automations (merged)" → pick your notify device.
7. Settings → Automations & Scenes → Scripts → Add Script → **Use Blueprint** → both "Portainer Maintenance: ..." script blueprints → pick your notify device for each, then set each script's Entity ID explicitly (via its settings cog) to `portainer_perform_update` / `portainer_update_done`.

## Manual installation (without HACS)

Copy `custom_components/portainer_maintenance/` into your Home Assistant config's `custom_components/` directory, then follow steps 4–7 above.

## Migrating from an older "Portainer Cleanup" setup

If you previously ran a separate `portainer_cleanup` integration, a hand-maintained `templates.yaml`, and/or standalone blueprint files:

- Remove the old `portainer_cleanup` integration (Settings → Devices & Services) — different domain, so it won't conflict, but no reason to keep it.
- Remove `templates.yaml`'s `template: !include` line from `configuration.yaml` and delete `templates.yaml` **before restarting** — otherwise the new native sensors will collide with the old template-based ones and land on `_2`-suffixed entity IDs instead of the real ones.
- The old standalone blueprint files can be left in place or removed; they simply aren't the ones used anymore (this integration installs its own copies under `blueprints/*/portainer_maintenance/`).

## License

MIT — see [LICENSE](LICENSE).
