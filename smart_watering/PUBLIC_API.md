# Smart Watering Public API

The Android application uses the server-driven v3 device-card contract. The
canonical architecture is documented in
[`../docs/DEVICE_CARD_ARCHITECTURE.md`](../docs/DEVICE_CARD_ARCHITECTURE.md), and
the machine-readable contract is
[`public_api.openapi.yaml`](public_api.openapi.yaml).

The checked-in schema is generated from the registered FastAPI application and is
verified against `/openapi.json` by the backend test suite. It must be regenerated
whenever a route signature changes.

## Public routes

- `GET /healthz`
- `GET /api/v3/app/latest`
- `GET /api/v3/app/releases/{version_code}/download`

The equivalent `/api/v2/app/...` release routes are deprecated compatibility aliases.

The two v2 application-release routes intentionally remain compatible so installed
legacy Android clients can discover and download a current APK. No other v2 route
is registered.

## Authentication

- `POST /api/v3/auth/login`
- `POST /api/v3/auth/google`
- `POST /api/v3/auth/logout`

Protected endpoints require `Authorization: Bearer <jwt>`. Tokens are HS256 JWTs
backed by active SQLite sessions.

## Device cards

- `GET /api/v3/devices`
- `GET /api/v3/devices/{device_id}/card`
- `GET /api/v3/devices/{device_id}/card/blocks/{block_id}`
- `POST /api/v3/devices/{device_id}/actions/{action}`

`device_id` is the immutable UUID from `devices.id`. Mutable backend names and MCU
identifiers are presentation/configuration data and are never used as resource IDs.

The backend owns device workflows, operation queues, connectivity state, snapshot
fallback, control schemas, and action URLs. The Android client renders the returned
blocks and never calls operation-oriented endpoints directly.
