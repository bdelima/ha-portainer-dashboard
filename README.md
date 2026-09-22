# Portainer Maintenance

A Home Assistant custom integration that turns the core [`portainer`](https://www.home-assistant.io/integrations/portainer/) integration into a full review-and-approve maintenance workflow for container updates, container health, and stale-device cleanup — built on top of a companion notification/automation design. See the [full design doc](https://github.com/bdelima/ha-portainer-dashboard/blob/main/docs/portainer-ha-container-management-design.md) for the whole system this integration is one piece of.

On setup, this integration:

- Registers `portainer_maintenance.remove_device` — a real service for deleting stale Portainer devices/stacks that HA's own UI can't delete via any documented service (built on the stable `device_registry.async_remove_device()` API).
- Registers `portainer_maintenance.prune_images` — reclaims disk space by pruning Docker images across every Portainer endpoint this HA instance knows about, discovered automatically from the device registry (no static host list to maintain).
- Registers `portainer_maintenance.perform_update` and `portainer_maintenance.update_done` — native services (not user-created scripts) for actually installing an update and posting the "update performed" confirmation, notifying whichever mobile_app device(s) you pick during setup. These used to be separate script blueprints requiring you to manually set each script's Entity ID to match what the automation blueprint called by name — an easy step to get wrong, which HA would then report as a cryptic "automation uses an unknown action" error. A native service has no such step to miss.
- Installs its own bundled automation blueprint into your Home Assistant config automatically — no manual file copying.
- Registers a sidebar panel (iframe) pointing at your own Portainer-actions webapp, at a fixed path — no dashboard-title guesswork.
- Computes the notification click-through URL automatically as a relative path, which the HA companion app opens inside the app itself — no dependency on HA's own external/internal URL (Settings → System → Network) being configured at all.
- Exposes three native tracking sensors — `sensor.portainer_updates_pending`, `sensor.portainer_container_trouble`, and `sensor.portainer_stale_devices` — replacing what used to be hand-maintained YAML template sensors.
- Keeps a native HA notification (the bell icon at the top of the sidebar) up to date with a running total across all three sensors whenever anything needs attention, and dismisses it once everything's clear — a real badge/count indicator with no third-party sidebar plugin needed.
- Registers `portainer_maintenance.hide_update_entities` and calls it automatically (at startup, and whenever a new Portainer `update.*` entity is created) — hides that entity from HA's own entity list so it doesn't clutter Settings, since its actual pending/not-pending state is meant to be reviewed on the dashboard instead. Closes what used to be a manual "remember to hide it" step for every new container.
- Attaches a `changelog_url` to each item in `sensor.portainer_updates_pending` when one can be found, surfaced by the webapp as a "Changelog" link next to Install, always pointed at the project's GitHub *Releases* page rather than its repo home page. A small hand-curated table covers a few pinned images directly; everything else is resolved automatically — both a `ghcr.io` image and a Docker Hub (or `lscr.io`) image first try a direct owner/repo guess against GitHub (right for any project, self-published images included, whose Docker Hub namespace and repo name match its GitHub owner/repo exactly), falling back to scanning the Docker Hub listing's README text for a GitHub link only when that direct guess doesn't resolve (needed for a project like linuxserver.io, whose GitHub repos are named `docker-<app>` rather than `<app>`) — with any guess verified live before it's used, and the result cached so this only costs a network round trip once per image ever seen with a pending update.
  - **Overriding a changelog link without a new release:** drop a JSON file named `portainer_maintenance_changelog_overrides.json` in your Home Assistant config directory (the same folder as `configuration.yaml`) with entries shaped `{"owner/repo-path": "https://..."}`, keyed by the image's repo path exactly as it appears in the image reference (no registry host, tag, or digest — e.g. `qmcgaw/gluetun`, not `ghcr.io/qmcgaw/gluetun:latest`). This file always wins over the integration's own built-in table when both have an entry for the same image, and is re-read on the next 5-minute poll — no HA restart, no new integration version. Example:
    ```json
    {
      "myorg/myimage": "https://github.com/myorg/myimage/releases",
      "someowner/some-fork": "https://github.com/someowner/some-fork/releases"
    }
    ```

## Requirements

- Home Assistant with the core `portainer` integration already configured against at least one Portainer endpoint.
- The [Portainer Sidecar](https://github.com/bdelima/ha-portainer-sidecar) webapp, running as its own Docker container and reachable from Home Assistant — this integration only points a sidebar panel at its URL, it doesn't build, run, or provide that container itself. A prebuilt multi-arch image is published to Docker Hub as [`bdelima/ha-portainer-sidecar`](https://hub.docker.com/r/bdelima/ha-portainer-sidecar) (`docker pull bdelima/ha-portainer-sidecar:latest`); see that repo's README for Compose and `docker run` examples, or to build it from source instead.

## Installation via HACS

1. HACS → the "⋮" menu (top right) → **Custom repositories**.
2. Repository: `https://github.com/bdelima/ha-portainer-dashboard`, Category: **Integration**.
3. Find **Portainer Maintenance** in HACS → Integrations → **+ Explore & Download Repositories**, install it.
4. **Restart Home Assistant** — new `custom_components` are only loaded at startup.
5. Settings → Devices & Services → **Add Integration** → search "Portainer Maintenance" → enter your webapp's URL and pick the mobile_app device(s) that should get the "update performed" confirmation push.
6. Settings → Automations & Scenes → Create Automation → **Use Blueprint** → "Portainer Maintenance: automations (merged)" → pick your notify device(s) (can be the same ones as step 5, or different).

Already on an older version and don't see the notify-device field from step 5? Settings → Devices & Services → Portainer Maintenance → ⋮ → **Reconfigure** adds it to your existing setup without deleting and re-adding the integration.

## Manual installation (without HACS)

Copy `custom_components/portainer_maintenance/` into your Home Assistant config's `custom_components/` directory, then follow steps 4–6 above.

## License

MIT — see [LICENSE](LICENSE).
