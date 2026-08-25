# Smart Watering Python Module

The `smart_watering` module is the Python control plane for Smart Watering.
It provides local CLI operations, persistent SQLite state, queued device writes, operation callbacks and a public JWT-authenticated API.
Commands below assume they are run from the repository root.

Package layout:

- `application/` â€” shared business service used by CLI and HTTP API.
- `domain/` â€” domain models, repositories, operation queue, and device workers.
- `infrastructure/` â€” SQLAlchemy storage and migrations adapter.
- `interfaces/` â€” command-line interface.
- `public_api_app/` and `callback_app/` â€” ASGI HTTP interfaces.
- `jobs/` â€” worker and snapshotter process entry points.
- `migrations/` â€” Alembic migration environment and revisions.

## Installation

Runtime dependencies are listed in `requirements.txt`; test dependencies are in `requirements-dev.txt`.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r smart_watering/requirements-dev.txt
```

## Storage Node

State is stored in SQLite through SQLAlchemy and Alembic migrations.

Default database path:

```text
/tmp/smart_watering_cli/smart_watering.db
```

Override it with:

```bash
export SMART_WATERING_DB_PATH=/path/to/smart_watering.db
```

Stored data:

- `devices` - registered devices. `name` is the primary key, so device names are unique.
- `command_queue` - queued writes waiting for the worker.
- `operations` and `operation_events` - operation history and callback lifecycle.
- `users` and `user_sessions` - public API users and active JWT sessions.

On startup, the module validates the database path, creates the parent directory when possible, checks read/write access and runs migrations.

## CLI Node

Run without arguments to open the interactive menu:

```bash
python -m smart_watering
```

Device registry:

```bash
python -m smart_watering devices add 192.168.1.50 plant_1 --type plant
python -m smart_watering devices add 192.168.1.51 main_tank --type tank
python -m smart_watering devices discover 192.168.1.52
python -m smart_watering devices list
python -m smart_watering devices remove plant_1
```

Default names:

- plants: `plant_1`, `plant_2`, ...
- tank: `tank`

Device names are unique. The database enforces `devices.name` as the primary key, and the CLI rejects duplicate names instead of overwriting an existing device.
`devices add` writes only the backend database. `devices discover` queues only a
read-only `GET /watering`; it never changes the MCU name or configuration.
Only one `tank` device is allowed. Registering a new tank removes the previous tank record.

Device config:

```bash
python -m smart_watering devices config plant_1 tare=450 dry=120
python -m smart_watering devices config plant_1 device_type=tank name=main_tank
```

Device config changes are applied to the local registry only after the controller confirms the `/config` operation with
`success`. Until then, `devices list` and the public API continue to show the last confirmed device name and type.

Supported config keys:

- `device_type` or `type`: `plant` / `tank`
- `name`
- `tare_weight_g` or `tare`
- `dry_weight_g` or `dry`

Reads:

```bash
python -m smart_watering status plant_1
python -m smart_watering constants plant_1
python -m smart_watering metrics plant_1
python -m smart_watering ping plant_1
```

Queue a best-effort status snapshot:

```bash
python -m smart_watering.jobs.snapshotter --once
```

Device actions:

```bash
python -m smart_watering sleep enable plant_1
python -m smart_watering sleep disable plant_1
python -m smart_watering sleep interval plant_1 20
python -m smart_watering zero plant_1
python -m smart_watering calibration plant_1 500
```

The CLI queues the command, waits for the worker/controller result, and prints `success`, `failed`, or `cancelled`.
Use `--no-wait` to return immediately after queueing. The wait timeout defaults to `900` seconds and can be configured
with `SMART_WATERING_CLI_OPERATION_WAIT_TIMEOUT_SEC`.
When using the Docker host wrappers, set that variable before running the wrapper or in `docker/.env` to override the
CLI process timeout without recreating the CLI container.
`sleep enable` re-enables controller deep sleep.
`sleep disable` keeps the controller awake.
`sleep interval` changes the deep sleep wake interval in minutes. The CLI accepts values from 1 to 50.
`zero` captures the current-sensor zero point.
`calibration` stores a new scale coefficient from the current raw value, saved zero point and known weight in grams.

Watering commands:

```bash
python -m smart_watering fill main_tank 200
python -m smart_watering stop main_tank
```

`fill` starts watering for a registered `tank` device with the target poured mass in grams. The CLI waits until the
controller reports the final operation result. Duplicate queued `fill` commands with the same tank and target are
deduplicated and tracked as the existing operation.
`stop` cancels active and queued watering starts for the tank, drops queued start commands, and queues watering stop.
When the controller is reachable and watering is active, the stop command is delivered to `/watering/stop` and requests pump stop.
When watering is not active, stop succeeds as a no-op with detail `no active watering`.

Operation history:

```bash
python -m smart_watering pending
python -m smart_watering queue clear plant_1
python -m smart_watering operations
python -m smart_watering operations <operation-id>
```

Public API users:

```bash
python -m smart_watering users add mobile-app
python -m smart_watering users list
python -m smart_watering users drop mobile-app
```

`users add` prompts for the password when `--password` is omitted.
`users drop` also removes active sessions for that user.

## Worker Node

The worker drains queued writes from SQLite and sends them to devices.

```bash
python -m smart_watering.jobs.worker
```

Writes are queued and retried because firmware may be online only for a short wake window.
The worker processes devices independently, so one sleeping device does not block commands for another device.
Watering start and sleep disable commands are retried. Other commands and status reads are attempted once, then marked
as `timeout` or `error` so they do not block later work for the same device.
The queue polling interval defaults to `1` second. Retry interval defaults to `5` seconds. The worker
also waits up to `900` seconds for retryable command delivery and for final controller callbacks after a device accepts a
command. These can be configured with:

```bash
export SMART_WATERING_WORKER_IDLE_INTERVAL_SEC=1
export SMART_WATERING_WORKER_RETRY_INTERVAL_SEC=5
export SMART_WATERING_WORKER_MAX_WAIT_SEC=900
```

## Snapshotter Node

The snapshotter periodically queues `device_status` reads for all registered devices.
The worker performs the actual `/watering` requests and stores successful responses
as operation results. The v3 card resolver consumes these snapshots internally when
runtime presence is offline; snapshots are not exposed through a dedicated route.

```bash
python -m smart_watering.jobs.snapshotter --interval-sec 300
```

The interval defaults to `300` seconds and can be configured with:

```bash
export SMART_WATERING_SNAPSHOT_INTERVAL_SEC=300
```

Queued status reads are deduplicated, so an offline device keeps at most one pending `/watering` snapshot command while the worker is retrying it.

## Callback Node

The callback node records best-effort operation callbacks from devices.

```bash
uvicorn smart_watering.callback_app.asgi:app --host 0.0.0.0 --port 8080 --no-access-log
```

The CLI sends `operation_id` and `callback_url` with write requests. The callback URL comes from `SMART_WATERING_NODE_URL` when set. Otherwise the module tries to detect the local IP and uses port `8080`.

Operation lifecycle:

- `queued`: the command is stored and waiting for the worker.
- `sending`: the worker picked the command and is trying to send it.
- `accepted`: the controller accepted the command.
- `running`: the controller reported that the operation is executing.
- `success`: the operation finished successfully.
- `error`: the operation failed.
- `timeout`: the worker could not reach the controller within the retry window.
- `cancelled`: a queued watering start was cancelled by a stop command.

Final callback details include values such as `target_reached`, `stop_requested`, `weight_not_changing` or `no_weight_available`.

## Public API Node

The mobile client uses `/api/v3/auth/...` and the server-driven `/api/v3/devices/...`
card contract. Device controls and operation queues are projected as card blocks and
advertised actions; the client does not call operation-oriented routes.

Only Android update compatibility remains under v2:

- `GET /api/v2/app/latest`
- `GET /api/v2/app/releases/{version_code}/download`

All protected `/api/v3/*` endpoints require a JWT bearer token signed with `HS256`.
Users and active sessions are stored in SQLite.
JWT session lifetime is controlled by `SMART_WATERING_PUBLIC_API_SESSION_TTL_SEC` and defaults to one hour.
Detailed client documentation is available in `smart_watering/PUBLIC_API.md`.
The generated OpenAPI schema is available in `smart_watering/public_api.openapi.yaml`;
tests require it to match the routes registered by FastAPI.

Set a long random secret:

```bash
export SMART_WATERING_PUBLIC_API_JWT_SECRET=replace-with-a-long-random-secret
export SMART_WATERING_PUBLIC_API_SESSION_TTL_SEC=3600
```

For Google Sign-In, create a Google OAuth Web client and set its client ID on the API server:

```bash
export SMART_WATERING_GOOGLE_WEB_CLIENT_ID=your-web-client-id.apps.googleusercontent.com
export SMART_WATERING_GOOGLE_ALLOWED_EMAILS=user@example.com
# Optional, for Workspace domains:
# export SMART_WATERING_GOOGLE_ALLOWED_DOMAINS=example.com
```

Run the server:

```bash
uvicorn smart_watering.public_api_app.asgi:app --host 0.0.0.0 --port 8081
```

Current API routes:

```text
POST /api/v3/auth/login
POST /api/v3/auth/google
POST /api/v3/auth/logout
GET  /api/v3/devices
GET  /api/v3/devices/<device>/card
GET  /api/v3/devices/<device>/card/blocks/<block>
POST /api/v3/devices/<device>/actions/<action>
GET  /api/v2/app/latest
GET  /api/v2/app/releases/<version_code>/download
```

Only the two Android release routes remain under v2. See `PUBLIC_API.md` and
`public_api.openapi.yaml` for the current contract.

## Docker Nodes

Compose starts the Python nodes as separate services:

- `smart-watering` - callback node on host port `8080`.
- `worker` - queue worker.
- `snapshotter` - periodic status snapshot enqueue process.
- `public-api` - public API on host port `8081`.
- `watering-detector` - periodic watering detection process.
- `cli` - idle management container available through the Portainer console.

Setup:

```bash
cp docker/.env.example docker/.env
# edit SMART_WATERING_NODE_URL to the LAN-reachable Docker host URL
# edit SMART_WATERING_PUBLIC_API_JWT_SECRET
docker compose --env-file docker/.env -f docker/docker-compose.yml up --build -d
```

Run CLI commands through the management container:

```bash
make cli CLI_ARGS="devices list"
make cli CLI_ARGS="users add mobile-app"
make cli CLI_ARGS="users drop mobile-app"
make cli CLI_ARGS="fill main_tank 200"
make cli CLI_ARGS="operations"
```

In Portainer, open the `cli` container console with `/bin/sh`, then run the same
commands as `python -m smart_watering devices list`, `python -m smart_watering
operations`, and so on.

Inspect logs:

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml logs -f smart-watering
docker compose --env-file docker/.env -f docker/docker-compose.yml logs -f worker
docker compose --env-file docker/.env -f docker/docker-compose.yml logs -f snapshotter
docker compose --env-file docker/.env -f docker/docker-compose.yml logs -f public-api
```

Stop containers while keeping the database volume:

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml down
```

Remove containers and the SQLite volume:

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml down -v
```

## Tests

```bash
python -m compileall -q smart_watering
pytest -v smart_watering/tests
make ENV_FILE=docker/.env.example config
make ENV_FILE=docker/.env.example build
```
