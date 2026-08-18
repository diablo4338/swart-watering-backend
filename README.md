# Smart Watering

Smart Watering is a small watering-control stack for ESP32-C3 devices and a Python control plane.
The firmware exposes local HTTP endpoints on devices, while the Python services keep device registry state, queue writes, collect operation callbacks and expose a public authenticated API.

## Components

- `firmware/` - ESP-IDF firmware for ESP32-C3 controllers. It reads weight through HX711, stores runtime config in NVS, controls pump GPIO for tank devices and exposes the device HTTP API.
- `smart_watering/` - Python module with CLI, SQLite storage, Alembic migrations, queue worker, callback node, public API and tests.
- `MyApplication/` - Android client for the public `/api/v2` API.
- `docker/` - Dockerfiles and Compose stack for callback node, worker, public API and an always-running CLI helper container.
- `scripts/` - host helper wrappers for running CLI commands inside the Compose CLI container.

## Documentation

- Firmware details: [firmware/README.md](firmware/README.md)
- Python services and APIs: [smart_watering/README.md](smart_watering/README.md)
- Android app build/env setup: [MyApplication/README.md](MyApplication/README.md)

## Runtime Flow

1. Devices are registered in the Python SQLite database.
2. Config and watering writes are queued because ESP devices may be online only for a short wake window.
3. The worker drains queued writes to devices.
4. Devices call back to the callback node with operation lifecycle events.
5. The public API exposes device discovery, type metadata, status reads, queued device-control commands, watering, and operation tracking behind JWT-backed sessions.

## Environment Variables

Core Python services:

| Variable | Default | Used by | Description |
| --- | --- | --- | --- |
| `SMART_WATERING_DB_PATH` | `/tmp/smart_watering_cli/smart_watering.db` locally, `/data/smart_watering.db` in Docker | CLI, callback node, worker, snapshotter, watering detector, public API | SQLite database path. All Python processes must point at the same database. |
| `SMART_WATERING_NODE_URL` | auto-detected `http://<host-ip>:8080` | CLI, worker, callback URL builder | Public callback-node base URL sent to ESP devices as `callback_url`. In Docker/LAN setups this must be reachable from the devices, so do not use `localhost` unless the device runs on the same host. |
| `SMART_WATERING_WORKER_IDLE_INTERVAL_SEC` | `1` | worker | Seconds the worker sleeps after a queue scan when there is no ready work. Lower values react faster but poll SQLite more often. |
| `SMART_WATERING_WORKER_RETRY_INTERVAL_SEC` | `5` | worker | Seconds between retry attempts for retryable watering-start and sleep-disable commands while a device is asleep/offline. |
| `SMART_WATERING_WORKER_MAX_WAIT_SEC` | `900` | worker | Maximum seconds the worker waits for retryable command delivery and final controller callbacks before timing out. |
| `SMART_WATERING_SNAPSHOT_INTERVAL_SEC` | `300` | snapshotter | Seconds between periodic status snapshot enqueue passes. |
| `SMART_WATERING_DETECTOR_INTERVAL_SEC` | `3600` | watering detector | Seconds between Prometheus scans for plant watering events. |
| `SMART_WATERING_DETECTOR_LOOKBACK_HOURS` | `3` | watering detector | Overlapping history window scanned on every pass. |
| `SMART_WATERING_DETECTION_WINDOW_MIN` | `5` | watering detector, CLI | Minutes after the first weight increase in which the maximum watering weight is selected. |
| `SMART_WATERING_MAX_DETECTED_WATERING_G` | `1000` | watering detector, CLI | Maximum accepted detected watering increase. Larger jumps are retained as invalid anomalies and are not returned to the app. |
| `SMART_WATERING_CLI_OPERATION_WAIT_TIMEOUT_SEC` | `900` | CLI, helper scripts | Maximum seconds the CLI waits for a queued operation result before returning a timeout to the user. |

Public API and auth:

| Variable | Default | Used by | Description |
| --- | --- | --- | --- |
| `SMART_WATERING_PUBLIC_API_JWT_SECRET` | none, required for public API | public API | Secret used to sign HS256 session JWTs. Set a long random value in every non-test deployment. |
| `SMART_WATERING_PUBLIC_API_SESSION_TTL_SEC` | `3600` | public API | Session token lifetime in seconds. |
| `SMART_WATERING_GOOGLE_WEB_CLIENT_ID` | empty | public API, Android app | Google OAuth Web client ID. Required to enable Google Sign-In. |
| `SMART_WATERING_GOOGLE_ALLOWED_EMAILS` | empty | public API | Comma-separated allowlist of Google account emails accepted by `/api/v2/auth/google`. |
| `SMART_WATERING_GOOGLE_ALLOWED_DOMAINS` | empty | public API | Comma-separated allowlist of Google Workspace domains accepted by `/api/v2/auth/google`. Use this or `SMART_WATERING_GOOGLE_ALLOWED_EMAILS` when Google Sign-In is enabled. |
| `SMART_WATERING_PROMETHEUS_URL` | `http://127.0.0.1:9090` | public API, watering detector, CLI | Prometheus server used for plant statistics and detected watering history. |
| `SMART_WATERING_STATISTICS_TIMEZONE` | `Europe/Berlin` | public API | Calendar timezone used for day (08:00–20:00) and night (20:00–08:00) periods. |

Docker Compose host settings:

| Variable | Default | Used by | Description |
| --- | --- | --- | --- |
| `SMART_WATERING_PORT` | `8080` | Docker Compose | Host port mapped to the callback node container's port `8080`. |
| `SMART_WATERING_PUBLIC_API_PORT` | `8081` | Docker Compose | Host port mapped to the public API container's port `8081`. |
| `TMPDIR` | `/data` in Dockerfiles/Compose | Python runtime in containers | Temporary directory used by Python. Compose pins it to the persistent data volume. |

Detected plant watering history can be backfilled idempotently from Prometheus:

```bash
make cli CLI_ARGS="watering-history sync --days 30"
make cli CLI_ARGS="watering-history sync --days 30 --device plant_1"
```

Detection compares minimum and maximum weights in five-minute windows and merges
multi-sample increases into one event. Scans include the preceding 50 minutes so
that a watering at the start of a scan still has a baseline when a sleeping device
reports at its longest supported interval. Deleting a false event in the mobile client
marks it invalid instead of removing it, so later detector scans do not recreate it.

Android build configuration:

| Variable | Default | Used by | Description |
| --- | --- | --- | --- |
| `SMART_WATERING_PUBLIC_API_BASE_URL` | `https://api.example.com/` | Android Gradle build | Public API server root URL embedded into the app. Do not include `/api/v2`; app paths already include it. |
| `SMART_WATERING_PUBLIC_API_URL` | fallback alias for base URL | Android Gradle build, smoke test | Legacy/fallback public API root URL. The smoke test requires this exact key. |
| `SMART_WATERING_PUBLIC_API_BASE_URL_DEBUG` | none | Android Gradle build | Debug-build override for `SMART_WATERING_PUBLIC_API_BASE_URL`. |
| `SMART_WATERING_PUBLIC_API_BASE_URL_RELEASE` | none | Android Gradle build | Release-build override for `SMART_WATERING_PUBLIC_API_BASE_URL`. |
| `SMART_WATERING_GOOGLE_WEB_CLIENT_ID_DEBUG` | none | Android Gradle build | Debug-build override for `SMART_WATERING_GOOGLE_WEB_CLIENT_ID`. |
| `SMART_WATERING_GOOGLE_WEB_CLIENT_ID_RELEASE` | none | Android Gradle build | Release-build override for `SMART_WATERING_GOOGLE_WEB_CLIENT_ID`. |
| `SMART_WATERING_RELEASE_STORE_FILE` | none | Android Gradle `local.properties` | Release signing keystore path. This is read from `local.properties`, not from OS env. |
| `SMART_WATERING_RELEASE_STORE_PASSWORD` | none | Android Gradle `local.properties` | Release signing keystore password. |
| `SMART_WATERING_RELEASE_KEY_ALIAS` | none | Android Gradle `local.properties` | Release signing key alias. |
| `SMART_WATERING_RELEASE_KEY_PASSWORD` | none | Android Gradle `local.properties` | Release signing key password. |
| `SMART_WATERING_ANDROID_RELEASES_DIR` | `/srv/smart-watering/releases` in production example | self-hosted runner, public API Compose mount | Absolute host directory where signed Android releases and `latest.json` are published. Runner and backend must use the same path. |

Smoke tests and helper scripts:

| Variable | Default | Used by | Description |
| --- | --- | --- | --- |
| `SMART_WATERING_PUBLIC_API_URL` | `examples/.env.smoke` | `examples/public-api-watering-smoke-test.py` | Public API root URL for the watering smoke test. |
| `SMART_WATERING_USERNAME` | fallback to `USER_USERNAME` | smoke test | Username for password login during the smoke test. |
| `SMART_WATERING_PASSWORD` | fallback to `USER_PASSWORD` | smoke test | Password for password login during the smoke test. |
| `USER_USERNAME` | none | smoke test | Legacy fallback username variable. |
| `USER_PASSWORD` | none | smoke test | Legacy fallback password variable. |

## Quick Start

Install Python dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r smart_watering/requirements-dev.txt
```

Run the interactive CLI:

```bash
python -m smart_watering
```

## Portainer Git Stack

Create a Git-backed Stack in Portainer with these settings:

- repository: this repository;
- Compose path: `docker/docker-compose.yml`;
- environment variables: copy the values from `docker/.env.example` and replace
  at least `SMART_WATERING_NODE_URL` and `SMART_WATERING_PUBLIC_API_JWT_SECRET`.

Portainer builds every service from the checked-out commit using the shared
Dockerfile and build cache. Enable Git auto-update/webhooks in Portainer if
deployments should follow repository updates automatically.

The same stack can be built and started directly with Docker Compose:

```bash
cp docker/.env.example docker/.env
# edit SMART_WATERING_NODE_URL and SMART_WATERING_PUBLIC_API_JWT_SECRET
docker compose --env-file docker/.env -f docker/docker-compose.yml up --build -d
```

Or use the Makefile shortcuts:

```bash
make restart
```

`make build`, `make up`, and `make restart` all use the same Compose file as
Portainer. The `cli` service stays running so commands can be executed from the
Portainer container console without SSH. Locally, use
`make cli CLI_ARGS="devices list"`.

Run the public API watering smoke test:

```bash
cp examples/.env.smoke.example examples/.env.smoke
# edit examples/.env.smoke
python examples/public-api-watering-smoke-test.py --target-g 1
```

## Android Release Publishing

The Android repository in `swart-watering-android/` contains a self-hosted GitHub
Actions workflow. A successful run builds and signs an APK in Docker, publishes a
versioned directory and atomically replaces `latest.json` in
`SMART_WATERING_ANDROID_RELEASES_DIR`. The public API mounts that host directory
read-only and exposes:

- `GET /api/v2/app/latest` — current version metadata and download URL;
- `GET /api/v2/app/releases/{version_code}/download` — the signed APK.

Because the runner, backend and Prometheus are on one machine, configure the same
absolute directory (for example `/srv/smart-watering/releases`) as both the Android
workflow's `RELEASES_DIR` repository variable and backend
`SMART_WATERING_ANDROID_RELEASES_DIR`. No file transfer or extra release server is
needed. Prometheus remains reachable from the containers through
`http://host.docker.internal:9090`.

The first release requires an Android tag such as `app-v1.0.0`. See
`swart-watering-android/README.md` for runner variables, signing files and the exact
release procedure.

Client build and test-publication commands are documented in
`swart-watering-android/README.md` and live entirely in the Android repository.

## Quick Check

```bash
python -m compileall -q smart_watering
pytest -v smart_watering/tests
docker compose --env-file docker/.env.example -f docker/docker-compose.yml config --quiet
docker compose --env-file docker/.env.example -f docker/docker-compose.yml build
```
