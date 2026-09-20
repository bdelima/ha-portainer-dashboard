# Portainer + Home Assistant Container Management System

## Overview

A notification-driven system for managing container image updates and health across drakebay and ojochal, built on top of Home Assistant's core Portainer integration. Replaces Watchtower's silent auto-update model with a review-and-approve workflow, plus proactive alerting for container trouble and registry hygiene, surfaced through a standalone webapp rather than a Lovelace dashboard.

**Design goals:**
- Get notified when a container image update is available, with the choice to update immediately or postpone
- Get alerted (high priority) when a container is down, dead, or failing its healthcheck
- Get alerted when Portainer devices/entities in HA go stale (container/stack removed but HA didn't clean up), with a real way to delete them
- Stay dynamic — new containers should be picked up automatically without editing automations

**A note on history:** this design went through a Lovelace-dashboard phase (auto-entities cards, double-tap multi-select via `input_text` helpers) before settling on the standalone webapp described here. That earlier approach is fully superseded and has been split out to [portainer-ha-container-management-legacy.md](portainer-ha-container-management-legacy.md) for reference — nothing in this document depends on it, and it doesn't need to be deployed.

## Setup steps

**Revised 2026-09-20 — collapsed from 9 steps to 5.** Almost everything that used to be a separate deploy block (tracking sensors, blueprints, the dashboard) is now handled by installing one integration. Everything below assumes HA's core Portainer integration is already configured against both endpoints (drakebay, ojochal). In order:

1. Run the deploy block to lay the [webapp](#webpage-dashboard) files down at `/opt/homeassistant/portainer-action-dashboard`, add its [service block](#deployment) to the existing `homeassistant` stack in Portainer, redeploy that stack, then set up an NPM proxy host for it (scoped/restricted — see [Auth](#auth-long-lived-access-token-held-server-side-only) for why this shouldn't be public).
2. Run the deploy block to install the [Portainer Maintenance integration](#portainer-maintenance-integration), **restart HA** (a real restart — new `custom_components` code only loads at startup), then Settings → Devices & Services → **Add Integration** → "Portainer Maintenance" → enter the webapp's own URL (e.g. `https://actions.o.pumapants.cc`). This one step now also: installs both blueprints, registers the sidebar panel, computes the notification click-through URL automatically, and creates the three tracking sensors. Nothing else to deploy separately.
3. Settings → Automations & Scenes → Create Automation → **Use Blueprint** → "Portainer Maintenance: automations (merged)" → pick your notify device.
4. Settings → Automations & Scenes → Scripts → Add Script → **Use Blueprint** → both "Portainer Maintenance: ..." script blueprints → pick your notify device for each, then open each one's settings (cog icon) and set its Entity ID explicitly to `portainer_perform_update` / `portainer_update_done` — see [Portainer Maintenance integration](#portainer-maintenance-integration) for why this one manual step is still necessary even from a blueprint.
5. Ongoing, per new container: hide its `update.*` entity (see [Operational notes](#operational-notes)).

Steps 1 and 2 each have a matching `deploy-*.txt` paste block in this project folder (under `webapp/` for step 1, `ha-config-deploy/` for step 2) — paste the whole block into an SSH session on the host running the `homeassistant` stack (currently ojochal) and it writes the files then runs itself with `sudo`. HA's config directory is `/opt/homeassistant/core/config`.

**Migrating from the pre-2026-09-20 design** (separate `portainer_cleanup` integration + `templates.yaml` + standalone blueprints under `blueprints/*/portainer/`): the new integration's deploy block prints the exact removal order in its own output — old integration, old `templates.yaml` include, old scripts.yaml entries — and it matters (removing `templates.yaml` *before* restarting is the one that isn't optional: otherwise the new native sensors collide with the old template ones and land on a `_2`-suffixed entity_id instead of the real one). Don't delete the old six individual automations or old scripts.yaml scripts until the new ones are verified working — see the [Verification checklist](#verification-checklist).

Before trusting any of this with a real pending update or a real stale device, run through the [Verification checklist](#verification-checklist) — several pieces here are new and haven't been exercised against production containers yet.

## Architecture

### Data source
Home Assistant's **core Portainer integration** (not the HACS `tomaae` version), pointed at one or more Portainer endpoints (drakebay, ojochal). Built on the `pyportainer` library.

### Device hierarchy
Portainer devices in HA nest as: **Endpoint** (host) → **Stack** → **Container**. A standalone container not in a stack links directly to its endpoint via `via_device_id`.

### Per-container entities (naming pattern: `<domain>.<container_name>_<sensor>`)
- `binary_sensor.<name>_status` — running/not
- `sensor.<name>_state` — running / exited / paused / dead
- `sensor.<name>_health` — healthy / unhealthy / starting (**only exists if the container defines a Docker `HEALTHCHECK`** — otherwise this entity is absent entirely, not just empty)
- `sensor.<name>_image` — current image reference
- `sensor.<name>_cpu_usage_total`, `_memory_usage`, `_memory_usage_percentage`, `_memory_limit`
- `update.<name>_update` (or similarly named) — binary update-available flag, digest-comparison based
- `button.<name>_recreate`, `_restart`, `_pause`, `_resume`, `_kill` — action buttons

### Dynamic entity scoping
Rather than hardcoding entity lists (which would require editing automations every time a container is added/removed), scoping is done by **config entry domain**, computed fresh each run:

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

**Note:** the built-in `integration_entities('portainer')` helper was tried first and worked in most cases, but was found to silently omit at least one legitimate entity in testing (cause unconfirmed — possibly related to config subentries or registry drift from a prior device deletion). The manual scan above is the reliable fallback and is what's used in the tracking sensors. Other automations still use `integration_entities()` for simplicity where the gap hasn't caused a problem in practice.

## Automations

**Revised 2026-09-20 — merged into one automation, deployed as one blueprint, blueprint now installed automatically by the integration.** All six behaviors below now live in a single automation entity, created from a single blueprint (`Portainer Maintenance: automations (merged)`) via Settings → Automations & Scenes → Create Automation → Use Blueprint — not six separate automations, and not pasted YAML. The blueprint file itself is laid down automatically by the [Portainer Maintenance integration](#portainer-maintenance-integration) on every load; there's no separate deploy step for it anymore. The blueprint defines five triggers, each tagged with an `id`, and a `choose:` block that dispatches to the matching behavior below based on `trigger.id` (plus, where applicable, the same template condition each behavior always had). See [Blueprints](#blueprints) for the full merged YAML, why this changed (`notify.mobile_app_bob_s_phone` was hardcoded in five of these plus both scripts, with no single place to change it), and — importantly — the tradeoff merging six automations into one accepts (no independent enable/disable or separate trace history per behavior anymore).

Each numbered item below is one `choose:` branch of that single automation, not a standalone automation entity. The `notify_device`/`notify_service` variables are computed once, up top, shared by every branch — shown here per-branch only where it clarifies which service ends up called.

### 1. Notify on container update available
Fires when any Portainer `update.*` entity transitions to `on`. Sends a phone push notification with two actions: **Update now** / **Dismiss**.

Includes the host name in the notification, via a `via_device_id`-walk-to-root lookup (same trick used everywhere else in this design that needs to say which host a container is on).

**Trigger id:** `update_available` (on the shared `event_type: state_changed` trigger — see [Blueprints](#blueprints) for why this has to be a generic event trigger, not `state`).

```yaml
conditions:
  - condition: trigger
    id: update_available
  - condition: template
    value_template: >
      {{ trigger.event.data.entity_id in (integration_entities('portainer') | select('match', 'update\\.') | list)
         and trigger.event.data.new_state.state == 'on'
         and trigger.event.data.old_state.state != 'on' }}
sequence:
  - variables:
      update_entity: "{{ trigger.event.data.entity_id }}"
      host_name: >
        {% set walk = namespace(current=device_id(update_entity)) %}
        {% for _ in range(4) %}
          {% set parent = device_attr(walk.current, 'via_device_id') %}
          {% if parent %}
            {% set walk.current = parent %}
          {% endif %}
        {% endfor %}
        {{ device_attr(walk.current, 'name') }}
  - action: "{{ notify_service }}"
    data:
      title: "Update available"
      message: "{{ trigger.event.data.new_state.attributes.friendly_name }} on {{ host_name }} has a new image ready."
      data:
        tag: "{{ trigger.event.data.entity_id }}"
        actions:
          - action: "EXECUTE_UPDATE_{{ trigger.event.data.entity_id }}"
            title: "Update now"
          - action: "DISMISS_UPDATE_{{ trigger.event.data.entity_id }}"
            title: "Dismiss"
```

**Why a plain `state` trigger doesn't work here:** HA's `state` trigger does not support templated `entity_id` — confirmed via testing and an open HA feature request. The workaround is triggering on the generic `state_changed` event for everything, then filtering with a template `condition`.

### 2. Handle "Update now"
Calls the shared `script.portainer_perform_update` (see [Scripts](#scripts)) with the tapped entity — recreate, clear notification, and host-name lookup all live in that shared script now, not inline here. This automation is just an event unwrap + a script call. The webapp's "Install" button and batch-install action call the same script directly.

**Trigger id:** `notification_action` (shared with the "Dismiss" branch below — both fire on `event_type: mobile_app_notification_action`; the template condition is what splits them apart).

```yaml
conditions:
  - condition: trigger
    id: notification_action
  - condition: template
    value_template: "{{ trigger.event.data.action.startswith('EXECUTE_UPDATE_') }}"
sequence:
  - action: script.portainer_perform_update
    data:
      update_entity: "{{ trigger.event.data.action.replace('EXECUTE_UPDATE_', '') }}"
```

**Field name gotcha:** the `portainer.recreate_container` action does **not** support a `target:` block, and does **not** use `device_id` as the data field name despite that being what the online docs show. The correct field, confirmed via Developer Tools → Actions → YAML mode (the live schema, not the docs), is **`container_device_id`**.

### 3. Handle "Dismiss"

"Dismiss" only clears the phone notification — it never touches the underlying `update.*` entity, so the item stays visible and installable from the webapp regardless. `sensor.portainer_updates_pending` (see [Tracking sensors](#tracking-sensors)) already tracks every currently-available update independent of whether it was dismissed. (An earlier version of this automation created a persistent notification with an embedded webhook link so the update could be triggered later — that's what automation 4, retired below, used to handle. It's no longer needed for the same reason.)

**Trigger id:** `notification_action` (same event as "Handle update now" above, split by the template condition).

```yaml
conditions:
  - condition: trigger
    id: notification_action
  - condition: template
    value_template: "{{ trigger.event.data.action.startswith('DISMISS_UPDATE_') }}"
sequence:
  - action: "{{ notify_service }}"
    data:
      message: "clear_notification"
      data:
        tag: "{{ trigger.event.data.action.replace('DISMISS_UPDATE_', '') }}"
```

### 4. Perform update via webhook — retired

Retired in favor of automations 2/3 plus the tracking sensor above; also closed an unauthenticated-webhook gap in the process. Full history in the [legacy doc](portainer-ha-container-management-legacy.md#automation-4-retired--perform-update-via-webhook).

### 5. Startup recheck for missed updates
Catches updates where the original phone notification was lost (e.g. Companion App restarted before the user tapped a button). Runs once at HA startup, waits for the Portainer coordinator's first poll to complete, then checks the `sensor.portainer_updates_pending` tracking sensor (see [Tracking sensors](#tracking-sensors)) and sends a single aggregate notification if anything's pending — no per-item notifications, no action buttons; tap opens the webapp where each pending update can be installed directly.

**Trigger id:** `startup_recheck` (on the shared `trigger: homeassistant, event: start` trigger).

```yaml
conditions:
  - condition: trigger
    id: startup_recheck
sequence:
  - wait_template: >
      {{ integration_entities('portainer') | select('match', 'update\\.') | map('states') | reject('in', ['unknown','unavailable']) | list | length > 0 }}
    timeout: "00:05:00"
    continue_on_timeout: true
  - delay:
      seconds: 30
  - condition: template
    value_template: "{{ states('sensor.portainer_updates_pending') | int(0) > 0 }}"
  - action: "{{ notify_service }}"
    data:
      title: "Updates available (recheck)"
      message: >
        {{ states('sensor.portainer_updates_pending') }} pending update{{ 's' if states('sensor.portainer_updates_pending') | int != 1 else '' }} found at startup. Tap to review.
      data:
        tag: "portainer_updates_pending"
        clickAction: "{{ states('sensor.portainer_actions_url') }}"
```

**Why the 30-second delay:** this automation and `sensor.portainer_updates_pending` both trigger on `homeassistant: start`, and trigger order between them isn't guaranteed. The delay gives the sensor's own startup recompute time to populate before this automation reads it.

### 6. Notify on container trouble
Alerts (high priority, bypasses silent/DND via the `alarm_stream` channel) when a container exits/dies unexpectedly, or fails its healthcheck. Fires once per *increase* in `sensor.portainer_container_trouble`'s count, however many containers changed at once — the detection itself (with a 2-minute settle-time debounce) lives in that sensor, not here.

**Trigger id:** `container_trouble` (on the shared `trigger: state, entity_id: sensor.portainer_container_trouble` trigger).

```yaml
conditions:
  - condition: trigger
    id: container_trouble
  - condition: template
    value_template: >
      {{ trigger.to_state.state | int(0) > (trigger.from_state.state | int(0) if trigger.from_state else 0) }}
sequence:
  - action: "{{ notify_service }}"
    data:
      title: "Container trouble"
      message: >
        {{ trigger.to_state.state }} container{{ 's' if trigger.to_state.state | int != 1 else '' }} need attention. Tap to review.
      data:
        tag: "portainer_container_trouble"
        priority: high
        ttl: 0
        channel: alarm_stream
        clickAction: "{{ states('sensor.portainer_actions_url') }}"
```

**Known coverage gap — restart loops:** containers using `restart: unless-stopped` (Bob's standard policy) that crash-loop rarely spend meaningful time in `exited`, since Docker restarts them almost immediately — the settle-time check in the tracking sensor can miss this entirely. No fix implemented; would require tracking Docker's `RestartCount` via a separate `command_line` sensor, which wasn't built. Accepted as a known limitation rather than adding a bounded restart policy (`on-failure:N`), since that would sacrifice the "always come back after host reboot" behavior `unless-stopped` provides.

### 7. Report stale Portainer devices
Detects devices (containers/stacks) whose entities have all gone `unavailable` for at least 12 hours while the parent endpoint remains healthy — a strong signal the underlying asset was deleted in Portainer but HA never cleaned up (a confirmed open bug: [home-assistant/core#155548](https://github.com/home-assistant/core/issues/155548)). Fires once per *increase* in `sensor.portainer_stale_devices`'s count; the detection itself lives in that sensor.

The 12-hour floor uses each entity's `last_changed` timestamp directly (no extra helper needed, since `last_changed` resets automatically the moment an entity flips back to available — a flapping container never accumulates dwell time), taking the *minimum* age across a device's entities so the whole device has to have been down the full 12 hours, not just its earliest entity to drop. Host-health and host-name are both derived by walking `via_device_id` up to the root Endpoint device — necessary for stack-nested containers, where the immediate parent is the Stack, not the actual host.

**Trigger id:** `stale_devices` (on the shared `trigger: state, entity_id: sensor.portainer_stale_devices` trigger).

```yaml
conditions:
  - condition: trigger
    id: stale_devices
  - condition: template
    value_template: >
      {{ trigger.to_state.state | int(0) > (trigger.from_state.state | int(0) if trigger.from_state else 0) }}
sequence:
  - action: "{{ notify_service }}"
    data:
      title: "Stale Portainer devices"
      message: >
        {{ trigger.to_state.state }} device{{ 's' if trigger.to_state.state | int != 1 else '' }} look gone. Tap to review.
      data:
        tag: "portainer_stale_devices"
        clickAction: "{{ states('sensor.portainer_actions_url') }}"
```

**Deletion:** the webapp's Stale tab supports reviewing (tap through to the device page) and batch-deleting (select, then confirm) via `portainer_maintenance.remove_device` — see [Portainer Maintenance integration](#portainer-maintenance-integration). There's still no way to delete a device from inside a plain HA automation/script without that custom integration; HA itself exposes no such action natively.

## Tracking sensors

Automations 5, 6, and 7 all used to recompute their own "what needs attention right now" list on every run and fire one notification per matching item — fine for one problem, a flood for several at once (a startup with multiple missed updates, a batch recreate that trips several containers, an hourly stale-device sweep with an undeduplicated backlog). The fix: pull that computation out of the notification automations entirely and into three sensors that just track a live count and list. The automations above become pure notifiers that fire once per *increase* in a sensor's count; the webapp is what actually browses and acts on the list.

**Revised 2026-09-20 — moved from YAML template sensors to native Python entities, defined in the [Portainer Maintenance integration](#portainer-maintenance-integration)'s `sensor.py`, one `DataUpdateCoordinator` per sensor at the same recompute cadence as before (5 min / 1 min / 1 hour).** The original mechanism — trigger-based `template:` sensors in a hand-maintained `templates.yaml` — worked, but had two real downsides that motivated the move: no Helpers UI support at all (multi-trigger template sensors with a shared `variables:` block aren't editable there, full stop — YAML only), and no way to unit-test the logic short of actually running it inside HA. As native entities, both problems go away: they show up like any other integration's sensors, and the underlying Python functions were verified with a standalone test harness before shipping (see below).

Each entity_id is pinned explicitly in code (`self.entity_id = "sensor.portainer_updates_pending"` etc., set before the entity is added) to exactly what `templates.yaml` used to produce — the merged automation blueprint and the webapp's REST calls didn't need to change at all. This only lands cleanly if the old `templates.yaml`-based sensors are removed, and its `configuration.yaml` include deleted, **before** the new integration's sensors are set up — otherwise HA's entity registry auto-suffixes the new ones as `_2` to avoid colliding with the old ones already holding those ids. See the [migration note](#setup-steps) in Setup steps.

The three sensors, their coordinators, and the logic each ports from the original Jinja:

- **`sensor.portainer_updates_pending`** (`PortainerUpdatesCoordinator`, 5-minute poll) — every `update.*` entity currently `on`, with a `via_device_id`-walk-to-root host lookup per item, same as before. No settle-time needed — `update.*` going `on` is already a stable, digest-based signal, not something that flaps.
- **`sensor.portainer_container_trouble`** (`PortainerTroubleCoordinator`, 1-minute poll) — any `_state` entity reading `exited`/`dead`, or `_health` reading `unhealthy`, each gated by the same 120-second settle window as before (checked directly against `last_changed`, no separate timer helper needed). A container that's both exited *and* unhealthy still produces two rows rather than being de-duplicated — more information, not noise.
- **`sensor.portainer_stale_devices`** (`PortainerStaleCoordinator`, 1-hour poll) — devices whose entities have all gone `unavailable` for 12+ hours while the parent endpoint stays healthy, same logic as before **plus one genuine bug fix found while porting it**: the original Jinja had no guard against a root Endpoint device itself qualifying as "stale" if its *own* entities went unavailable for 12h+ (i.e. the whole host down, not a removed container) — because with no parent to check, the "no endpoint entities to check → assume healthy" branch let an endpoint pass its own stale check. Caught by a failing assertion in the standalone test harness described below, not by production behavior — fixed with an explicit `if root_id == device_id: continue` guard (a device with no `via_device_id` is a root Endpoint and can never be a stale *child* device candidate) immediately after computing each device's root.
- **`sensor.portainer_actions_url`** (no coordinator, computed once at setup) — the read-only click-through URL, replacing the old `text.portainer_cleanup_actions_dashboard_url` helper entity entirely; see [Portainer Maintenance integration](#portainer-maintenance-integration) for how it's computed.

Each list sensor's state is the item count, with the full list exposed as an `items` attribute — same shape as before: `{entity, name, secondary_info}` for updates/trouble, `{name, secondary_info, device_id, navigation_path}` for stale (the fields the webapp uses to build its "Review" link and to call `portainer_maintenance.remove_device`).

**How the port was verified without a live HA instance:** installing the real `homeassistant` pip package in this project's dev sandbox proved impractical (`--no-deps` fails on a missing transitive import; a full install fails building an unrelated dependency's wheel under modern `setuptools`). Instead, the pure logic functions (`_walk_to_root`, `_device_name`, the trouble dwell-time gate, the stale 12h-floor + endpoint-health gate) were extracted and exercised against a small duck-typed fake device/entity registry covering six scenarios, including the host-down edge case above. All six pass post-fix. This isn't a substitute for testing against real Portainer devices — see the [Verification checklist](#verification-checklist) — but it's real coverage of the logic itself, and it's how the bug above was actually found.

For reference, this is the original `templates.yaml` content the native sensors replace — kept here for the historical record and in case native entities ever need to be rolled back, not because it should still be deployed:

<details>
<summary>Original templates.yaml (superseded, click to expand)</summary>

```yaml
template:
  - trigger:
      - trigger: time_pattern
        minutes: "/5"
      - trigger: homeassistant
        event: start
    variables:
      update_items: >
        {% set ns = namespace(found=[]) %}
        {% for e in integration_entities('portainer') | select('match', 'update\\.') | list %}
          {% if states(e) == 'on' %}
            {% set walk = namespace(current=device_id(e)) %}
            {% for _ in range(4) %}
              {% set parent = device_attr(walk.current, 'via_device_id') %}
              {% if parent %}
                {% set walk.current = parent %}
              {% endif %}
            {% endfor %}
            {% set host = device_attr(walk.current, 'name') %}
            {% set ns.found = ns.found + [{'entity': e, 'name': (state_attr(e,'friendly_name') ~ ' (' ~ host ~ ')'), 'secondary_info': 'Update available'}] %}
          {% endif %}
        {% endfor %}
        {{ ns.found }}
    sensor:
      - name: "Portainer updates pending"
        unique_id: portainer_updates_pending
        state: "{{ update_items | length }}"
        attributes:
          items: "{{ update_items }}"

  - trigger:
      - trigger: time_pattern
        minutes: "/1"
      - trigger: homeassistant
        event: start
    variables:
      portainer_entities: >
        {% set ns = namespace(ids=[]) %}
        {% for e in states | map(attribute='entity_id') %}
          {% set eid = config_entry_id(e) %}
          {% if eid and config_entry_attr(eid, 'domain') == 'portainer' %}
            {% set ns.ids = ns.ids + [e] %}
          {% endif %}
        {% endfor %}
        {{ ns.ids }}
      trouble_items: >
        {% set ns = namespace(found=[]) %}
        {% for e in portainer_entities | select('match', '.*_state$') | list %}
          {% if states(e) in ['exited', 'dead'] and (now() - states[e].last_changed).total_seconds() >= 120 %}
            {% set did = device_id(e) %}
            {% set walk = namespace(current=did) %}
            {% for _ in range(4) %}
              {% set parent = device_attr(walk.current, 'via_device_id') %}
              {% if parent %}
                {% set walk.current = parent %}
              {% endif %}
            {% endfor %}
            {% set host = device_attr(walk.current, 'name') %}
            {% set ns.found = ns.found + [{'entity': e, 'name': (device_attr(did,'name') ~ ' (' ~ host ~ ')'), 'secondary_info': states(e)}] %}
          {% endif %}
        {% endfor %}
        {% for e in portainer_entities | select('match', '.*_health$') | list %}
          {% if states(e) == 'unhealthy' and (now() - states[e].last_changed).total_seconds() >= 120 %}
            {% set did = device_id(e) %}
            {% set walk = namespace(current=did) %}
            {% for _ in range(4) %}
              {% set parent = device_attr(walk.current, 'via_device_id') %}
              {% if parent %}
                {% set walk.current = parent %}
              {% endif %}
            {% endfor %}
            {% set host = device_attr(walk.current, 'name') %}
            {% set ns.found = ns.found + [{'entity': e, 'name': (device_attr(did,'name') ~ ' (' ~ host ~ ')'), 'secondary_info': 'unhealthy'}] %}
          {% endif %}
        {% endfor %}
        {{ ns.found }}
    sensor:
      - name: "Portainer container trouble"
        unique_id: portainer_container_trouble
        state: "{{ trouble_items | length }}"
        attributes:
          items: "{{ trouble_items }}"

  - trigger:
      - trigger: time_pattern
        hours: "/1"
      - trigger: homeassistant
        event: start
    variables:
      portainer_entities: >
        {% set ns = namespace(ids=[]) %}
        {% for e in states | map(attribute='entity_id') %}
          {% set eid = config_entry_id(e) %}
          {% if eid and config_entry_attr(eid, 'domain') == 'portainer' %}
            {% set ns.ids = ns.ids + [e] %}
          {% endif %}
        {% endfor %}
        {{ ns.ids }}
      stale_items: >
        {% set ns = namespace(found=[]) %}
        {% set devices_seen = portainer_entities | map('device_id') | reject('none') | unique | list %}
        {% for did in devices_seen %}
          {% set dev_entities = device_entities(did) | select('in', portainer_entities) | list %}
          {% set all_unavailable = dev_entities | map('states') | select('eq', 'unavailable') | list | length == dev_entities | length %}
          {% set walk = namespace(current=did) %}
          {% for _ in range(4) %}
            {% set parent = device_attr(walk.current, 'via_device_id') %}
            {% if parent %}
              {% set walk.current = parent %}
            {% endif %}
          {% endfor %}
          {% set endpoint_id = walk.current %}
          {% set host_name = device_attr(endpoint_id, 'name') %}
          {% set endpoint_entities = device_entities(endpoint_id) | select('in', portainer_entities) | list if endpoint_id != did else [] %}
          {% set endpoint_healthy = (endpoint_entities | map('states') | select('eq', 'unavailable') | list | length == 0) if endpoint_entities | length > 0 else true %}
          {% if all_unavailable and dev_entities | length > 0 and endpoint_healthy %}
            {% set ages = namespace(seconds=[]) %}
            {% for e in dev_entities %}
              {% set ages.seconds = ages.seconds + [(now() - states[e].last_changed).total_seconds()] %}
            {% endfor %}
            {% set min_age = ages.seconds | min %}
            {% if min_age >= 43200 %}
              {% set ns.found = ns.found + [{'name': (device_attr(did,'name') ~ ' (' ~ host_name ~ ')'), 'secondary_info': 'Stale — 12h+ unavailable, host healthy', 'device_id': did, 'navigation_path': ('/config/devices/device/' ~ did)}] %}
            {% endif %}
          {% endif %}
        {% endfor %}
        {{ ns.found }}
    sensor:
      - name: "Portainer stale devices"
        unique_id: portainer_stale_devices
        state: "{{ stale_items | length }}"
        attributes:
          items: "{{ stale_items }}"
```

</details>

## Portainer Maintenance integration

There's no built-in way to delete a device from an automation, script, or any documented service — the native Settings → Devices page's Delete button calls an internal frontend WebSocket command, not a service (full rationale for why a real Lovelace card couldn't reach it either is in the [legacy doc](portainer-ha-container-management-legacy.md#why-not-a-real-custom-lovelace-card)). This custom integration exposes device deletion as one real service — `portainer_maintenance.remove_device` — built on `device_registry.async_remove_device()`, a stable, public API integrations routinely use to prune their own stale devices. The webapp's stale-device delete action calls this service directly, once per selected device.

**Renamed from "Portainer Cleanup" and substantially expanded, 2026-09-20.** What started as a single-service shim has grown into the thing that actually ties the whole design together, so it earned a name that reflects that ("Portainer Maintenance") and a proper `ConfigEntry`-based setup (`config_flow: true`, single field: the webapp's URL). Beyond the `remove_device` service, on every load this integration now also:

1. **Installs its bundled automation/script blueprints automatically** — `bundled_blueprints/` inside the integration is copied into HA's `blueprints/` dir on every setup (`shutil.copy`, offloaded to the executor since it's real file I/O). No more separate SSH deploy step for the blueprints; see [Blueprints](#blueprints). Treat the deployed copies as generated, not hand-edited — they're overwritten on every HA restart.
2. **Registers an iframe sidebar panel** for the actions webapp at a fixed, known path (`portainer-actions`), via `frontend.async_register_built_in_panel` — the same primitive the legacy `panel_iframe` YAML integration used, just invoked from a config-flow integration instead of static YAML. This replaces the old manual "Add Dashboard → Webpage → read the random URL out of the address bar" step entirely — see [Embedding in Home Assistant](#embedding-in-home-assistant).
3. **Computes the notification click-through URL automatically**, from this HA instance's own configured `external_url`/`internal_url` (Settings → System → Network) plus the fixed panel path, and exposes it as the read-only `sensor.portainer_actions_url` — no more typing a URL into a text helper or hunting for a dashboard's auto-generated path.
4. **Defines the three tracking sensors** as native Python entities on `DataUpdateCoordinator`s instead of YAML template sensors — see [Tracking sensors](#tracking-sensors) for the full writeup, including a genuine logic bug this rewrite found and fixed.

**`custom_components/portainer_maintenance/manifest.json`:**

```json
{
  "domain": "portainer_maintenance",
  "name": "Portainer Maintenance",
  "version": "3.0.0",
  "documentation": "https://github.com/bdelima/homeassistant-config",
  "issue_tracker": "https://github.com/bdelima/homeassistant-config/issues",
  "dependencies": ["portainer"],
  "codeowners": ["@bdelima"],
  "iot_class": "local_polling",
  "config_flow": true
}
```

**`custom_components/portainer_maintenance/const.py`:**

```python
"""Constants for the Portainer Maintenance integration."""

DOMAIN = "portainer_maintenance"
CONF_WEBAPP_URL = "webapp_url"

PANEL_PATH = "portainer-actions"
PANEL_TITLE = "Portainer Actions"
PANEL_ICON = "mdi:docker"

SENSOR_UPDATES_PENDING = "portainer_updates_pending"
SENSOR_CONTAINER_TROUBLE = "portainer_container_trouble"
SENSOR_STALE_DEVICES = "portainer_stale_devices"
SENSOR_ACTIONS_URL = "portainer_actions_url"
```

**`custom_components/portainer_maintenance/config_flow.py`:**

```python
"""Config flow for Portainer Maintenance.

One-time setup: asks for the webapp's own URL (what the sidebar panel
iframes to). Only one instance is needed -- there's nothing per-device to
configure.
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
            # Single-instance integration -- nothing to key multiple entries on.
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Portainer Maintenance", data=user_input)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA)
```

**`custom_components/portainer_maintenance/__init__.py`:**

```python
"""Portainer Maintenance.

Formerly "Portainer Cleanup" -- renamed and expanded once the design grew
past "expose one service" into a real maintenance layer on top of the core
`portainer` integration. On setup this integration:

1. Registers `portainer_maintenance.remove_device`, built on the stable
   `device_registry.async_remove_device()` API -- the webapp's stale-device
   delete calls this, because the native Settings -> Devices page's own
   Delete button calls an internal frontend WebSocket command, not a
   documented service.

2. Installs its bundled automation/script blueprints into HA's config dir
   automatically (see bundled_blueprints/) -- no more separate SSH deploy
   step for those. Re-copied on every load, so treat the deployed copies
   as generated, not hand-editable.

3. Registers an iframe sidebar panel pointing at the Portainer actions
   webapp, at a fixed, known path (PANEL_PATH) -- via the same
   `frontend.async_register_built_in_panel` primitive the legacy
   `panel_iframe` YAML integration used, just invoked from a config-flow
   integration instead of static YAML. This removes the old manual
   "Add Dashboard -> Webpage -> read the random URL from the address bar"
   step entirely.

4. Computes the notification click-through URL automatically from this HA
   instance's own configured external/internal URL plus the fixed panel
   path, and exposes it as a read-only sensor (see sensor.py) -- no more
   typing a URL into a text helper or a config field.

5. Forwards to the sensor platform, which defines the three tracking
   sensors (updates pending / container trouble / stale devices) as native
   entities on coordinators, instead of YAML template sensors in
   templates.yaml. Their entity_ids are pinned explicitly to match what
   templates.yaml used to produce (sensor.portainer_updates_pending, etc.)
   so the merged automation blueprint and the webapp don't need to change.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr

from .const import CONF_WEBAPP_URL, DOMAIN, PANEL_ICON, PANEL_PATH, PANEL_TITLE

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

SERVICE_REMOVE_DEVICE = "remove_device"
SERVICE_REMOVE_DEVICE_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})

BUNDLED_BLUEPRINTS_DIR = Path(__file__).parent / "bundled_blueprints"

# (bundled source, relative to BUNDLED_BLUEPRINTS_DIR) -> (dest, relative to config dir)
BLUEPRINT_FILES = [
    (
        "automation/portainer_automations.yaml",
        f"blueprints/automation/{DOMAIN}/portainer_automations.yaml",
    ),
    (
        "script/perform_update.yaml",
        f"blueprints/script/{DOMAIN}/perform_update.yaml",
    ),
    (
        "script/update_done.yaml",
        f"blueprints/script/{DOMAIN}/update_done.yaml",
    ),
]


def _install_blueprints(hass: HomeAssistant) -> None:
    """Copy the bundled blueprint files into HA's config dir. Blocking I/O -- run in the executor."""
    for src_rel, dest_rel in BLUEPRINT_FILES:
        src = BUNDLED_BLUEPRINTS_DIR / src_rel
        dest = Path(hass.config.path(dest_rel))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
        _LOGGER.debug("Installed blueprint %s -> %s", src, dest)


def _register_panel(hass: HomeAssistant, webapp_url: str) -> None:
    try:
        frontend.async_register_built_in_panel(
            hass,
            component_name="iframe",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            frontend_url_path=PANEL_PATH,
            config={"url": webapp_url},
            require_admin=False,
        )
    except ValueError:
        # Already registered (e.g. a config entry reload) -- replace it so
        # a changed webapp_url actually takes effect.
        frontend.async_remove_panel(hass, PANEL_PATH)
        frontend.async_register_built_in_panel(
            hass,
            component_name="iframe",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            frontend_url_path=PANEL_PATH,
            config={"url": webapp_url},
            require_admin=False,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Portainer Maintenance from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    async def handle_remove_device(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        registry = dr.async_get(hass)
        device = registry.async_get(device_id)

        if device is None:
            raise ValueError(f"No device found with id '{device_id}'")

        _LOGGER.info(
            "Removing device '%s' (%s) via %s.remove_device",
            device.name_by_user or device.name,
            device_id,
            DOMAIN,
        )
        registry.async_remove_device(device_id)

    if not hass.services.has_service(DOMAIN, SERVICE_REMOVE_DEVICE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REMOVE_DEVICE,
            handle_remove_device,
            schema=SERVICE_REMOVE_DEVICE_SCHEMA,
        )

    await hass.async_add_executor_job(_install_blueprints, hass)

    webapp_url = entry.data[CONF_WEBAPP_URL]

    base_url = hass.config.external_url or hass.config.internal_url or ""
    if base_url:
        actions_url = f"{base_url.rstrip('/')}/{PANEL_PATH}"
    else:
        actions_url = ""
        _LOGGER.warning(
            "No external_url or internal_url configured in Home Assistant "
            "(Settings -> System -> Network) -- the Portainer actions URL "
            "sensor will be empty until one is set."
        )
    hass.data[DOMAIN][entry.entry_id]["actions_url"] = actions_url

    _register_panel(hass, webapp_url)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Portainer Maintenance config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        frontend.async_remove_panel(hass, PANEL_PATH)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_REMOVE_DEVICE)
    return unload_ok
```

**`custom_components/portainer_maintenance/sensor.py`:** the three tracking sensors plus `sensor.portainer_actions_url` — see [Tracking sensors](#tracking-sensors) for the full writeup of what each one does and the stale-device edge case it fixes. Full source lives in this project folder; not reproduced twice here.

**`custom_components/portainer_maintenance/services.yaml`:**

```yaml
remove_device:
  name: Remove device
  description: >-
    Removes a device from the device registry. Used to clean up stale
    Portainer devices/stacks that Portainer's own HA integration doesn't
    clean up on its own (a confirmed open bug: home-assistant/core#155548).
    This deletes the device outright -- there is no undo.
  fields:
    device_id:
      name: Device
      description: The device_id to remove (not an entity_id).
      required: true
      example: "3f9e1c2b4a5d6e7f8091a2b3c4d5e6f7"
      selector:
        text:
```

**`custom_components/portainer_maintenance/translations/en.json`:**

```json
{
  "config": {
    "step": {
      "user": {
        "title": "Portainer Maintenance",
        "description": "One-time setup. Enter the Portainer actions webapp's own URL -- this integration will register a sidebar panel pointing at it, install its bundled blueprints, compute the notification click-through URL automatically, and create the tracking sensors.",
        "data": {
          "webapp_url": "Webapp URL"
        }
      }
    },
    "abort": {
      "already_configured": "Portainer Maintenance is already set up."
    }
  }
}
```

**Deployment:** drop this folder as `custom_components/portainer_maintenance/` under `/opt/homeassistant/core/config/` — HA loads `custom_components` at startup, so this needs a full HA restart before it's even visible to Add Integration, not just a config reload. Not published to HACS — small and personal enough that manual placement is simpler than standing up a repo for it. Since `config_flow: true`, **the restart alone doesn't finish setup** — nothing runs until you also add it via Settings → Devices & Services → Add Integration → "Portainer Maintenance" and enter the webapp's URL (see [Setup steps](#setup-steps)).

**Deploy block:** `deploy-portainer-maintenance-integration.txt` (under `ha-config-deploy/` in this project folder) writes the full integration — `manifest.json`, `const.py`, `config_flow.py`, `__init__.py`, `sensor.py`, `services.yaml`, `translations/en.json`, and the 3 bundled blueprint YAMLs — to `/opt/homeassistant/core/config/custom_components/portainer_maintenance/`. Paste it into an SSH session on ojochal; its own output prints the full next-steps sequence, including the migration removal order if you're coming from the old `portainer_cleanup` + `templates.yaml` + standalone-blueprints setup (see the [migration note](#setup-steps) in Setup steps).

**Not yet verified end-to-end** — `device_registry.async_remove_device()`'s exact behavior against a real Portainer device (versus the frontend's own `remove_config_entry`-scoped removal) hasn't been tested, and neither has the panel registration, the auto-installed blueprints, or the native sensors against a real running HA instance (only the Python logic itself has been unit-tested standalone — see [Tracking sensors](#tracking-sensors)). Test against a real, already-confirmed-stale device before trusting deletion with anything you'd mind losing, and work through the full [Verification checklist](#verification-checklist) before relying on the rest.

**How far this was pushed, and where the line was drawn:** the integration lays down the blueprint *files* and could, in principle, go one step further and also create the actual automation/script *instances* from them (auto-filling `notify_device`, writing the resulting automation/script config directly). That was considered and deliberately not built — it would mean the integration writing directly into HA's internal automation/script storage format, bypassing the supported config-entry/entity API surface entirely, and it'd be fragile against any future change to how HA stores those configs internally. Clicking "Use Blueprint" twice at setup is a small enough manual step to leave alone.
## Webpage dashboard

A small standalone web app, embedded in HA as a `type: iframe` card, giving a genuine multi-select data table for updates and stale devices instead of Lovelace's declarative card limits. This is the primary interface for everything above — see [Setup steps](#setup-steps) for where it fits in the overall rollout.

**What it reuses unchanged:** all three tracking sensors, `script.portainer_perform_update`, `portainer_maintenance.remove_device`, and automations 1–3 and 5–7. None of the HA-side backend changes for this — it's purely a presentation layer that reads the same sensors and calls the same scripts/services, just from real code instead of Lovelace card YAML.

**Why this can't be a claude.ai Artifact:** published Artifacts run under a content-security policy that blocks fetch/XHR/WebSocket to arbitrary hosts (only specific CDNs are allowlisted, and only for scripts/styles) — a hosted Artifact page could never actually call `https://ha.pumapants.cc`. This has to be a real, self-hosted app, deployed the same way as everything else in the fleet.

### Architecture

A two-file app: a small FastAPI backend that holds the HA long-lived access token server-side (never sent to the browser) and proxies a handful of endpoints, plus a static HTML/CSS/JS frontend it serves directly — one container, no build step, no framework. Uvicorn binds to **port 8000** inside the container (the Dockerfile's `EXPOSE 8000` / `CMD`) — this is the port an NPM proxy host needs to forward to (see [Embedding in Home Assistant](#embedding-in-home-assistant)); the sample service block below deliberately publishes no host port mapping, so 8000 is only reachable from other containers on the same Docker network, not from the host or LAN directly.

- `GET /api/config` — returns the HA base URL (not sensitive, needed for building "open in HA" links)
- `GET /api/action-items` — reads all three tracking sensors via HA's REST API, returns `{updates: {count, items}, trouble: {...}, stale: {...}}`
- `POST /api/actions/install` — takes a list of `update.*` entity ids, calls `script.portainer_perform_update` for each
- `POST /api/actions/delete-stale` — takes a list of device ids, calls `portainer_maintenance.remove_device` for each
- Frontend polls `/api/action-items` every 15s, renders three tabs (Updates / Trouble / Stale) as real tables with checkboxes on the two actionable tabs, and a floating action bar that appears once something's selected

The frontend reads each tracking sensor's `items` attribute directly — `entity`, `name`, `secondary_info` on every item, plus `device_id` and `navigation_path` on stale items (see [Tracking sensors](#tracking-sensors)) — and maintains its own selection state client-side; nothing here depends on the `input_text` helpers the old Lovelace UI used.

**Files** (in this project folder under `webapp/`): `main.py`, `requirements.txt`, `static/index.html`, `static/style.css`, `static/app.js`, `Dockerfile`. The deploy script lays these down on disk; there's no standalone compose file — see [Deployment](#deployment) for the sample service block to add to the existing `homeassistant` stack instead.

### Auth: long-lived access token, held server-side only

Generate one from your own HA profile (Profile → Security → Long-Lived Access Tokens). Treat it exactly like the old webhook_id was flagged: a secret with full API access as whichever user created it. The backend reads it from `HA_TOKEN_FILE` (a mounted file — preferred, keeps it out of `docker inspect`/compose-file output) or falls back to a plain `HA_TOKEN` env var if that's simpler for now. It's never sent to the browser — the frontend only ever talks to this app's own `/api/*` endpoints, not HA directly.

**This app should not be exposed publicly.** Scope its NPM proxy host the same way `dev-share` is scoped to your desktop's IP, or keep it LAN-only / behind Tailscale — anyone who can reach it can install updates and delete devices through it.

### Deployment

No standalone compose file/stack for this — it rides along in the existing `homeassistant` stack in Portainer. Run `deploy-portainer-action-dashboard.sh` (in this project folder, alongside `webapp/`) first to lay the app's files down at `/opt/homeassistant/portainer-action-dashboard` (source + Dockerfile + a placeholder `ha_token`), then add a service block like this to that stack's compose in Portainer and redeploy:

```yaml
  # Add this service to your existing homeassistant stack's compose.
  # Sample only -- adjust to fit whatever else is already in that stack.
  portainer-action-dashboard:
    build: /opt/homeassistant/portainer-action-dashboard
    container_name: portainer-action-dashboard
    restart: unless-stopped
    environment:
      HA_BASE_URL: "https://ha.pumapants.cc"
      HA_TOKEN_FILE: /run/secrets/ha_token
    volumes:
      - /opt/homeassistant/portainer-action-dashboard/ha_token:/run/secrets/ha_token:ro
    networks:
      - npm_proxy
```

Notes on merging this in: `build:` points at an absolute path rather than `.`, since this service block is meant to be pasted into a compose file that lives elsewhere (wherever the `homeassistant` stack's compose already lives) — Portainer builds from that path on the host when the stack deploys. Drop the `networks:` line entirely if the existing `homeassistant` stack already declares `npm_proxy` (either as an external network at the top level, or because another service in the stack already joins it) — no need for a second `networks: npm_proxy: external: true` block if one's already there. No `ports:` mapping is included since NPM is the intended path in — the container only needs to be reachable from NPM over the shared `npm_proxy` network, at `portainer-action-dashboard:8000` (container name : the port from [Architecture](#architecture) above). Add a `ports:` mapping only if you also want direct break-glass access from the host/LAN. Fill in the real token at `/opt/homeassistant/portainer-action-dashboard/ha_token` (the deploy script writes a placeholder) before redeploying the stack.

### Embedding in Home Assistant

Before this can work, the NPM proxy host for `actions.o.pumapants.cc` needs to forward to **`portainer-action-dashboard:8000`** (container name : port — see [Architecture](#architecture); NPM needs to be on the shared `npm_proxy` network to resolve that name). Scoped/restricted per [Auth](#auth-long-lived-access-token-held-server-side-only) — not public.

**Revised 2026-09-20 — this is now automatic, done by the [Portainer Maintenance integration](#portainer-maintenance-integration) itself.** The manual steps this section used to describe — Settings → Dashboards → Add Dashboard → **Webpage**, then reading the auto-generated path back out of the browser's address bar because HA derives it unpredictably from the dashboard's title — are gone. Setting up the integration (Settings → Devices & Services → Add Integration → "Portainer Maintenance", enter the webapp's own URL) registers a sidebar panel at a **fixed, known path** (`/portainer-actions`, via `frontend.async_register_built_in_panel` — the same primitive the legacy `panel_iframe` YAML integration used) automatically. There's nothing left to configure here beyond the one URL entered at setup.

**Why not just link to the webapp's own domain (`https://actions.o.pumapants.cc`) directly:** the Companion App's `clickAction` only navigates *within* the app when the URL matches your HA instance's own domain (`ha.pumapants.cc`) — anything else opens as an external link in a separate browser/webview, dropping you outside the app entirely. So the merged automation's notifications point at `https://ha.pumapants.cc/portainer-actions` (the panel), not the webapp's own domain — that's what actually opens the panel inside HA rather than kicking you out to a bare page.

**How the click-through URL is computed and kept current:** since the panel's path is now fixed rather than dashboard-title-dependent, the integration computes the full URL itself at setup — `hass.config.external_url` (or `internal_url` as a fallback) plus the fixed panel path — and exposes it as the read-only `sensor.portainer_actions_url`. This replaced two earlier approaches in turn: plain literal duplication across all three notification branches (worked, but meant three lines to find-and-replace by hand any time the URL changed), and then a one-off editable `text.*` helper entity on the old `portainer_cleanup` integration (better, but still something a human had to type in correctly once). Neither is needed anymore — there's no value here that can be entered wrong, since nothing is entered at all. The merged automation blueprint reads it the same way regardless:

```yaml
clickAction: "{{ states('sensor.portainer_actions_url') }}"
```

If this sensor is ever empty, check Settings → System → Network — it means neither `external_url` nor `internal_url` is set on this HA instance.

## Scripts

**Revised 2026-09-20 — deployed as blueprints, auto-installed by the integration.** Both scripts below are defined as blueprints, laid down automatically as part of the [Portainer Maintenance integration](#portainer-maintenance-integration)'s bundled blueprints, and instantiated via Settings → Automations & Scenes → Scripts → Add Script → Use Blueprint, same mechanism as the automation. See [Blueprints](#blueprints) for why this changed, and a real gotcha specific to scripts: **you still have to manually set each script's Entity ID after creating it from the blueprint** — HA doesn't do this automatically, and both `script.portainer_perform_update` and `script.portainer_update_done` are called by that exact name from elsewhere (automation 2, the webapp's backend, and each other). The YAML below is what each blueprint contains.

**The old direct-`scripts.yaml`-append approach (`deploy-portainer-scripts.txt`) still works** as a simpler fallback if the blueprint's manual entity-id step ever causes problems — it guarantees the right `entity_id` from creation with no extra step, at the cost of the notify-target duplication the blueprint conversion was meant to fix. Don't run both against the same HA instance — pick one per script to avoid a duplicate-id conflict.

### `script.portainer_perform_update`
The single entry point that actually does an update: recreates the container with a fresh image pull, clears any lingering phone notification for it, then hands off to the confirmation script. Automation 2 (live "Update now" tap) and the webapp's install/batch-install actions all call this directly with just an `update_entity`.

```yaml
fields:
  update_entity:
    example: "update.plex_update"
sequence:
  - variables:
      notify_device: !input notify_device
      notify_service: "{{ 'notify.mobile_app_' ~ device_attr(notify_device, 'name') | slugify }}"
      container_device_id: "{{ device_id(update_entity) }}"
      host_name: >
        {% set walk = namespace(current=container_device_id) %}
        {% for _ in range(4) %}
          {% set parent = device_attr(walk.current, 'via_device_id') %}
          {% if parent %}
            {% set walk.current = parent %}
          {% endif %}
        {% endfor %}
        {{ device_attr(walk.current, 'name') }}
      device_name: "{{ state_attr(update_entity, 'friendly_name') }} ({{ host_name }})"
  - action: portainer.recreate_container
    data:
      container_device_id: "{{ container_device_id }}"
      pull_image: true
  - action: "{{ notify_service }}"
    data:
      message: "clear_notification"
      data:
        tag: "{{ update_entity }}"
  - action: script.portainer_update_done
    data:
      device_name: "{{ device_name }}"
      update_entity: "{{ update_entity }}"
```

### `script.portainer_update_done`
Shared finishing logic — posts the "Update performed" confirmation. Called only by `script.portainer_perform_update`.

**Revised 2026-09-20** — this only ever created a `persistent_notification`, which shows up in HA's own notification bell but never pushes to the phone — so short of having HA open, there was no actual way to know an update went through. Added a matching phone push alongside it.

```yaml
fields:
  device_name:
    example: "Plex"
  update_entity:
    example: "update.plex_update"
sequence:
  - variables:
      notify_device: !input notify_device
      notify_service: "{{ 'notify.mobile_app_' ~ device_attr(notify_device, 'name') | slugify }}"
  - action: persistent_notification.dismiss
    data:
      notification_id: "portainer_update_{{ update_entity | replace('.', '_') }}"
  - action: persistent_notification.create
    data:
      notification_id: "portainer_update_{{ update_entity | replace('.', '_') }}"
      title: "Update performed: {{ device_name }}"
      message: "Updated on {{ now().strftime('%Y-%m-%d %H:%M') }}"
  - action: "{{ notify_service }}"
    data:
      title: "Update performed"
      message: "{{ device_name }} updated on {{ now().strftime('%Y-%m-%d %H:%M') }}."
      data:
        tag: "portainer_update_done_{{ update_entity | replace('.', '_') }}"
```

## Blueprints

**Added 2026-09-20, merged the same day, then bundled into the integration itself later the same day.** All six live automations are defined as **one blueprint** (`Portainer Maintenance: automations (merged)`) producing **one automation entity** — not six. Both scripts remain separate script blueprints (a script blueprint's `fields:` already parameterize per-call values like `update_entity`, so merging them wouldn't remove duplication the way merging the automations does — see "Why the scripts stayed separate" below). This closes a real, previously-unaddressed gap: the phone notify target was hardcoded in five automations (1, 3, 5, 6, 7) and both scripts — seven places — with no single spot to change it, unlike the dashboard URL, which got the [Portainer Maintenance integration](#portainer-maintenance-integration)'s treatment earlier the same day.

**These three files now live inside the integration** (`custom_components/portainer_maintenance/bundled_blueprints/`) and are copied into HA's `blueprints/` dir automatically on every load — there's no more standalone `deploy-portainer-blueprints.txt` step. Treat the deployed copies (under `blueprints/automation|script/portainer_maintenance/`) as generated: they're overwritten every time HA restarts with this integration installed, so don't hand-edit them in place.

**How the merged automation works:** the blueprint defines five triggers, each tagged with an `id` (`update_available`, `notification_action`, `startup_recheck`, `container_trouble`, `stale_devices` — `notification_action` covers both "Update now" and "Dismiss", split by a template condition same as before). A shared `variables:` step computes the notify service once; then a single `choose:` block dispatches to the matching behavior using a `condition: trigger, id: ...` check (HA's built-in trigger-id condition), each paired with the same template condition each behavior always had. See [Automations](#automations) above for each behavior's branch, numbered 1–7 (4 stayed retired) as before, or read the blueprint file itself for the complete picture in one place.

**Tradeoff accepted by merging:** one automation entity means one enable/disable toggle and one shared trace/run history for all six behaviors. Turning off just "stale device alerts," for instance, now means editing the `choose:` block (or wrapping that one branch's `sequence:` in a `condition: false`) rather than flipping a switch in the UI, and HA's trace view shows one interleaved history instead of six separate ones. Accepted as the cost of "one thing to look at" — revisit if that granularity turns out to matter in practice.

**How the notify target is parameterized:** the blueprint takes one `notify_device` input — a `device` selector filtered to the `mobile_app` integration, giving a real dropdown at creation time instead of typing a service name. The actual service is derived from the picked device and called via a templated `action:` (HA supports templating the service/action name directly, not just its data):

```yaml
variables:
  notify_device: !input notify_device
  notify_service: "{{ 'notify.mobile_app_' ~ device_attr(notify_device, 'name') | slugify }}"
action: "{{ notify_service }}"
```

The two script blueprints take the same `notify_device` input independently (a script blueprint is still its own thing, separate from the automation blueprint) — set it to the same device in both places.

**Why the dashboard URL is *not* a blueprint input:** a blueprint input is set once per instance at creation time. Since it's now all one automation instance, this matters less than it did when weighing per-automation duplication, but it's still not a blueprint input — automations 5, 6, and 7's branches read it live via `{{ states('sensor.portainer_actions_url') }}`, keeping it in sync with whatever the integration currently computes rather than freezing it at automation-creation time.

**Why the scripts stayed separate (not merged into one):** `script.portainer_perform_update` and `script.portainer_update_done` are called by exact name from different places (automation 2's branch, the webapp's backend, and `perform_update` calling `update_done` itself) — they're not alternate reactions to different triggers the way the six automation behaviors were, so there's no natural single dispatch point to merge them around. Each already takes its own `fields:` for the one thing that varies per call.

**No live YAML-paste editor for authoring a *new* blueprint** (unlike automations/scripts) — Settings → Blueprints → Import Blueprint only accepts a URL, not pasted YAML. So these are deployed as files — which is exactly why bundling them into the integration and auto-installing on every load (see below) made sense once the integration existed anyway.

**The 3 blueprint files:**
- `bundled_blueprints/automation/portainer_automations.yaml` → installs to `blueprints/automation/portainer_maintenance/portainer_automations.yaml` — all six live automations, input: `notify_device`
- `bundled_blueprints/script/perform_update.yaml` → installs to `blueprints/script/portainer_maintenance/perform_update.yaml` — `script.portainer_perform_update`, input: `notify_device`, plus its existing `update_entity` field
- `bundled_blueprints/script/update_done.yaml` → installs to `blueprints/script/portainer_maintenance/update_done.yaml` — `script.portainer_update_done`, input: `notify_device`, plus its existing `device_name`/`update_entity` fields

**No separate deploy step anymore** — these three files live inside `custom_components/portainer_maintenance/` and are copied into place by the integration itself on every load (see [Portainer Maintenance integration](#portainer-maintenance-integration)). Once the integration is installed and configured, the blueprints already exist; you still have to create the automation instance (once) and both script instances via the UI (Settings → Automations & Scenes → Create Automation / Add Script → **Use Blueprint**) — nothing creates those instances for you (see [Portainer Maintenance integration](#portainer-maintenance-integration) for why that step was deliberately left manual rather than also automated).

**Critical manual step for the two script blueprints only:** creating a script from a blueprint does **not** give it the friendly `entity_id` this design depends on — HA still derives it from whatever title you type at creation (same limitation direct-YAML deployment was built to avoid for scripts in the first place; blueprints don't change this). After creating each script instance, open its settings (cog icon) and set the Entity ID explicitly to `portainer_perform_update` / `portainer_update_done`. The merged automation doesn't have this problem — nothing calls it by a specific id.

**If you already deployed automations/scripts the old way** (pasted YAML, the direct `scripts.yaml` append, the standalone `deploy-portainer-blueprints.txt` blueprints under `blueprints/*/portainer/`, or the earlier six-blueprint version from the same day) — delete/remove those first before creating the merged-automation and script instances, to avoid duplicate/conflicting triggers or a duplicate-id conflict on the scripts. The old standalone blueprint files under `.../portainer/` (as opposed to the integration's own `.../portainer_maintenance/`) can simply be left in place or removed — they don't conflict, they're just no longer the ones that matter.

**Not yet verified end-to-end** — the `device_attr(notify_device, 'name') | slugify` derivation of the notify service name is the standard community pattern for this, but hasn't been exercised against a real mobile_app device selection in this instance yet, and neither has the merged automation's `choose`/trigger-id dispatch (each branch's logic is unchanged from what worked before, but the dispatch mechanism itself is new). Confirm the derived service name matches your actual `notify.mobile_app_*` entity, and that all six behaviors still fire correctly, before relying on the rest.

## Operational notes

- **New containers require one manual step:** hide their `update.*` entity (Settings → visibility toggle — hides from the Settings badge/dashboards without affecting automation triggering) to avoid update-available badge clutter. Confirmed hiding has zero effect on automation firing.
- **Portainer's HA integration auto-assigns Area = stack/host name** to every device it creates. This is cosmetic and has no effect on any automation (none of the logic here uses Area). Left alone rather than bulk-reassigning.
- **Watchtower has been retired** in favor of this review-and-approve flow. Recommend removing the Watchtower stack/container entirely (it held broad Docker socket access) rather than leaving it stopped.
- **Known accepted limitation — multi-arch manifest-list images:** some GHCR/Docker Hub images (confirmed case: `ghcr.io/bakito/adguardhome-sync`, all current tags) publish only multi-architecture manifest lists, with no clean single-platform tag available at any current version. Update detection for these can get stuck/never trigger, likely due to a digest-comparison mismatch between the manifest-list digest and the platform-specific digest actually running. No fix implemented — accepted as a rare, container-specific gap; Portainer's own UI remains the fallback source of truth for these specific images. Not a general problem with multi-arch images — most others in the fleet update correctly.

## Verification checklist

Everything here is new/rewritten and hasn't been exercised against production yet. Work through this before relying on any of it for a real update or a real stale device:

- Confirm the HA REST API calls from the webapp's backend actually authenticate (`GET /api/config`, `GET /api/action-items`) and return the expected sensor shape.
- Confirm the webapp's install action actually installs (not just opens more-info) — try it on one real pending update first.
- Confirm `portainer_maintenance.remove_device` actually removes a device cleanly against a real, already-confirmed-stale device (12h+ unavailable, endpoint healthy) — not just against test data.
- Confirm the batch-select + confirm flow in the webapp for both Updates and Stale.
- Check the iframe renders acceptably at phone width in the Companion App specifically (iframes inside the Companion App's webview have occasionally been finicky in the wider HA community) — not just in a desktop browser.
- Confirm the `portainer_maintenance` config entry actually sets up on restart + Add Integration: sidebar panel appears at the expected path, `sensor.portainer_actions_url` has a real (non-empty) value, and the blueprint files actually land under `blueprints/automation|script/portainer_maintenance/`.
- Confirm the three native tracking sensors (`sensor.portainer_updates_pending`, `_container_trouble`, `_stale_devices`) show up under the Portainer Maintenance device with their exact intended entity_ids — no `_2` suffix, which would mean the old `templates.yaml`-based sensors weren't fully removed before restart (see the [migration note](#setup-steps)).
- Confirm each blueprint-based automation/script instance fires correctly and that `device_attr(notify_device, 'name') | slugify` resolves to the real `notify.mobile_app_*` service — especially the two scripts' entity_id after the manual rename step.
- Specifically exercise the stale-devices bug fix: confirm a whole host going down (not just one container) does *not* cause the host's own Endpoint device to appear in `sensor.portainer_stale_devices` after 12+ hours — this was a real gap found and fixed while porting the sensor logic (see [Tracking sensors](#tracking-sensors)), but hasn't been confirmed against a real host outage yet.

## Testing methodology used

Real update-available states were forced deterministically by building a disposable test image and pushing two versions under the same tag to a personal Docker Hub repo (`bdelima/test-discovery`), using an isolated `docker buildx` builder (`--driver docker-container`) to avoid corrupting the local image cache/tag on the host running the test container — a real gotcha hit early in testing (`docker build`/default-driver `buildx build --push` both clobber the local `:latest` tag on whichever host runs them, causing the exact "dangling tag / SHA hash" symptom this whole system exists to detect).
