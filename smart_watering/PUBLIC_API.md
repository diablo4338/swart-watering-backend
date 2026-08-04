# Smart Watering Public API

This document describes the current mobile-client API. The public API exposes only `/api/v2/*`; `/api/v1/*` is not supported.

Protected endpoints require `Authorization: Bearer <jwt>`. Tokens are HS256 JWTs backed by active SQLite sessions.

OpenAPI schema: `smart_watering/public_api.openapi.yaml`

The HTTP application uses FastAPI and is split into `public_api_app` modules:

- `main.py` creates the application and installs middleware/error handlers.
- `dependencies.py` contains reusable authentication/runtime dependencies.
- `routers/` groups auth, device, watering, and operation routes by domain.
- `config.py` loads and validates environment-backed settings.
- `security.py` owns JWT and Google identity verification.
- `service.py` and `statistics.py` contain control-plane and Prometheus logic.
- `runtime.py` wires settings, storage, and services together.
- `asgi.py` exposes the application object used by Uvicorn.

## Authentication

### `POST /api/v2/auth/login`

Request:

```json
{"username":"mobile-app","password":"secret"}
```

Response `200`:

```json
{"token":"<jwt>","expires_at":1782750000.0}
```

### `POST /api/v2/auth/google`

Request:

```json
{"id_token":"<google-id-token>"}
```

Response is the same as password login. The API server validates the token against `SMART_WATERING_GOOGLE_WEB_CLIENT_ID` and the configured email/domain allowlist.

### `POST /api/v2/auth/logout`

Revokes the current session.

Response `200`:

```json
{"status":"logged_out"}
```

## Devices

### `GET /api/v2/devices`

Response `200`:

```json
{"devices":[{"name":"plant_1","type":"plant","has_pending_operations":true},{"name":"main_tank","type":"tank","has_pending_operations":false}]}
```

`has_pending_operations` is a lightweight flag for non-terminal device-control
commands. Operation details are not included in this response.

### `GET /api/v2/device-types`

Returns the device types accepted by configuration commands. Clients should use
this endpoint to populate selectors instead of hard-coding the values.

```json
{"types":["plant","tank"]}
```

### `GET /api/v2/devices/{device}/status/latest`

Returns the latest successful queued status snapshot. Snapshot data is never
read directly from the ESP. Because a stored snapshot does not prove current
connectivity, `status` is `unknown`. If no successful snapshot
exists, `available` is `false`.

Response `200`:

```json
{
  "device": "plant_1",
  "status": "unknown",
  "source": "snapshot",
  "available": true,
  "result": {
    "device": {"name": "plant_1", "type": "plant"},
    "watering": {
      "active": false,
      "state": "waiting",
      "last_operation_type": null,
      "last_operation_status": null
    },
    "config": {
      "target_g": null,
      "dry_weight_g": 120.0,
      "tare_weight_g": 450.0,
      "sleep_disabled": false,
      "sleep_interval_min": 15
    },
    "weight": {"gross_weight_g": 520.0, "useful_weight_g": 70.0, "water_used_g": null}
  },
  "result_received_at": 1782750010.0,
  "operation_id": "status-snapshot-operation-id",
  "pending_operation_id": null,
  "pending_operation_status": null,
  "error": null
}
```

When no snapshot is available:

```json
{
  "device": "plant_1",
  "status": "offline",
  "source": "none",
  "available": false,
  "result": null,
  "result_received_at": null,
  "operation_id": null,
  "pending_operation_id": null,
  "pending_operation_status": null,
  "error": {
    "code": "device_status_snapshot_not_found",
    "message": "no stored status snapshot exists for device 'plant_1'",
    "retryable": true
  }
}
```

### `GET /api/v2/devices/{device}/status/live`

Returns a direct live controller read from the ESP `/watering` endpoint. This
route does not fall back to the latest stored snapshot. If the device is
sleeping or unreachable, `available` is `false` and `source` is `none`.

The response shape matches `status/latest`.

### `POST /api/v2/devices/{device}/status`

Queues an asynchronous `/watering` snapshot read for sleeping devices.

Response `202`:

```json
{
  "operation_id": "6fbeb936-d95b-4c14-b3c2-4a1f2261a2b1",
  "device": "plant_1",
  "type": "device_status",
  "status": "queued",
  "updated_at": 1782750010.0,
  "finished_at": null
}
```

## Device control

All device-control writes are asynchronous. A `202` response contains an
operation document. While its status is `queued` or `sending`, the requested
values have not yet been confirmed by the controller.

### `POST /api/v2/devices/{device}/config`

Queues a configuration update. Supported fields are `device_type`, `name`,
`dry_weight_g`, and `tare_weight_g`; at least one field is required.

```json
{"device_type":"plant","name":"plant_1","dry_weight_g":120,"tare_weight_g":450}
```

The operation response exposes submitted values as top-level fields so clients
can render pending values beside the current controller values.

### `POST /api/v2/devices/{device}/sleep/enable`

Queues enabling deep sleep. An empty JSON object may be sent.

### `POST /api/v2/devices/{device}/sleep/disable`

Queues disabling deep sleep. An empty JSON object may be sent.

### `POST /api/v2/devices/{device}/sleep/interval`

Queues a sleep interval update. Valid values are `1..50` minutes.

```json
{"minutes":15}
```

### `POST /api/v2/devices/{device}/zero`

Queues capture of the current scale reading as zero.

### `POST /api/v2/devices/{device}/calibration`

Queues scale calibration using a known positive weight.

```json
{"weight_g":500}
```

### `POST /api/v2/devices/{device}/queue/clear`

Cancels and removes commands that are still in the command queue.

```json
{"cleared":2}
```

## Watering

### `GET /api/v2/devices/{device}/water-consumption`

Returns plant water-consumption periods for the current application day and the previous
six dates. The application day starts at 08:00. Day is
08:00–20:00; night is 20:00 on the displayed date through 08:00 on the next date. An active period is
calculated from its start through the current time. A future period, or a period without
Prometheus samples, is returned as `null`.
Values are calculated from the real `gross_weight_g` firmware metric.
When a device has no sample exactly at a boundary, the first and last available samples
inside the period are used.
The signed weight change (`last - first`) is divided by the number of elapsed hours in the
period. A positive value means that the plant weight increased. Completed periods use 12
hours; an active period uses the actual time from its start through now.
Prometheus defaults to `http://127.0.0.1:9090` and can be overridden with
`SMART_WATERING_PROMETHEUS_URL`.
The latest completed day or night period is compared with the median of earlier completed
periods of the same type. The calculation window is controlled by
`SMART_WATERING_CONSUMPTION_MEDIAN_DAYS` (default: `5`). Its
`*_below_weekly_median` flag is set
when consumption is at least `SMART_WATERING_CONSUMPTION_DROP_THRESHOLD_PERCENT`
percent lower (default: `30`).

```json
{
  "device": "main_tank",
  "days": [
    {"date": "2026-07-26", "day": null, "night": 18.25},
    {"date": "2026-07-25", "day": 42.5, "night": 11.75},
    {"date": "2026-07-24", "day": 37.0, "night": 9.25}
  ]
}
```

### `GET /api/v2/devices/{device}/watering/status`

Returns a normalized direct-live status for the tank UI.

Response `200`:

```json
{
  "device": {"name": "main_tank", "type": "tank"},
  "active": true,
  "state": "watering",
  "gap_g": 75.0,
  "percent_complete": 25.0,
  "last_operation": {"type": "start", "status": "in_progress"},
  "source": "live",
  "available": true,
  "result_received_at": 1782750010.0,
  "operation_id": null,
  "pending_operation_id": null,
  "pending_operation_status": null,
  "planned_watering": null,
  "result": {
    "device": {"name": "main_tank", "type": "tank"},
    "watering": {
      "active": true,
      "state": "watering",
      "last_operation_type": "start",
      "last_operation_status": "in_progress"
    },
    "config": {"target_g": 100.0, "dry_weight_g": null, "tare_weight_g": 450.0},
    "weight": {"gross_weight_g": 500.0, "useful_weight_g": null, "water_used_g": 25.0}
  }
}
```

If a watering start command is queued or being sent, `planned_watering` is:

```json
{"operation_id":"2d80e2fb-2c89-4c10-9667-63d8c42f9c6a","target_g":200.0,"status":"queued"}
```

### `GET /api/v2/devices/{device}/watering/last`

Returns the latest terminal watering start operation, regardless of recent non-watering operations.

Response `200`:

```json
{
  "operation": {
    "operation_id": "2d80e2fb-2c89-4c10-9667-63d8c42f9c6a",
    "device": "main_tank",
    "type": "watering_start",
    "status": "success",
    "target_g": 200.0,
    "updated_at": 1782750100.0,
    "finished_at": 1782750100.0
  }
}
```

If no terminal watering exists:

```json
{"operation":null}
```

### `POST /api/v2/devices/{device}/watering/start`

Request:

```json
{"target_g":200}
```

Response `202` is an operation document:

```json
{
  "operation_id": "2d80e2fb-2c89-4c10-9667-63d8c42f9c6a",
  "device": "main_tank",
  "type": "watering_start",
  "status": "queued",
  "target_g": 200.0,
  "updated_at": 1782750010.0,
  "finished_at": null
}
```

### `POST /api/v2/devices/{device}/watering/stop`

Queues a stop command and cancels queued watering start commands for the device.

Request:

```json
{}
```

Response `202`:

```json
{
  "operation_id": "9abcc3ea-eabe-4e69-a4d6-604d38e5f57e",
  "device": "main_tank",
  "type": "watering_stop",
  "status": "queued",
  "updated_at": 1782750015.0,
  "finished_at": null
}
```

## Operations

### `GET /api/v2/devices/{device}/operations`

Returns recent operations for one device. Add `?active=true` to return every
non-terminal operation for that device. The active form is not limited to the
recent-operation window and is intended for restoring client-side command
queues after an application restart.

```json
{"operations":[{"operation_id":"...","device":"main_tank","type":"watering_start","status":"queued","target_g":200.0,"updated_at":1782750010.0,"finished_at":null}]}
```

### `GET /api/v2/operations/{operation_id}`

Returns one compact operation document.

Terminal errors include an `error` object:

```json
{
  "operation_id": "2d80e2fb-2c89-4c10-9667-63d8c42f9c6a",
  "device": "main_tank",
  "type": "watering_start",
  "status": "error",
  "target_g": 200.0,
  "updated_at": 1782750100.0,
  "finished_at": 1782750100.0,
  "error": {"code":"error","message":"weight_not_changing","detail":"weight_not_changing","retryable":true}
}
```

### `GET /api/v2/operations/{operation_id}/events`

Response `200`:

```json
{"operation_id":"2d80e2fb-2c89-4c10-9667-63d8c42f9c6a","events":[{"status":"queued","message":"operation queued"},{"status":"sending","message":"worker picked operation"}]}
```

## Client Flow

1. `POST /api/v2/auth/login` or `POST /api/v2/auth/google`
2. Store `token` and `expires_at`
3. `GET /api/v2/devices` and `GET /api/v2/device-types`
4. Plants: poll `GET /api/v2/devices/{device}/status/latest`
5. Tanks: poll `GET /api/v2/devices/{device}/watering/status` and `GET /api/v2/devices/{device}/watering/last`
6. If `source` is `none` and `pending_operation_id` is null, call `POST /api/v2/devices/{device}/status`
7. Use the device-control endpoints for configuration, sleep, zero, calibration, and queue clearing
8. `POST /api/v2/devices/{device}/watering/start` for tank watering
9. Poll `GET /api/v2/operations/{operation_id}` until final status
10. `POST /api/v2/devices/{device}/watering/stop` to stop or cancel watering
11. `POST /api/v2/auth/logout` when signing out

Final operation statuses are `success`, `error`, `timeout`, and `cancelled`.
