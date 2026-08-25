# Device card architecture

Status: v3 baseline implemented; breaking changes are allowed.

## Implemented baseline

- `GET /api/v3/devices` returns immutable database IDs, display names, card profiles,
  and opaque card links. The MCU name is not an identity field in the client contract.
- `GET /api/v3/devices/{device_id}/card` returns the initial ordered block projection.
- `GET /api/v3/devices/{device_id}/card/blocks/{block}` refreshes one visible block.
- `POST /api/v3/devices/{device_id}/actions/{action}` executes an advertised action and returns a new projection.
- Every v3 device URL and response `device_id` uses immutable `devices.id`; backend
  names and MCU identifiers are never API resource identifiers.
- The same identity rule applies below HTTP: application commands, operations, queue
  rows, worker partition keys, runtime snapshots, presence and caches are keyed by
  `devices.id`. A rename therefore changes presentation only and cannot move work or
  state between keys. Names, IPs and controller identifiers are never recovery or
  correlation keys.
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
checks use a dedicated client with a one-second timeout and bounded concurrency. Their
Binary `online` or `offline` results are stored in the thread-safe runtime
`DevicePresenceRegistry`.

Card and block requests never contact the MCU. The periodic snapshot task is the
single regular source of full MCU state and stores the latest successful `/watering`
response in `device_snapshots`, keyed by `device_id`. Snapshot reads are runtime
tasks, not operations: they never enter `operations`, `operation_events`, or the
user-command queue, and a failed read is retried by the next snapshot cycle.
The public API keeps a `DeviceRuntimeState` owner for the current normalized state.
Snapshot/callback persistence revisions refresh that owner; projections read only a
serializer-validated copy. If its process-local value is missing, stale, or fails
serialization, it self-recovers from the latest successful stored snapshot before
returning data to a card builder.
When the MCU confirms a deterministic configuration command through its callback,
the immutable stored snapshot is not rewritten. The callback persists a confirmed
operation delta; `DeviceRuntimeState` applies deltas newer than its base snapshot in
order. This keeps snapshot-backed blocks current without making a callback masquerade
as a full MCU snapshot or waiting for the next snapshot cycle.
The presence monitor independently projects `online` or `offline` from its health
probes. A device is `offline` until its first successful probe. Cards read the latest
stored snapshot regardless of connectivity and without using the command queue. The
refresh button follows only that
advertised `refresh_card` action: it does not reload the catalog, prefetch blocks, or
run client-side recovery. A successful action returns the complete card projected
from the new MCU snapshot, so every block is replaced together. A failed manual read
returns `accepted: false`, and Android keeps its current card unchanged. Overview data
exposes `source` as `snapshot` or `none`. The Android client never owns or infers
connectivity state.

Connectivity and workflow are separate overview fields. Connectivity never changes
to `watering`; overview workflow is derived only from the snapshot and is therefore
either `idle` or `watering`. Queued and applying commands are represented only by
the operation queue. Snapshot tasks never appear in that queue. The overview projection
includes `snapshot_at` when the MCU is offline and displayed values come from a
stored snapshot.

## Goal

The Android client asks for a device and its card. It does not inspect operation queues, infer workflow state, recover running operations, or know backend operation status names. The backend exposes a ready-to-render semantic read model and owns all device state machines.

The design is deliberately split between:

- a native client layout for each known card profile (`plant`, `tank`, and future profiles);
- server-driven blocks, fields, values, actions, links, and refresh policies inside that layout.

The client is not a generic browser for arbitrary server-defined UI. It supports a versioned vocabulary of block and field types and fails safely when it receives an unsupported required component.

## Responsibility boundary

### Device registration safety boundary

Device registration has exactly two explicit strategies:

- `devices add <ip> <backend-name> --type <type>` creates only a backend database
  record. It never contacts or configures the MCU.
- `devices discover <ip>` queues a worker-owned, read-only `GET /watering`. The
  worker imports the MCU-provided identity, type, and supported settings. Discovery
  must never enqueue or execute `/config` or any other write request.

The worker rejects any command under a discovery identity unless it is exactly
`GET /watering` with no payload. Changing the MCU identifier is a separate explicit
action and cannot be inferred from either registration strategy.

### Block data dependency contract

Each block is a projection of explicitly allowed sources. A block must not read an
operation merely to show a newer desired value, disable a control, or synthesize a
workflow state. Until a command is reflected by a full MCU snapshot or an
MCU-confirmed patch to that snapshot, it is visible only in `operation_queue`.

| Block | Allowed data sources | Explicitly forbidden |
| --- | --- | --- |
| `overview` | latest stored MCU snapshot; binary runtime presence for connectivity; cached statistics for the statistics section; registry identity for title/subtitle | operation records and direct MCU reads |
| `control` | latest stored MCU snapshot, including MCU-confirmed callback patches; registry identity for the backend name and device fallback metadata | operation records, queue state, and direct MCU reads |
| `watering_parameters` | latest stored MCU snapshot | registry watering-setting overrides, operation records, queue state, and direct MCU reads |
| `operation_queue` | active user-visible operation records | MCU snapshots, presence, statistics, history, and direct MCU reads |
| `watering_history` | stored watering history/events | MCU snapshots, presence, operation queue, and direct MCU reads |
| tank `watering` | latest stored MCU snapshot and the relevant active watering operation | unrelated operation types and direct MCU reads |

The tank `watering` block is the intentional exception to single-source projection:
its purpose is to render both observed device state and the progress/cancellation of
the current watering command. The full-card endpoint may load the union of sources
needed by its blocks, but every block builder receives only the sources allowed by
this table. A per-block endpoint loads only that block's allowed sources.

Every independently fetched block response has its own `block_revision`. There is
no card-wide revision and revisions from different blocks are never compared.
Block revision watermarks may include source metadata that is not rendered. In
particular, overview revision includes the latest presence probe timestamp, and the
operation-queue revision includes the latest user-visible operation update even when
that update made the operation terminal and removed it from the rendered list. This
prevents the client from rejecting a disappearance or connectivity change as stale.

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

`action_toggle.v1` owns generic optimistic interaction behavior. After a tap it
shows the requested value immediately, disables itself for at least the containing
block's polling interval, and waits for the action HTTP result. Poll replacements
must not overwrite its displayed value while this guard is active. It unlocks only
after both the guard interval and a successful response; a failed response restores
the previous value and shows the request error. This behavior is tied to the stable
control type, not to a block id or operation type.

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
  "device_id": "550e8400-e29b-41d4-a716-446655440000",
  "profile": "plant.v1",
  "schema_version": 1,
  "blocks": [
    {
      "id": "overview",
      "kind": "device_overview",
      "slot": "primary",
      "required": true,
      "data": {
        "title": "Avocado",
        "status": {"code": "online", "label": "Online", "severity": "success"},
        "workflow": {"code": "idle", "label": "Idle", "severity": "success"},
        "primary_value": {"value": 1240, "unit": "g", "label": "Weight"},
        "snapshot_at": "2026-08-24T12:00:00Z",
        "statistics": []
      },
      "actions": [
        {
          "kind": "action",
          "id": "refresh_card",
          "label": "Refresh card",
          "control_type": "button.v1",
          "enabled": true,
          "request": {
            "method": "POST",
            "href": "/api/v3/devices/550e8400-e29b-41d4-a716-446655440000/actions/refresh-card",
            "body": {"binding": "none"}
          }
        }
      ],
      "refresh": {
        "mode": "poll",
        "interval_ms": 5000,
        "href": "/api/v3/devices/550e8400-e29b-41d4-a716-446655440000/card/blocks/overview",
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
        "href": "/api/v3/devices/550e8400-e29b-41d4-a716-446655440000/card/blocks/control"
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
        "href": "/api/v3/devices/550e8400-e29b-41d4-a716-446655440000/card/blocks/watering_parameters"
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
        "href": "/api/v3/devices/550e8400-e29b-41d4-a716-446655440000/card/blocks/watering_history"
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
Each block endpoint loads only the backend projections required by that block. In
particular, `operation_queue` must not load the device snapshot, and lightweight
operation projections must not load event histories that are not rendered.

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
- `on_open`: refresh whenever that expandable block is opened;
- `manual`: only an explicit user action refreshes it;
- `poll`: poll only while visible, honouring the server interval and ETag;
- `stream`: subscribe while visible and apply block/card updates by revision.

Recommended initial policies:

- overview: `poll`;
- operation queue: `poll`;
- control: `on_open`, with action responses replacing its state;
- watering parameters: `on_open`, with save response replacing its state;
- watering history: `on_open`, paginated on demand;

The server may return a changed refresh policy with any block response. The client must enforce reasonable global minimum and maximum polling intervals to protect itself from a bad manifest.

## State projection

Commands submitted through block actions enter the backend queue. Workers and MCU
callbacks update operation records. `DeviceCardService` exposes those records only
through `operation_queue`; snapshot-backed blocks do not project desired or pending
operation state. Android does not translate backend operation types or lifecycle
statuses.

Every action currently returns the complete updated card:

```json
{
  "accepted": true,
  "card": {
    "device_id": "550e8400-e29b-41d4-a716-446655440000",
    "profile": "plant.v1",
    "schema_version": 1,
    "blocks": []
  }
}
```

Queue item IDs may appear as opaque values inside server-advertised cancel requests.
The client transports them unchanged and never interprets or tracks their lifecycle.

## Versioning and caching

- `profile` versions native composition, for example `plant.v1`.
- `schema_version` versions the block vocabulary and manifest contract.
- `block_revision` orders responses of one block. Android keys it by
  `device_id:block.id` and never compares revisions belonging to different blocks.
  The polled operation-queue block uses response creation time, so removing the last
  operation still produces a newer empty response without scanning terminal history.
- Block GET endpoints should support `ETag` and `If-None-Match`.
- Form schemas may be referenced by a cacheable `schema_href` when many devices share the same schema. Start with embedded schemas; introduce references only when payload size justifies the complexity.

## Failure behaviour

- Unsupported optional block: omit it and log telemetry.
- Unsupported required block or newer incompatible schema: show an explicit client-update-required state.
- Failed block request: show an error inside that block; do not destroy the whole card.
- Failed action: return a structured error and, when possible, the latest block state.
- Out-of-order block response: discard it only when its `block_revision` is older
  than the rendered revision for the same `device_id:block.id`.

## Current implementation boundary

The replacement is complete. The HTTP layer contains only authentication, Android
release delivery, and the v3 card router. Device/operation legacy routers and their
response adapters have been deleted. Queue records remain an internal backend model
and are exposed to Android only as semantic `operation_queue` block items with an
advertised per-item cancel action.

The checked-in `smart_watering/public_api.openapi.yaml` is generated from the
registered FastAPI routes. A regression test compares it with `/openapi.json`, so a
route change must update the schema in the same change.
