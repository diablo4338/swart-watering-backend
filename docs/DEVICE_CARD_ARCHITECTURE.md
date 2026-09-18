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
- Hourly water consumption queries Prometheus on a 60-second grid but accepts intervals up to and including one hour between valid weights: both their elapsed time and weight change are included. Longer gaps contribute neither time nor weight change; the first valid weight after such a gap starts a new baseline. Invalid/nonpositive weights are skipped, and the gap is measured between the surrounding valid points. Periods without a usable pair return `null`, while observed unchanged weight returns zero. The backend owns this calculation; clients render the supplied values.
- The MCU rounds Prometheus weights to whole grams and may repeat a weight between sensor updates. Consumption is accumulated in one-hour windows within each contiguous segment, then checked against 25 g/hour. A trailing partial window uses its actual duration. Windows above the limit contribute neither consumption nor duration; if none are usable, the rate is `null`. Drops are measured against the consumption baseline, never the preceding noisy sample: a return from a small positive spike can still establish a new minimum. The separate abrupt-drop guard rejects a baseline loss above 25 g (scaled for sample intervals longer than an hour), without extrapolating minute changes into hourly rates. A rejected spike resets the baseline and contributes no consumption; its observed interval remains in the window duration. Gaps reset both the window and baseline.
- Before consumption accumulation, the backend interpolates confirmed temporary scale excursions: an abrupt departure above 10 g and up to 30 g (covering +/-15 g noise), followed within five minutes by a return to the preceding weight trend. Return tolerance is 2 g plus the permitted 25 g/hour downward trend. Interpolation preserves timestamps and requires consecutive samples no more than 60 seconds apart. Missing data, persistent shifts, larger jumps, and excursions without a confirmed return are left to the existing guards. This filter applies only to consumption, not watering-event detection.
- Water-consumption statistics use one backend calculation and one device-ID-keyed cache for both views. The overview serializes only `date`, `day`, `night`, and the two median flags per row. Full cards (including action responses) advertise an empty `consumption_analysis` block with slot `statistics` and `refresh.mode = on_open`. Selecting `diagnostic` requests its server-provided `/api/v3/devices/{device_id}/card/blocks/consumption_analysis` URL; this response includes `analysis`, `day_analysis`, and (once started) `night_analysis` from the same cached rows. Minimal serialization never mutates that cache. Each cache refresh makes exactly one Prometheus range request, from the oldest required period (including the median lookback) to a single captured `now`. All period slices, rates, medians, endpoint differences, and diagnostics are computed locally from that immutable response. Adjacent periods share the same boundary measurement. Concurrent requests for the same device share the in-flight refresh through a per-device lock. The detailed block returns all seven days and their intervals in one response, without pagination or per-day requests; expanding days is local UI state. Both responses expose the same `snapshot_at` cutoff, and the detailed screen displays its `snapshot_label`. The detailed block does not poll, so its loaded days remain one snapshot until the user selects it again. The detailed view introduces no second calculation algorithm or extra Prometheus queries while the common cache is valid. A day retains the existing local 08:00–08:00 boundary, capped at now for the active day. Daily averages combine the ready-made day/night rates, weighted by each period's counted duration plus its internal data gaps. This prevents missing daytime data from increasing the relative weight of slower nighttime consumption. Internal gaps are represented by the estimated rate of their own period, not by their unknown weight difference. Rate-limit rejections and pending rises do not increase the weight, and periods without an estimate are omitted rather than treated as zero. Partial periods use only their available elapsed span. Counted seconds, measured grams, interval decisions, and the independent diagnostic percentage formula remain unchanged; the aggregate estimate need not equal counted grams divided by counted hours. This assumes observed consumption represents missing portions of the same day/night period; existing per-period rates and forecast behavior remain unchanged.
- Before resetting its baseline for a moderate positive rise (above 10 g and up to 30 g), the average estimator also checks the rise against its own consumption baseline. With consecutive measurements at most 60 seconds apart, a return within five minutes to that baseline (2 g tolerance plus the permitted downward trend) suppresses the reset and preserves elapsed time. This catches excursions whose adjacent-sample rise is at most 10 g but whose baseline-relative rise exceeds it. Five minutes of sustained elevation retains the baseline shift; larger shifts and sparse data keep their existing treatment. A moderate rise at the snapshot tail with insufficient confirmation excludes the pending tail from both time and consumption, with an explicit interval reason, and is re-evaluated from the next snapshot. Diagnostics are unchanged and use the raw endpoints.
- Average estimation and quality evaluation are independent sibling modules: `consumption_average.py` owns the production estimator and combination of period averages; `consumption_diagnostics.py` evaluates a supplied result against raw samples. Their shared contract is `AverageConsumptionResult` in `consumption_result.py`: an explicit ready-made rate, counted time/grams, and algorithm-provided interval decisions/labels. The service runs the estimator once per period, sends the same result to the summary and evaluator, and combines daily estimates in the average module. Diagnostics never invoke the estimator or reconstruct its rate from grams/time; the percentage denominator is the supplied unrounded rate. Raw endpoint comparison remains independent of the estimator's filtering. Changing the average algorithm does not require changing diagnostic code or adding its new interval reasons there.
- Analysis includes `counted_seconds`, `total_seconds`, `consumed_g`, positive `average_rate_g_per_hour`, raw first/last valid samples with their times and weights, `sample_span_seconds`, signed `endpoint_consumed_g` (first minus last), integer `endpoint_consumed_rounded_g` for the button, `endpoint_rate_g_per_hour`, `filtered_samples`, and chronological `intervals`. Endpoints are the nearest available valid measurements inside the queried period, before smoothing; they are never extrapolated. Fewer than two distinct sample times give a null endpoint difference. Adjacent intervals with the same inclusion decision and reason are merged. Leading/trailing missing data and long gaps are excluded; an abrupt drop or weight increase can retain its elapsed time while adding zero grams, which is explicitly described by the server's `reason_label`. Timestamps and display labels use the configured statistics timezone, supplied as `timezone`.
- `agreement_percent = endpoint_rate_g_per_hour / average_rate_g_per_hour * 100`, using unrounded values and the actual time between raw endpoints. It is null when either rate is unavailable or the calculated average is zero. It is neither clamped nor converted to an absolute value: values above 100% or negative values expose divergence, including water additions. This compares two estimates, not a statistical confidence or guaranteed accuracy score; watering and gaps also affect it. The Android client places a `short / diagnostic` selector between the queue and statistics. It owns the selected mode per device and expanded-day state. Changing modes recreates the content, cancels the previous request, shows a loading indicator, and immediately fetches the selected server-provided block URL (`overview` for short, `consumption_analysis` for diagnostic). Failed loads offer Retry. The short view continues to receive overview polling updates; diagnostic retains the complete response locally without per-day requests. The analysis block is rendered here rather than in the upper menu. The client only formats/renders server-calculated values and interval decisions.
- `/healthz` and `/api/v3/app/...` expose release metadata/downloads.
- No endpoints remain under v2.

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
user-command queue, and a failed read is retried by the next snapshot cycle. A
successful discovery response is stored as the new device's first snapshot.

The device card is one projection assembled from two owners. The registry owns
backend metadata such as `devices.name`; the persisted snapshot and
`DeviceRuntimeState` own all MCU-reported fields, including `device.name`. A snapshot
replaces the base runtime state and confirmed callbacks apply patches to it. MCU state
must never be copied into `devices` or projected from registry fields. Consequently
the overview title comes from `devices.name`, while its MCU subtitle comes from the
current runtime projection's `result.device.name`.
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
stored snapshot. Android keeps those values visible but renders a client-side error
when `snapshot_at` is more than 24 hours older than the current time.

## Goal

The Android client asks for a device and its card. The backend owns all device state
machines and operation recovery. Android renders described controls without interpreting operation types or statuses.

The design is deliberately split between:

- a native client layout for each known card profile (`plant`, `tank`, and future profiles);
- server-driven blocks, fields, values, actions, links, and refresh policies inside that layout.

The client renders a fixed vocabulary of block and control types. Unknown required
block kinds show an update-required message. Control fallback behavior is described
under Failure handling and implementation limits.

## Responsibility boundary

### Manual history refresh

History exposes a plain `button.v1` in `schema.controls`, including when empty.
Its advertised `collect-statistics` action uses a `none` body binding. Pressing it
queues a scan of the previous 30 days; Android does not track the operation.
History reads stored events and reloads on opening. Overview statistics keep their
normal five-minute cache lifetime.

The path is `DeviceCardService.execute_action` →
`SmartWateringService.queue_statistics_collection` → shared `_enqueue` → `OperationLog.create` / `CommandQueue.enqueue` →
`BackgroundWorker._collect_statistics` → `PlantWateringDetector.scan_device`.
The detector reads Prometheus and upserts history through the existing repositories.
The queued command stores `device_id` and its fixed UTC period. The worker selects
the history handler by the persisted operation type `statistics_collection`, not
by the command's HTTP method or path. It performs no MCU HTTP delivery for this type.
Queue rows retain the ordinary `POST` default; transport fields are unused by the
history handler. MCU callback timeout checks likewise use the operation type.
There is no separate backend-operation dispatcher or operation-aware UI component.

Requests use the ordinary operation creation and command queue flow, including
the existing exact-command duplicate check. New presses normally create new commands
because each request captures its own current UTC end time. There is no
history-specific merging, period comparison, priority, or active-operation reuse.
History commands for the same device execute in queue order; other devices have
independent workers. Existing MCU command retries may move those commands to the tail.

The existing worker owns success, errors, deadlines, cancellation and recovery.
After a worker restart, a non-terminal history command scans its saved period again
if its deadline has not expired; it does not resume from a checkpoint. Terminal
history commands are removed without another scan. Local scans do not wait for MCU
callbacks. Cancellation during a scan does not interrupt it or roll back saved events.
Prometheus requests use a ten-second HTTP timeout. The worker checks elapsed time
before and after scanning; its deadline is not a hard interrupt of a running scan.
The button's HTTP response confirms enqueueing, not completion. Updated history is
read when the block is reopened; the button does not poll for completion.

Automatic collection is scheduled in `jobs/worker.py`: on startup and hourly it
queues a three-hour scan for each plant. `SMART_WATERING_DETECTOR_INTERVAL_SEC` and
`SMART_WATERING_DETECTOR_LOOKBACK_HOURS` configure it. This uses the same queue and
worker as the manual button, with no separate container.

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
| `overview` | latest stored MCU snapshot; binary runtime presence for connectivity; cached statistics for the statistics section; registry identity for the title | operation records and direct MCU reads |
| `control` | latest stored MCU snapshot, including MCU-confirmed callback patches; registry identity for the backend name and device fallback metadata | operation records, queue state, and direct MCU reads |
| `watering_parameters` | latest stored MCU snapshot | registry watering-setting overrides, operation records, queue state, and direct MCU reads |
| `operation_queue` | active user-visible operation records | MCU snapshots, presence, statistics, history, and direct MCU reads |
| `watering_history` | stored watering history/events | MCU snapshots, presence, operation records, and direct MCU reads |
| tank `watering` | latest stored MCU snapshot and the relevant active watering operation | unrelated operation types and direct MCU reads |

The tank `watering` block is the intentional exception to single-source projection:
its purpose is to render both observed device state and the progress/cancellation of
the current watering command. The full-card endpoint may load the union of sources
needed by its blocks, but every block builder receives only the sources allowed by
this table. A per-block endpoint loads only that block's allowed sources.

Each block response has a separate `block_revision`. History and operation queue
use response creation time; snapshot-backed blocks use source timestamps. Revisions
from different blocks are never compared. See Refresh and revisions below.

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
- per-block revisions for ordering responses.

### Client owns

- native visual layout for a known `card_profile`;
- rendering supported semantic block and field types;
- local form drafts and client-side validation copied from the schema;
- following server-provided relative links through one generic API executor;
- scheduling refreshes only while the relevant card/block is visible;
- stopping refreshes when the lifecycle owner is not active.

The client must not interpret operation statuses or filter operation types. No component owns operation recovery or per-command workflows.

`action_toggle.v1` owns generic optimistic interaction behavior. After a tap it
shows the requested value immediately, disables itself for at least the containing
block's polling interval, and waits for the action HTTP result. Poll replacements
must not overwrite its displayed value while this guard is active. It unlocks only
after both the guard interval and a successful response; a failed response restores
the previous value and shows the request error. This behavior is tied to the stable
control type, not to a block id or operation type.

## Current card contract

`GET /api/v3/devices` returns database IDs, display names, `card_profile`, and
`card_href`. IDs in URLs are `devices.id`, never display names.
`GET /api/v3/devices/{device_id}/card` returns `device_id`, `profile`,
`schema_version`, and ordered `blocks`. Closed history is initially a descriptor;
its contents and button are loaded when the block opens.

A block has `id`, `kind`, `slot`, `required`, `refresh`, and optional `title`,
`schema`, `data`, and `actions`. Android's existing `CardBlockRenderer` supports
`device_overview`, `dynamic_form`, `history`, `operation_queue`, `progress`, and
`message`. Plant cards contain overview, control, watering parameters, history,
and operation queue. Tank cards use the `watering` block, rendered as a form or
progress block according to the backend projection.

### History button: existing control, no new component

Manual history refresh reuses the existing `button.v1` branch of `ActionControl`.
`HistoryBlock` renders the server's `schema.controls` before history rows. The
button submits an empty body through the same action executor as other buttons.
It has no operation type, operation state, completion subscription, or restoration
logic. Its normal pending indicator lasts only for the action HTTP request.

Example of the actual button descriptor within a loaded history block:

```json
{
  "kind": "action",
  "id": "collect_statistics",
  "label": "Collect statistics",
  "control_type": "button.v1",
  "enabled": true,
  "request": {
    "method": "POST",
    "href": "/api/v3/devices/550e8400-e29b-41d4-a716-446655440000/actions/collect-statistics",
    "body": {"binding": "none"}
  }
}
```

History item actions also reuse existing controls: `action_toggle.v1` for the
fertilized flag, and `hold_action.v1` with `history_delete_hold.v1` for deletion.
The API returns up to 50 history items and `next_offset`; the current Android
renderer does not request further pages.

### Weight difference dialog

The watering-history block advertises a `date_time_range.v1` action labelled
`Weight difference` in `actions`, including in its initial deferred descriptor.
Android renders menu-block actions directly below their menu button, so this
outlined button appears immediately below Watering history without opening it.
It is separate from the control form and the statistics section.
Its native Android renderer opens a dialog with start/end dates and 24-hour times
(initially the previous hour). Both boundaries use the displayed phone timezone
and are sent as UTC instants. The two date selectors also allow periods spanning
midnight. Times skipped by DST are rejected; repeated times use the earlier offset.
`Request` follows the server-provided action URL and uses the existing
`control_value` binding with property `period`. Only `{period: {start, end}}` is sent.
The backend validates explicit timezones, start < end, and end <= now.

`POST /api/v3/devices/{device_id}/actions/weight-difference` performs a read-only
Prometheus query for each boundary, without queueing a device operation. It selects
`gross_weight_g{instance="<host:port>"}` through raw range selectors in the instant
query API. It selects the nearest finite raw sample on either side of each boundary;
ties choose the earlier point. Search windows expand from one hour in both directions
until a sample is found or all stored history since the Unix epoch has been covered.
There is no five-minute age limit. The user-approved Prometheus integration exception
uses `instance` derived from the registered device's delivery URL, just as existing
consumption statistics do: deployed metrics do not carry `device_id`. The service
still selects the registry record by `device_id`, and HTTP resources, response DTOs,
and client state retain that identity. Multiple matching series
are rejected as ambiguous. Missing or nonfinite endpoint weights return an error;
zero is a valid measurement. No consumption filtering, smoothing, rate calculation,
or extrapolation applies: the result is end weight minus start weight in grams.

The ordinary accepted/card action response additionally includes `result`, carrying
`device_id`, requested UTC boundaries, actual sample timestamps, both endpoint
weights, `difference_g`, and a server-rendered `message`. Android passes this
optional result through the generic completion callback and displays the message
in the dialog, with loading/error/retry states. Both actual sample timestamps are
displayed as dates and times with seconds and UTC offset in the phone timezone.
It does not calculate the difference
or persist it in card/runtime state. Editing either boundary clears the old result.

### Existing form and action controls

The current renderer uses these identifiers; this list does not propose new controls:

| Identifier | Existing rendering |
| --- | --- |
| `text_input.v1` | Text input |
| `number_input.v1` | Text input with numeric keyboard |
| `select.v1` | Selection buttons |
| `readonly.v1` | Read-only text field |
| `button.v1` | Button with pending state for its HTTP request |
| `date_time_range.v1` | Button opening a date/time interval dialog and displaying the action result |
| `action_toggle.v1` | Switch with an optimistic value and HTTP error rollback |
| `hold_action.v1` | Press-and-hold action |

Hold presets are `zero_capture_hold.v1` (2 seconds), `calibration_hold.v1`
(3 seconds), and `history_delete_hold.v1` (5 seconds). Releasing before the hold
completes sends no request. The current fallback for an unknown hold preset is
2 seconds. No haptic feedback or strict preset compatibility check is implemented.

Forms use `schema.controls` and `data.values`. A field's `commit.request` submits
its explicitly declared binding. Numeric values are parsed as integer or decimal;
boolean values are parsed as booleans, and other input is kept as text. There is
no dedicated slider, local boolean-toggle field, or duration editor. Date/time
selection is supported by the interval action dialog described above.

### Requests and responses

The current Retrofit API uses GET for card/block reads and POST for advertised
actions. `MainViewModel` passes the supplied `href` to this executor; it does not
dispatch arbitrary HTTP methods from `request.method`.

Implemented body bindings in `bindBody` are:

- `none`: empty object;
- `control_value`: one named property;
- `fields`: only declared fields, with optional property-name mapping;
- `literal`: the declared object;
- `literal_and_control_value`: the declared object plus one control value.

Unknown bindings fail. There is no `selected_item` binding. Relative action links
are supplied by the backend; the current action executor does not itself enforce
an allowlist of URL schemes or HTTP methods.

Every successful action response has `accepted` and a complete `card`. Acceptance
of a queued command is not its completion. The client replaces the returned device's
card without changing which device is currently selected. A rejected action with
`accepted: false` leaves the current card in place.

### Refresh and revisions

The backend currently advertises `on_open` and `poll`. Overview and operation queue
poll every 5 seconds; tank watering polls every 3 or 10 seconds according to its
projection. Control, watering parameters, and history refresh on opening. Android
clamps polling intervals to 2-300 seconds and polls only the active device's
always-visible blocks and currently open expandable block.

Each block response has its own `block_revision`. Android stores it under
`device_id:block.id` and rejects responses older than the last accepted revision.
History and operation queue use response creation time. Snapshot-backed blocks
use source timestamps via `_calculate_block_revision`. There is no card revision.
ETag, streaming, and referenced schemas are not implemented.

### Failure handling and implementation limits

Unknown required block kinds show an update-required message; unknown optional
block kinds are omitted. Unknown action control types show an unsupported-control
message. Unrecognized field control types currently fall back to a text input.
The document does not claim strict schema-version or preset negotiation.

Opening a block reports request errors through the existing card error handling.
Background polling failures are logged and keep the last data; authentication
failures clear the active session. Actions use the shared request handling and
optional completion callback. No history-specific error/recovery mechanism exists.

## Sources of this contract

- `smart_watering/public_api_app/card_service.py`: advertised blocks, controls,
  actions, refresh policies, and revisions.
- `client/app/src/main/java/com/smartwatering/app/ui/Screens.kt`: actual block and
  control renderers.
- `client/app/src/main/java/com/smartwatering/app/ui/MainViewModel.kt`: bindings,
  request results, visible-block refresh, and revision comparison.
- `client/app/src/main/java/com/smartwatering/app/api/ApiService.kt`: GET/POST API calls.
- `smart_watering/public_api.openapi.yaml`: schema generated from FastAPI routes;
  its regression test compares it with `/openapi.json`.
