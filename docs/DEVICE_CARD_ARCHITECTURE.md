# Device card architecture

Status: v3 baseline implemented; breaking changes are allowed.

## Implemented baseline

- `GET /api/v3/devices` returns card profiles and opaque card links.
- `GET /api/v3/devices/{device}/card` returns the initial ordered block projection.
- `GET /api/v3/devices/{device}/card/blocks/{block}` refreshes only one visible block.
- `POST /api/v3/devices/{device}/actions/{action}` executes an advertised action and returns a new projection.
- `/api/v3/auth/...` owns login, Google login, and logout for the current client.
- Android renders `device_overview`, `dynamic_form`, `history`, `operation_queue`, and `progress` through a native control registry.
- In the plant profile, the operation queue is rendered after the expandable history/control content and before overview statistics.
- Android has no operation models, operation endpoints, status interpretation, recovery loop, or operation polling.
- Overview statistics are cached server-side for five minutes; per-block polling does not reload history.
- `/healthz` and `/api/v2/app/...` release metadata/download routes remain compatible.
- No device, operation, or authentication endpoints remain under v2.

The current implementation uses block polling. ETag support, streaming, and persistent
materialized card projections are intentionally deferred until polling becomes a
measured bottleneck.

## MCU availability and data source

A process-local `DevicePresenceMonitor` starts in the FastAPI lifespan, immediately
checks every registered device, and repeats the cycle every five seconds. Health
checks use a dedicated client with a 200 ms timeout and bounded concurrency. Their
`unknown`, `online`, or `offline` results are stored in the thread-safe runtime
`DevicePresenceRegistry`.

Card and block requests never perform a health probe. If runtime presence is online,
the resolver requests current MCU state for the projection. A failed live-state
request immediately marks the device offline and falls back to the latest stored
snapshot; an already-offline device uses the snapshot without contacting the MCU.
The next monitor cycle is responsible for bringing it online again. Overview data
exposes `source` as `live`, `snapshot`, or `none`. The Android client never owns or
infers connectivity state.

The overview projection includes `snapshot_at` only when the MCU is offline and the
displayed values come from a stored snapshot. Android additionally renders that
timestamp only for the explicit `offline` status.

## Goal

The Android client asks for a device and its card. It does not inspect operation queues, infer workflow state, recover running operations, or know backend operation status names. The backend exposes a ready-to-render semantic read model and owns all device state machines.

The design is deliberately split between:

- a native client layout for each known card profile (`plant`, `tank`, and future profiles);
- server-driven blocks, fields, values, actions, links, and refresh policies inside that layout.

The client is not a generic browser for arbitrary server-defined UI. It supports a versioned vocabulary of block and field types and fails safely when it receives an unsupported required component.

## Responsibility boundary

### Backend naming

The implementation uses names that expose the architectural role of a method:

- `project_*` builds a client-facing read model without implying an HTTP response;
- `request_*` performs direct MCU I/O;
- `load_*` and `find_*` read backend persistence or queue state;
- `execute_*`, `delete_*`, and `set_*` perform commands;
- `require_*` and `validate_*` reject invalid action payloads;
- `advertised_action_request` builds an opaque action descriptor for the client.

`DeviceStateProjectionService` owns MCU/snapshot normalization and supporting
statistics/history projections. `DeviceCardService` composes those projections into
cards and blocks. Runtime wiring exposes them as `device_state` and `cards`.

### Backend owns

- device and command state machines;
- queue ordering, retries, timeouts, cancellation, and recovery;
- mapping low-level operations and events to user-facing device state;
- which actions are available and enabled;
- field definitions, validation constraints, defaults, and current values;
- block data sources and submission targets;
- refresh policy for each block;
- a monotonically increasing card or block revision.

### Client owns

- native visual layout for a known `card_profile`;
- rendering supported semantic block and field types;
- local form drafts and client-side validation copied from the schema;
- following server-provided relative links through one generic API executor;
- scheduling refreshes only while the relevant card/block is visible;
- stopping refreshes when the lifecycle owner is not active.

The client must not contain `isFinalOperationStatus`, operation-type filters, operation recovery loops, or per-command workflow logic.

## API shape

### 1. Device discovery

```http
GET /api/v3/devices
```

```json
{
  "devices": [
    {
      "id": "avocado",
      "name": "Avocado",
      "device_type": "plant",
      "card_profile": "plant.v1",
      "card_href": "/api/v3/devices/avocado/card"
    }
  ]
}
```

`card_profile` selects a native layout known to the client. `card_href` is opaque to feature code and is followed by the generic API layer.

### 2. Card manifest

```http
GET /api/v3/devices/avocado/card
```

The response describes ordered blocks and returns enough initial data to render without an N+1 request burst:

```json
{
  "device_id": "avocado",
  "profile": "plant.v1",
  "schema_version": 1,
  "revision": 184,
  "blocks": [
    {
      "id": "overview",
      "kind": "device_overview",
      "slot": "primary",
      "required": true,
      "data": {
        "title": "Avocado",
        "status": {"code": "online", "label": "Online", "severity": "success"},
        "primary_value": {"value": 1240, "unit": "g", "label": "Weight"},
        "snapshot_at": "2026-08-24T12:00:00Z",
        "statistics": []
      },
      "actions": [],
      "refresh": {
        "mode": "poll",
        "interval_ms": 5000,
        "href": "/api/v3/devices/avocado/card/blocks/overview",
        "etag": "overview-184"
      }
    },
    {
      "id": "control",
      "kind": "dynamic_form",
      "slot": "control",
      "required": true,
      "schema": {},
      "data": {},
      "refresh": {
        "mode": "on_open",
        "href": "/api/v3/devices/avocado/card/blocks/control"
      }
    },
    {
      "id": "watering_parameters",
      "kind": "dynamic_form",
      "slot": "watering_parameters",
      "required": true,
      "schema": {},
      "data": {},
      "refresh": {
        "mode": "on_open",
        "href": "/api/v3/devices/avocado/card/blocks/watering_parameters"
      }
    },
    {
      "id": "watering_history",
      "kind": "history",
      "slot": "history",
      "required": false,
      "data": {},
      "refresh": {
        "mode": "once",
        "href": "/api/v3/devices/avocado/card/blocks/watering_history"
      }
    }
  ]
}
```

The initial card response contains the live summary plus the schemas and descriptors
of closed blocks. Data for `on_open` and `once` blocks is deliberately empty and is
loaded from `refresh.href` only when the user opens the block. A `once` block is
loaded once per client session; an `on_open` block is reloaded on every opening.
Polling is active only for always-visible blocks and for the currently open block.

## Block model

Every block has:

- `id`: stable identifier within a card;
- `kind`: semantic renderer type understood by the client;
- `slot`: placement in the native profile layout;
- `required`: whether lack of client support makes the card incompatible;
- optional `schema`: form or table definition;
- optional `data`: current read model;
- optional `actions`: currently available affordances;
- `refresh`: lifecycle and caching policy.

Initial block kinds:

- `device_overview`: name, connectivity, primary value, data timestamp, and statistics;
- `dynamic_form`: control and parameter forms;
- `history`: paginated watering history;
- `operation_queue`: active backend operations for one device, each with its own advertised cancel action;
- `message`: warnings and errors that do not belong to a form;
- `progress`: a long-running device workflow rendered from backend state.

Control and watering parameters are separate block instances but may share the `dynamic_form` renderer.

## Dynamic form schema

Example control block:

```json
{
  "id": "control",
  "kind": "dynamic_form",
  "slot": "control",
  "title": "Control",
  "schema": {
    "controls": [
      {
        "kind": "field",
        "id": "target_g",
        "label": "Target amount",
        "control_type": "number_input.v1",
        "value_type": "decimal",
        "default": 100,
        "constraints": {"min": 1, "max": 1000, "step": 1},
        "unit": "g"
      },
      {
        "kind": "field",
        "id": "mode",
        "label": "Mode",
        "control_type": "select.v1",
        "value_type": "string",
        "default": "normal",
        "options": [
          {"value": "normal", "label": "Normal"},
          {"value": "slow", "label": "Slow"}
        ]
      },
      {
        "kind": "action",
        "id": "sleep_enabled",
        "label": "Sleep mode",
        "control_type": "action_toggle.v1",
        "value_type": "boolean",
        "enabled": true,
        "request": {
          "method": "POST",
          "href": "/api/v3/devices/avocado/actions/set-sleep",
          "body": {
            "binding": "control_value",
            "property": "enabled"
          }
        }
      },
      {
        "kind": "field",
        "id": "sleep_interval_minutes",
        "label": "Sleep interval",
        "control_type": "number_input.v1",
        "value_type": "integer",
        "default": 30,
        "constraints": {"min": 1, "max": 1440},
        "unit": "min",
        "commit": {
          "mode": "button",
          "label": "Set interval",
          "request": {
            "method": "POST",
            "href": "/api/v3/devices/avocado/actions/set-sleep-interval",
            "body": {
              "binding": "control_value",
              "property": "minutes"
            }
          }
        }
      },
      {
        "kind": "action",
        "id": "capture_zero",
        "label": "Capture zero",
        "control_type": "hold_action.v1",
        "preset": "zero_capture_hold.v1",
        "enabled": true,
        "request": {
          "method": "POST",
          "href": "/api/v3/devices/avocado/actions/capture-zero",
          "body": {"binding": "none"}
        }
      }
    ]
  },
  "data": {
    "values": {
      "target_g": 100,
      "mode": "normal",
      "sleep_enabled": false,
      "sleep_interval_minutes": 30
    }
  },
  "refresh": {"mode": "on_open"}
}
```

The `controls` array is ordered and contains independent `field` and `action` elements. Every element declares a versioned `control_type` resolved through the native client control registry. This lets the backend select a known native control and place it correctly without describing Compose layout or animation details.

Initial control types:

- `text_input.v1`;
- `number_input.v1`;
- `select.v1`;
- `toggle.v1` for a locally edited boolean field;
- `action_toggle.v1` for an immediately submitted boolean command;
- `slider.v1`;
- `readonly.v1`;
- `button.v1`;
- `hold_action.v1` for a press-and-hold command.

Initial value types:

- `string`;
- `integer`;
- `decimal`;
- `boolean`;
- `duration`;
- `timestamp`.

Every action inside a block has its own request and body binding. Actions do not implicitly submit the block. The sleep toggle submits only its new boolean value. The sleep interval input owns a separate commit button which submits only that input. The capture-zero control submits no form values and uses a client-defined hold preset. A Save action may still explicitly bind several named fields. Updating one control must not accidentally submit unrelated draft values from the block.

### Native control registry and presets

The Android client maintains a registry keyed by `control_type` and optional `preset`:

```text
number_input.v1          -> native numeric input renderer
action_toggle.v1         -> native switch with pending/error/revert behaviour
hold_action.v1           -> native press-and-hold button renderer
zero_capture_hold.v1     -> two-second hold, progress animation, haptic feedback
```

`id` identifies the semantic control instance agreed with the backend (`sleep_enabled`, `sleep_interval_minutes`, `capture_zero`). `control_type` selects reusable behaviour. `preset` selects a fully client-defined UX configuration. The server must not send animation frames, colours, gesture code, or arbitrary duration scripts.

For `hold_action.v1`, the client completes the HTTP request only after the preset's hold gesture succeeds. Releasing early performs no request. While the request is pending, the control disables itself; the backend response supplies the new block state or a structured error.

Presets are versioned contract identifiers. Changing the behaviour of an existing preset incompatibly requires a new identifier. An unknown required `control_type` or `preset` produces the explicit client-update-required state; an unknown optional control may be omitted.

## Links and client binding

Feature code must not hardcode endpoint paths, and the server must not send Kotlin handler names. The binding is an HTTP affordance included in the card:

```json
{
  "request": {
    "method": "POST",
    "href": "/api/v3/devices/avocado/actions/start-watering",
    "body": {"binding": "fields", "fields": ["target_g", "mode"]}
  }
}
```

The generic executor supports a deliberately small protocol:

- same-origin relative `href` only;
- allowed methods: `GET`, `POST`, `PUT`, `PATCH`, `DELETE`;
- body bindings: `none`, `control_value`, `fields`, `selected_item`, and `literal`;
- standard response: an updated block, updated card, or accepted asynchronous transition.

This is preferable to client URL hardcoding because routes can change without changing feature code. It is preferable to arbitrary executable server instructions because the client retains a small, auditable protocol.

For safety, the client rejects absolute URLs, unknown methods, unknown bindings, and unsupported required components.

## Refresh policies

Refresh is declared per block:

```json
{"mode": "none"}
{"mode": "once"}
{"mode": "on_open"}
{"mode": "manual"}
{"mode": "poll", "interval_ms": 5000, "href": "...", "etag": "..."}
{"mode": "stream", "href": "/api/v3/devices/avocado/card/events"}
```

Semantics:

- `none`: embedded data is immutable for this card lifetime;
- `once`: load once when the block first becomes visible;
- `on_open`: refresh whenever the card becomes active;
- `manual`: only an explicit user action refreshes it;
- `poll`: poll only while visible, honouring the server interval and ETag;
- `stream`: subscribe while visible and apply block/card updates by revision.

Recommended initial policies:

- overview: `poll`;
- control: `on_open`, with action responses replacing its state;
- watering parameters: `on_open`, with save response replacing its state;
- watering history: `once`, paginated on demand, and invalidated by a completed watering event.

The server may return a changed refresh policy with any block response. The client must enforce reasonable global minimum and maximum polling intervals to protect itself from a bad manifest.

## State projection

Commands submitted through block actions enter the backend queue. Workers and MCU
callbacks update operation records; `DeviceCardService` projects those internal
records and the current MCU/snapshot data into user-facing states such as `idle`,
`watering_queued`, `watering`, and `updating`. Android does not translate backend
operation types or lifecycle statuses.

Every action currently returns the complete updated card:

```json
{
  "accepted": true,
  "card": {
    "device_id": "fikus",
    "profile": "plant.v1",
    "schema_version": 1,
    "revision": 185,
    "blocks": []
  }
}
```

Queue item IDs may appear as opaque values inside server-advertised cancel requests.
The client transports them unchanged and never interprets or tracks their lifecycle.

## Versioning and caching

- `profile` versions native composition, for example `plant.v1`.
- `schema_version` versions the block vocabulary and manifest contract.
- `revision` orders card updates and prevents stale responses from replacing newer state.
- Block GET endpoints should support `ETag` and `If-None-Match`.
- Form schemas may be referenced by a cacheable `schema_href` when many devices share the same schema. Start with embedded schemas; introduce references only when payload size justifies the complexity.

## Failure behaviour

- Unsupported optional block: omit it and log telemetry.
- Unsupported required block or newer incompatible schema: show an explicit client-update-required state.
- Failed block request: show an error inside that block; do not destroy the whole card.
- Failed action: return a structured error and, when possible, the latest block/card revision.
- Out-of-order response: discard it when its revision is older than the rendered revision.

## Current implementation boundary

The replacement is complete. The HTTP layer contains only authentication, Android
release delivery, and the v3 card router. Device/operation legacy routers and their
response adapters have been deleted. Queue records remain an internal backend model
and are exposed to Android only as semantic `operation_queue` block items with an
advertised per-item cancel action.

The checked-in `smart_watering/public_api.openapi.yaml` is generated from the
registered FastAPI routes. A regression test compares it with `/openapi.json`, so a
route change must update the schema in the same change.
