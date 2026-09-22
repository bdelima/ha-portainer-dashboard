# Portainer + Home Assistant Container Management System

## Overview

A notification-driven system for managing container image updates and health across drakebay and ojochal, built on top of Home Assistant's core Portainer integration. Replaces Watchtower's silent auto-update model with a review-and-approve workflow, plus proactive alerting for container trouble and registry hygiene, surfaced through a standalone webapp rather than a Lovelace dashboard.

**Design goals:**
- Get notified when a container image update is available, with the choice to update immediately or postpone
- Get alerted (high priority) when a container is down, dead, or failing its healthcheck
- Get alerted when Portainer devices/entities in HA go stale (container/stack removed but HA didn't clean up), with a real way to delete them
- Stay dynamic — new containers should be picked up automatically without editing automations
- Handle the reality that some containers can't be safely recreated in isolation (see [Stack-restart-needed flow](#stack-restart-needed-flow-networkmodeservice-containers))

**A note on history:** this system has been through three distinct eras. A Lovelace-dashboard phase (auto-entities cards, double-tap multi-select via `input_text` helpers) came first and is fully superseded — see [portainer-ha-container-management-legacy.md](portainer-ha-container-management-legacy.md), nothing here depends on it. Next came a single-host, SSH-deploy-block era: a hand-maintained `templates.yaml`, standalone blueprints, and a webapp built with paste-and-run deploy scripts, everything living under `/opt/homeassistant/...` on ojochal with no version history beyond git commits to a private `homeassistant-config` repo. **This document now describes the current, third era**, current as of the integration's `1.1.4` release and the webapp's `1.1.1` release: both pieces are real, independently versioned, publicly-hosted projects with their own release history, replacing the deploy-block model entirely.

## Where the code actually lives

This document is deliberately not a mirror of the source — an earlier version of it reproduced entire files inline and quietly went stale as the real repos moved on without it (script-based `service.portainer_perform_update`, a `homeassistant-config` repo that no longer matches reality, a `manifest.json` version three major bumps behind). To avoid repeating that, this document covers **architecture, rationale, and current behavior** and defers to the repos themselves for anything that changes with every release:

- **[bdelima/ha-portainer-dashboard](https://github.com/bdelima/ha-portainer-dashboard)** — the HA custom integration (`custom_components/portainer_maintenance/`). HACS-installable (or manual copy), Python 1.1.x, GitHub Releases + an automatic `release-on-version-bump` workflow.
- **[bdelima/ha-portainer-sidecar](https://github.com/bdelima/ha-portainer-sidecar)** — the standalone webapp. Published as a multi-arch Docker image on [Docker Hub](https://hub.docker.com/r/bdelima/ha-portainer-sidecar), built and released automatically on every `VERSION` bump.

Both repos were renamed to their current names from `ha-portainer-maintenance` and `portainer-action-dashboard` respectively (a `gh repo rename`, which preserves history/issues/secrets and leaves a GitHub redirect from the old name). The integration's internal domain deliberately stayed `portainer_maintenance` — that's the "Portainer Maintenance" friendly name users actually see, so the domain still matches it even though the repo's own name changed.

## Setup steps

Everything that used to be a hand-run SSH deploy block is now either a HACS/git install or a Docker pull. In order:

1. **Install the webapp.** Pull `bdelima/ha-portainer-sidecar:latest` (or add it as a service to an existing compose stack — see the repo's own README for the compose/Docker-run examples). Give it an HA long-lived access token via `HA_TOKEN`/`HA_TOKEN_FILE`. `HA_BASE_URL` no longer needs to be set by hand in the common case — see [Webapp auto-discovery](#webapp-auto-discovery) — set it explicitly only if discovery doesn't find your HA instance, or you want to pin it.
2. **Install the integration.** Either via HACS (add `bdelima/ha-portainer-dashboard` as a custom repository) or by copying `custom_components/portainer_maintenance/` into HA's config dir by hand. **Restart HA** (a real restart — `custom_components` code only loads at startup), then Settings → Devices & Services → **Add Integration** → "Portainer Maintenance" → enter the webapp's own URL, and pick the mobile_app device(s) that should get pushes. This one step installs the bundled blueprint, registers the sidebar panel, computes the notification click-through URL, and creates the tracking sensors. A one-time `persistent_notification` appears right after setup finishes, linking straight to the automations dashboard so the next step isn't a hunt through Settings.
3. Settings → Automations & Scenes → Create Automation → **Use Blueprint** → "Portainer Maintenance: automations (merged)" → nothing else to configure; it reads its notify target from the config entry, not a blueprint input (see [why](#automations) below).

No per-new-container manual step remains: hiding a new container's `update.*` entity — previously a "remember to do this every time" chore — is now handled automatically (see `hide_update_entities` below, staged for the next release).

An existing install picks up a `notify_devices` field it didn't originally have via Settings → Devices & Services → Portainer Maintenance → **Reconfigure**, without deleting and re-adding the integration.

**Migrating from the pre-repo era** (a hand-maintained `templates.yaml`, standalone blueprints under `blueprints/*/portainer/`, or the old `portainer_cleanup` integration): remove the old `templates.yaml` include *before* restarting with the new integration installed — otherwise the native sensors collide with the old template ones and land on a `_2`-suffixed entity_id. Don't delete old automations/scripts until the new ones are verified.

Before trusting this with a real pending update or a real stale device, run through the [Verification checklist](#verification-checklist).

## Architecture

### Data source
Home Assistant's **core Portainer integration** (not the HACS `tomaae` version), pointed at one or more Portainer endpoints (drakebay, ojochal). Built on the `pyportainer` library.

### Device hierarchy
Portainer devices in HA nest as: **Endpoint** (host) → **Stack** → **Container**, via `via_device_id`. A standalone container not in a Compose stack links directly to its endpoint instead of through an intermediate Stack device — confirmed directly in HA's device list, not assumed. Distinguishing the two: a real Stack device has its own `via_device_id` pointing further up to the Endpoint, while the Endpoint itself has none — so if a container's immediate parent has no further parent, that parent *is* the Endpoint and the container is standalone. This exact check (`_stack_info` in `sensor.py`) is what both the dashboard's stack-grouped tree and the stack-restart-needed flow below are built on.

### Per-container entities (naming pattern: `<domain>.<container_name>_<sensor>`)
- `binary_sensor.<name>_status` — running/not
- `sensor.<name>_state` — running / exited / paused / dead
- `sensor.<name>_health` — healthy / unhealthy / starting (**only exists if the container defines a Docker `HEALTHCHECK`** — otherwise this entity is absent entirely, not just empty)
- `sensor.<name>_image` — current image reference
- `sensor.<name>_cpu_usage_total`, `_memory_usage`, `_memory_usage_percentage`, `_memory_limit`
- `update.<name>_update` — binary update-available flag, digest-comparison based (see the important caveat in [Just-updated suppression](#just-updated-suppression-working-around-a-core-caching-bug))
- `switch.<stack name>` — one per Compose stack, start/stop every container in it (used by `restart_stack`, below)
- `button.<name>_recreate`, `_restart`, `_pause`, `_resume`, `_kill` — action buttons (mostly superseded here by the native services)

### Dynamic entity scoping
Rather than hardcoding entity lists, scoping is done by **config entry domain**, computed fresh each run — the same idea whether expressed in Jinja (blueprint) or Python (`_portainer_entity_ids` in `sensor.py`):

```jinja
{% set ns = namespace(ids=[]) %}
{% for e in states | map(attribute='entity_id') %}
  {% set eid = config_entry_id(e) %}
  {% if eid and config_entry_attr(eid, 'domain') == 'portainer' %}
    {% set ns.ids = ns.ids + [e] %}
  {% endif %}
{% endfor %}
{{ ns.ids }}
```

`integration_entities('portainer')` was tried first and worked in most cases, but was found to silently omit at least one legitimate entity in testing (cause unconfirmed — possibly config subentries or registry drift from a prior device deletion). The manual scan above is the reliable fallback used in the tracking sensors and `prune_images`'s host discovery.

## The Portainer Maintenance integration

What started as a single-service shim (`portainer_maintenance.remove_device`, because HA has no documented way to delete a device from an automation/script at all — the Settings → Devices page's Delete button calls an internal frontend WebSocket command, not a service) has grown into the thing that actually ties this whole design together. Config-entry based (`config_flow: true`), single instance, two fields: the webapp's URL and the notify device(s).

On every load, it:

1. Registers five native services (table below).
2. Installs its one bundled automation blueprint into HA's `blueprints/` dir (re-copied on every load — treat the deployed copy as generated, never hand-edit it).
3. Registers an iframe sidebar panel at a fixed path (`portainer-actions`) pointing at the webapp — no more "Add Dashboard → Webpage → read the random URL back out of the address bar."
4. Computes the notification click-through path as a bare relative path (`/portainer-actions`, not a full URL) and exposes it as `sensor.portainer_actions_url`. Being relative means the HA companion app treats it as "navigate within the server I'm already connected to," so it works with zero dependency on `external_url`/`internal_url` being configured at all.
5. Forwards to the sensor platform, which defines the three tracking sensors as native `DataUpdateCoordinator`-backed entities.
6. On first-ever setup only (not reconfigure, not a restart), posts a one-time `persistent_notification` linking to `/config/automations/dashboard` — the closest available nudge toward creating the automation, since no supported mechanism exists for a config flow to deep-link HA's frontend straight into "create an automation from this blueprint" (checked: no query param the automation editor reads, no matching `my.home-assistant.io` redirect type).

### Native services

| Service | Purpose |
|---|---|
| `portainer_maintenance.remove_device` | Deletes a device from the registry outright — no undo. For stale Portainer devices Portainer's own HA integration doesn't clean up on its own ([home-assistant/core#155548](https://github.com/home-assistant/core/issues/155548), confirmed open). |
| `portainer_maintenance.prune_images` | Wraps core's `portainer.prune_images`, called once per discovered endpoint (host), so a newly-added host needs no extra configuration. `dangling`-only is always safe; the `until_hours` floor protects a stack you stop and quickly restart from losing its cached image to a prune run in between. |
| `portainer_maintenance.perform_update` | The real entry point for installing an update — see below and [Stack-restart-needed flow](#stack-restart-needed-flow-networkmodeservice-containers). Returns `{needs_stack_restart, stack_switch_entity_id}` via HA's action-response-data feature (`supports_response=OPTIONAL`), so a caller that isn't just a phone tapping a push notification — the webapp — can react synchronously instead of only through the async push. |
| `portainer_maintenance.update_done` | Shared "update complete" notification logic, exposed as its own callable service for anything that performs an update through some other path and just wants this system's tracking/notification state to reflect it. |
| `portainer_maintenance.restart_stack` | Stop → 5s settle → start of a whole stack via its `switch.*` entity. Deliberately never called automatically — see below. |
| `portainer_maintenance.hide_update_entities` | Scans every Portainer `update.*` entity and hides any not already hidden (staged for the next release — not yet in a tagged version). Closes the "hide its `update.*` entity" manual per-new-container step; see [Automations](#automations) branches 5 and 8 for when it's called. Re-hides an entity a user manually un-hid, by design — this system's whole point is that these entities' state belongs on the dashboard, not HA's own entity list. |

`perform_update`/`update_done` used to be blueprint *scripts*, each requiring the user to create a script instance from the blueprint and then manually override its auto-generated Entity ID to the exact literal string the automation blueprint called by name — an easy step to miss, and when missed, HA reported it as an opaque "automation uses an unknown action" repair with no obvious link back to the real cause. Native services have no user-assigned entity_id to get wrong in the first place; both read who to notify from the config entry's `notify_devices` instead of a blueprint input, since a plain service call has no blueprint inputs to read from.

### `perform_update`, in detail

1. Resolves the target container's owning stack (if any) up front via the same `_stack_info` helper the tree UI uses.
2. Calls core's `portainer.recreate_container` (`container_device_id`, `pull_image: true`), blocking.
3. On success: posts the normal "update performed" notification (persistent + phone push) and tells the updates-pending coordinator this entity is confirmed done — see [Just-updated suppression](#just-updated-suppression-working-around-a-core-caching-bug).
4. On failure: only treated as the known [network_mode:service conflict](#stack-restart-needed-flow-networkmodeservice-containers) if **both** the container is part of a stack **and** the exception text matches Docker's own daemon message (`"conflicting options"` + `"network mode"`, case-insensitive). Any other failure — including that same text for a standalone container, which the known bug doesn't apply to — is re-raised and fails the update normally, exactly as it did before this feature existed. This double gate exists because an earlier version swallowed *every* exception unconditionally, which meant a real failure for a standalone container silently reported success — caught and fixed before release, not in production.

## Automations

**One blueprint, one automation entity** (`Portainer Maintenance: automations (merged)`), not one per behavior — closes a real gap the original per-behavior design had: the phone notify target used to be hardcoded in five separate places with no single spot to change it. A shared `variables:` step (from the config entry's `notify_devices`) computes the notify service list once; a `choose:` block dispatches by `trigger.id`.

**Tradeoff accepted by merging:** one enable/disable toggle and one shared trace history for all nine branches below, rather than independent per-behavior control. Revisit if that granularity turns out to matter in practice.

The current branches:

1. **Notify on update available** — phone push with **Update now** / **Dismiss** actions, tag'd by entity_id so a later dismiss/clear targets the right notification.
2. **Handle "Update now"** — calls `portainer_maintenance.perform_update` with the tapped entity.
3. **Handle "Dismiss"** — clears the phone notification only; never touches the underlying `update.*` entity, so the item stays visible/installable from the webapp regardless.
4. **Startup recheck** — catches updates whose original notification was lost (e.g. the Companion App restarted before a tap). Waits for the Portainer coordinator's first poll, then a 30s settle delay (this branch and the tracking sensors both trigger on `homeassistant: start` with no guaranteed order between them), then sends one aggregate push if anything's pending.
5. **Container trouble** — high-priority push (bypasses silent/DND via `alarm_stream`) when `sensor.portainer_container_trouble`'s count *increases*. Known accepted gap: `restart: unless-stopped` containers that crash-loop rarely spend meaningful time in `exited`, since Docker restarts them almost immediately — the tracking sensor's settle-time check can miss this. No fix implemented (would need a separate `command_line` sensor tracking Docker's `RestartCount`).
6. **Stale devices** — push when `sensor.portainer_stale_devices`'s count increases. Deletion happens from the webapp's Stale tab (`portainer_maintenance.remove_device`), not from this branch.
7. **Sidebar-bell restart-stack action** — when a phone push's "Restart Stack Now" action fires, calls `portainer_maintenance.restart_stack` with the entity_id encoded in the action string (`RESTART_STACK_<switch_entity_id>`).
8. **Hide a newly-registered update entity** *(staged for the next release)* — fires on `entity_registry_updated`, filtered by the same dynamic config-entry-domain scoping used everywhere else in this design, to just a newly-created `update.*` entity under the `portainer` platform. Calls `hide_update_entities` the moment such an entity appears, rather than waiting for the next HA restart. Branch 4 (startup recheck) gained a matching one-time sweep for anything that predates this feature.
9. **Sidebar-bell action-items summary** — recomputed after *every* branch above fires, off the three sensors' current values (not deltas), so it's always correct regardless of which branch ran. Builds a natural-language sentence from only the nonzero categories and links straight to the panel:

   ```
   Portainer is reporting 2 image updates and 1 stale device. Open dashboard to review.
   ```

   Two items join with "and"; three or more get an Oxford comma; a category at zero is omitted entirely rather than padding the sentence with "0 containers in trouble." This replaced an earlier version that always listed all three counts including zeros, and separately didn't link anywhere at all — both fixed after review of an actual on-device screenshot, not from a spec.

**Trigger mechanics carried over from the original design:** HA's `state` trigger doesn't support a templated `entity_id`, so the update-available branch triggers on the generic `state_changed` event and filters with a template condition instead. The `portainer.recreate_container` action does not support a `target:` block, and does not use `device_id` as its data field name despite that being what the online docs show at various points — the correct field, confirmed via Developer Tools → Actions → YAML mode, is `container_device_id`.

## Tracking sensors

Three native `DataUpdateCoordinator`-backed sensors, defined in `sensor.py`, replacing what used to be trigger-based `template:` sensors in a hand-maintained `templates.yaml` (no Helpers UI support at all for a multi-trigger template sensor with a shared `variables:` block — YAML only). Entity_ids are pinned explicitly to what `templates.yaml` used to produce, so nothing downstream needed to change when they moved.

- **`sensor.portainer_updates_pending`** (5-minute poll, plus an immediate forced refresh right after `perform_update` confirms one) — every `update.*` entity currently `on`, each tagged with `stack_name`/`stack_switch_entity_id` (or both `null` for a standalone container) so the webapp can group them into a tree. See [Just-updated suppression](#just-updated-suppression-working-around-a-core-caching-bug) for the filtering logic layered on top of the raw "on" check.
- **`sensor.portainer_container_trouble`** (1-minute poll) — any `_state` entity reading `exited`/`dead`, or `_health` reading `unhealthy`, gated by a 120-second settle window checked against `last_changed` directly (no separate timer helper). A container that's both exited *and* unhealthy produces two rows, not a de-duplicated one.
- **`sensor.portainer_stale_devices`** (1-hour poll) — devices whose entities have all gone `unavailable` for 12+ hours while the parent endpoint stays healthy. Uses each entity's `last_changed` directly (resets automatically on any flap, so a flapping container never accumulates dwell time), taking the *minimum* age across a device's entities. Ported from the original Jinja with one genuine bug fix found in the process: the original had no guard against a root Endpoint device itself qualifying as "stale" when its own entities went unavailable (i.e. the whole host down, not a removed container) — a device with no `via_device_id` is a root Endpoint and can never be a stale *child* candidate; an explicit guard was added. Caught by a standalone test harness, not by production behavior.
- **`sensor.portainer_actions_url`** (no coordinator, computed once at setup) — the read-only relative click-through path described above.

Each list sensor's state is the item count, with the full list on the `items` attribute.

## Stack-restart-needed flow (`network_mode:service` containers)

**Confirmed, unfixed, open Portainer/Docker daemon bug** (not this system's bug): recreating a single container whose `network_mode` is `service:<other>` or `container:<other>` — the pattern a VPN sidecar like gluetun uses — makes Docker's daemon reject the create call ("conflicting options: hostname and the network mode"), because Portainer re-applies the previous container's hostname even though Docker forbids a hostname when a container shares another container's network namespace. Portainer's own maintainers' documented workaround is to update the whole stack, never a single container ([github.com/orgs/portainer/discussions/12389](https://github.com/orgs/portainer/discussions/12389)). Independently reproduced by hand through Portainer's own UI, with no HA or this integration involved at all — settling, before any of this was built, that it's a real Portainer/Docker-level behavior and not an artifact of HA's own caching.

The pull+recreate genuinely completes despite the daemon error; only the image tag's final reconciliation needs the stack restarted afterward.

**Design constraint that shaped the whole flow:** a full stack restart bounces *every* container in that stack, not just the one that needed it — core's own stack `switch.*` entity is a stop/start of the whole thing, there's no narrower "just fix this one container's network" action. So this never happens automatically. `perform_update` swallows the specific daemon-conflict error (see the double gate in [`perform_update`, in detail](#perform_update-in-detail) above) and instead offers a choice: a phone push with **Open Dashboard** / **Restart Stack Now** actions, or — for whoever's already looking at the webapp when the install finishes — a warning banner above the tabs with the same "Restart Stack Now" button, driven by `perform_update`'s synchronous `needs_stack_restart` response rather than waiting on the async push. Either path calls `portainer_maintenance.restart_stack` explicitly; nothing restarts on its own.

**Status as of this writing: shipped (1.1.0 for the detection/service, 1.1.1 for the webapp banner), but not yet exercised against a real occurrence.** No `network_mode:service` container has had a live image update land since this shipped, so the exception-text match itself — the one piece that couldn't be verified without a live daemon error — remains unconfirmed in production. If it turns out HA doesn't propagate Docker's literal daemon text into the exception (wrapped, translated, or truncated somewhere in the call chain), the safe failure direction is that the update just fails outright like it always did, with no stack-restart offer — not a silent false success. Watch the integration log for `%s.perform_update: recreate_container failed ...` on the first real occurrence; it logs both whether a stack was found and whether the text matched, regardless of which branch it took.

## Just-updated suppression (working around a core caching bug)

**Confirmed, open, unmerged Home Assistant core bug** ([home-assistant/core#182584](https://github.com/home-assistant/core/issues/182584); two competing unmerged fixes as of writing, #182648 and #182654): after a container is recreated, it gets a new Docker container ID, but core's `PortainerImageWatcher` cache stays keyed to the *old* ID and doesn't refresh until its own next full rescan — which can be as long as 24 hours. The practical effect: the `update.*` entity can keep reporting "update available" for hours after an update that actually succeeded, with nothing in HA's own state making that distinction visible.

This can't be fixed from here — it's core's own internal cache, not something this system's code touches. The workaround: the moment `perform_update` confirms a recreate actually went through (cleanly, or via the swallowed daemon-conflict case above — both mean the pull+recreate happened), it tells `PortainerUpdatesCoordinator` directly (`mark_recently_updated`), which then hides that one entity from the pending list for **25 hours** — chosen to clear the full known 24h worst case rather than stop short of it, since an early expiry just means the blueprint treats the stale "on" reappearing as a *newly appeared* update and sends a fresh, wrong push about it, which is worse than doing nothing. A cost accepted along with this: a genuinely new update landing for the same container within that 25-hour window would also be hidden, which in practice essentially never happens for homelab-cadence image releases.

The suppression clears early, before the 25h window elapses, the moment the entity is actually observed as `off` — a state that's always trustworthy on its own, since the underlying bug only ever over-reports "on," never wrongly clears itself. It deliberately does **not** clear on `unavailable`, since that state can appear transiently while a container is mid-recreate, or from an unrelated core-integration polling hiccup — treating it the same as `off` was an early version of this fix, caught in review before shipping, that would have let exactly the flicker this feature exists to smooth over defeat it.

This is in-memory only (an attribute on the coordinator instance), so an HA restart within the suppression window loses it and the item can briefly reappear. Not incorrect, just the one remaining limitation — not worth persisting for what's fundamentally papering over an upstream bug rather than fixing anything of this system's own.

**Status: confirmed working in production.** Tested against a real container update (not synthetic) — the dashboard/sidebar stopped showing the item as pending immediately after the recreate, instead of sitting on the stale `on` state.

## Changelog links *(staged for the next release)*

The same core update-entity limitation shows up again here: `installed_version`/`latest_version` are raw digest hashes, and neither `release_summary` nor `release_url` is ever populated — there's no changelog data anywhere in this system to just surface. Three ways to get one anyway were considered:

- **Read the image's own OCI labels** (`org.opencontainers.image.source`) via Portainer's API. Technically clean — Portainer proxies Docker's image-inspect endpoint verbatim, so the label data is there for the taking with no registry-auth complexity. Rejected: it needs this integration to hold its own separate Portainer API credentials (a new setup field, on top of everything core's own `portainer` integration already asks for), and coverage would still be inconsistent — confirmed present on some GHCR/GitHub-Actions-built images (gluetun sets it), confirmed **absent** on linuxserver.io images, which is a large share of a typical homelab and set only `build_version`/`maintainer`, no OCI annotations at all.
- **Scrape a search engine's results for the image tag.** Rejected outright, not just deprioritized: there's no free, sanctioned API for it (Google's Custom Search API needs its own paid-tier credential — reintroducing the exact setup cost the other two options avoid), and scraping the results page's HTML directly is against Google's terms of service and gets automated traffic from a single home IP rate-limited or CAPTCHA'd in practice — not something to build a background HA process around.
- **Read the same two places a search engine's result actually comes from, directly.** This is what shipped, as automatic discovery layered under the original hand-curated table:
  - **ghcr.io images:** the image path already *is* a GitHub owner/repo path (`ghcr.io/qdm12/gluetun` → `github.com/qdm12/gluetun`), so the URL is a direct guess.
  - **Docker Hub images** (this is what covers LSIO): Docker Hub's own public repository API (`hub.docker.com/v2/repositories/<namespace>/<repo>/`, no auth needed) returns the README text, which conventionally links back to the upstream GitHub repo; the first plausible `github.com/<owner>/<repo>` match is extracted from it. `lscr.io` (linuxserver.io's own pull-through mirror, identical paths to their real Docker Hub images) is treated the same as Docker Hub for this lookup.
  - Either way, the guessed/scraped URL is never trusted blindly — it's confirmed with a live HTTPS request first, and only used if that resolves. A result (found or not) is cached per normalized image repo for the life of the integration, so the network round trip only happens once per distinct image ever seen with a pending update, not on every 5-minute poll.

The hand-curated table (`_KNOWN_CHANGELOG_URLS` in `sensor.py`) didn't go away — it's now checked first, as an override that always wins over discovery. That matters for exactly the cases discovery can't get right on its own: Home Assistant's own Docker Hub path (`homeassistant/home-assistant`) doesn't match its real repo (`home-assistant/core`), so it stays a manual entry; a private/self-hosted registry has no generic, credential-free way to be read at all, so it silently gets no link unless one is added by hand.

**Seeded with:** Home Assistant core (`homeassistant/home-assistant`), gluetun (`qmcgaw/gluetun`), and Portainer itself (`portainer/portainer-ce`) — all three would very likely resolve correctly through discovery too, but are pinned here anyway as a guarantee. **Deliberately excluded, and undiscoverable by any of this:** Plex — Plex Media Server is closed-source, so there's no public GitHub releases page for it at all, an inherent limit of the upstream project rather than a gap in this table or in discovery.

Surfaced as a `changelog_url` field on each pending-update item (alongside `stack_name`/`stack_switch_entity_id`), rendered by the webapp as a small "Changelog" link next to Install whenever present, opening in a new tab.

**New operational dependency to note:** this adds outbound calls from HA itself to `hub.docker.com` and `github.com` (only for images not already in the table, and only once per image thanks to the cache) — a normal homelab with unrestricted outbound internet needs nothing extra, but a locked-down egress policy would silently just get no discovered links, same as any other unmapped image.

## Webapp (`ha-portainer-sidecar`)

A small standalone web app, embedded in HA as a `type: iframe` sidebar panel, giving a genuine multi-select data table instead of Lovelace's declarative card limits. Two files: a FastAPI backend holding the HA long-lived access token server-side (never sent to the browser), and a static HTML/CSS/JS frontend it serves directly — one container, no build step, no frontend framework.

**Why this can't be a claude.ai Artifact:** a published Artifact runs under a content-security policy blocking fetch/XHR/WebSocket to arbitrary hosts (only specific CDNs allowlisted, scripts/styles only) — it could never call this HA instance directly. Has to be a real, self-hosted container.

### Distribution
Published as a multi-arch (amd64/arm64) image on Docker Hub, `bdelima/ha-portainer-sidecar`, tagged per release plus `latest`, built and pushed automatically by the repo's own GitHub Actions workflow whenever `VERSION` bumps on `main`. Building from source (`build: .`) still works.

### Webapp auto-discovery
`HA_BASE_URL` is optional. If unset, the app finds HA on startup, unauthenticated (no token sent during discovery, fingerprinting `/manifest.json`'s `"name": "Home Assistant"`), in order: common container hostnames (`homeassistant`, `home-assistant`, `hass`, `ha`) via Docker's embedded DNS; a scan of its own `/24` subnet; then the Docker host's default gateway and `host.docker.internal`. Fails to start with a clear error if none of these find anything — set `HA_BASE_URL` explicitly in that case. A separate optional `HA_PUBLIC_URL` exists purely for browser-facing "open in HA" links, since an auto-discovered address (a container name, an internal Docker IP) is meaningless to a phone or laptop browser; it's reused automatically from `HA_BASE_URL` whenever that was set explicitly (not auto-discovered) to something already browser-reachable.

### Stack-grouped tree (Updates tab)
Pending updates group under a collapsible header per Portainer stack, with a cascading checkbox (select the stack, or individual containers within it). Falls back to a flat list only when there's nothing to group — specifically, when every pending item is standalone; a single *named* stack still gets its own header shown even with just one item in it, so the grouping is visible the moment there's anything to show, not only once two-plus stacks have pending updates at the same time. Selecting a stack's checkbox is purely a bulk-select convenience — installing still calls `perform_update` once per individual container entity underneath it; there is no stack-level "update" action anywhere in this system, only the explicit, always-opt-in `restart_stack`.

### Endpoints
- `GET /api/config` — HA base URL (non-sensitive; used to build "open in HA" links)
- `GET /api/action-items` — reads all three tracking sensors, returns `{updates: {count, items}, trouble: {...}, stale: {...}}`, `items` carrying whatever fields each sensor attaches (including `stack_name`/`stack_switch_entity_id` on updates)
- `POST /api/actions/install` — one `perform_update` call per selected `update.*` entity; aggregates any `needs_stack_restart` results into the response for the banner described above
- `POST /api/actions/restart-stack` — calls `portainer_maintenance.restart_stack`; validates `switch_entity_id` starts with `switch.` (a sanity check against a stray value, not a security boundary — this app has no auth of its own, same as every other endpoint)
- `POST /api/actions/delete-stale` — one `remove_device` call per selected stale device
- `POST /api/actions/prune-images` — calls `portainer_maintenance.prune_images`
- Frontend polls `/api/action-items` every 15s

### Auth
A long-lived HA access token, read from `HA_TOKEN_FILE` (preferred — keeps it out of `docker inspect`/compose-file output) or plain `HA_TOKEN`. Never sent to the browser. **This app should not be exposed publicly** — no auth layer of its own beyond whatever reverse proxy sits in front of it; anyone who can reach it can install updates and delete HA devices.

## Operational notes

- **New containers require one manual step:** hide their `update.*` entity to avoid badge clutter — confirmed zero effect on automation triggering.
- **Portainer's HA integration auto-assigns Area = stack/host name** to every device it creates. Cosmetic, no automation here uses Area.
- **Watchtower has been retired** in favor of this review-and-approve flow. Recommend removing the Watchtower stack/container entirely (it held broad Docker socket access) rather than leaving it stopped.
- **Known accepted limitation — multi-arch manifest-list images:** some images (confirmed case: `ghcr.io/bakito/adguardhome-sync`, all current tags) publish only multi-architecture manifest lists with no clean single-platform tag. Update detection for these can get stuck, likely a digest-comparison mismatch between the manifest-list digest and the platform-specific digest actually running. No fix implemented — Portainer's own UI remains the fallback source of truth for these specific images.

## Verification checklist

- [x] `perform_update`'s success path actually installs (not just opens more-info) — confirmed against real containers.
- [x] Just-updated suppression — confirmed against a real update; item stopped showing as pending immediately, not after the core cache's usual delay.
- [x] Sidebar-bell notification — confirmed readable and correctly linked after the wording rewrite.
- [ ] **Stack-restart-needed detection for a real `network_mode:service` container — not yet exercised.** The single highest-value thing left to verify; requires a live image update landing on one of the gluetun-attached containers. Watch the integration log's `perform_update: recreate_container failed` line for whether the exception text actually matched.
- [ ] `portainer_maintenance.restart_stack` against a real stack, end to end (stop → settle → start actually brings every container back).
- [ ] `portainer_maintenance.remove_device` against a real, already-confirmed-stale device.
- [ ] Batch-select + confirm flow in the webapp for Updates, Trouble, and Stale together (each has been touched individually; not stress-tested together).
- [ ] The stale-devices whole-host-down guard (a host outage should not make the Endpoint device itself appear in `sensor.portainer_stale_devices`) — logic fixed and unit-tested standalone, not yet confirmed against a real host outage.
- [ ] iframe rendering at phone width in the Companion App specifically, post stack-tree UI changes.

## Version history

**Integration (`ha-portainer-dashboard`):**
| Version | Highlights |
|---|---|
| 1.1.0 | Stack-restart-needed detection (`restart_stack` service, `perform_update`'s swallow-and-notify logic), stack-aware sensor data, post-setup automation nudge |
| 1.1.1 | Sidebar-bell notification: added a working link to the panel |
| 1.1.2 | Sidebar-bell notification: rewritten as a natural sentence, zero-count categories omitted |
| 1.1.3 | Just-updated suppression (first pass) |
| 1.1.4 | Just-updated suppression fixes: `unavailable` no longer clears it early, grace window extended to 25h |

**Webapp (`ha-portainer-sidecar`):**
| Version | Highlights |
|---|---|
| 1.1.0 | Renamed from `portainer-action-dashboard`; service-name fix |
| 1.1.1 | Auto-discovery of `HA_BASE_URL`/`HA_PUBLIC_URL`, stack-grouped tree UI, stack-restart-needed banner |

**Staged locally, held back pending more real-world testing before the next release:**
- Integration: `hide_update_entities` service plus blueprint branches 4/8, closing the "hide its update.* entity" manual setup step. Also: `changelog_url` on the updates-pending sensor's items, backed by the `_KNOWN_CHANGELOG_URLS` override table plus automatic ghcr.io/Docker Hub discovery — see [Changelog links](#changelog-links-staged-for-the-next-release).
- Webapp: a page-heading tweak, "Container Actions" → "Items Needing Attention." Also: the "Changelog" link next to Install, when `changelog_url` is present.

## Testing methodology used

Real update-available states were forced deterministically by building a disposable test image and pushing two versions under the same tag to a personal Docker Hub repo (`bdelima/test-discovery`), using an isolated `docker buildx` builder (`--driver docker-container`) to avoid corrupting the local image cache/tag on the host running the test container — `docker build` and the default-driver `buildx build --push` both clobber the local `:latest` tag on whichever host runs them, which produces the exact "dangling tag / SHA hash" symptom this whole system exists to detect, and was a real gotcha hit early on.

The `network_mode:service` daemon-conflict bug was confirmed by hand, twice independently: once through Portainer's own UI with no HA or this integration involved at all (settling that it's a real Portainer/Docker-level bug, not a caching artifact of anything built here), and once via this system's own `perform_update` flow reproducing the identical error. The just-updated suppression fix was confirmed against a real, non-synthetic container update in production. The stack-restart-needed *service call itself* has not yet had that same live confirmation — see the [Verification checklist](#verification-checklist).
