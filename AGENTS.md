# Smart Watering project instructions

## Target architecture

- Backward compatibility is not required while implementing the new architecture. Prefer a clean replacement over compatibility adapters unless the user explicitly asks for a staged migration.
- The backend owns device workflows, operation state machines, queues, recovery, timeouts, retries, and the decision about which actions are currently available.
- The Android client must not interpret backend operation types or operation lifecycle statuses and must not restore or track operations itself.
- The Android client renders server-provided device-card blocks. It owns visual composition for known card profiles and supported component types, but business state and available actions come from the backend.
- Dynamic controls use stable, versioned `control_type` and optional `preset` identifiers resolved by a native Android registry. Each action or field commit submits only its explicitly declared value binding; never implicitly submit an entire block.
- `devices.id` is the only device identity at every layer: HTTP resources, application-service arguments, operation and queue ownership, worker partitioning, runtime state, presence, caches, callbacks, and client state. Never key, join, route, partition, correlate, or recover a registered device by backend name, MCU name, IP, or URL.
- Device names are mutable display data only. A delivery adapter may show them, but must pass `device_id` after selection. Temporary discovery targets that do not have a database ID yet must use an explicitly named discovery key and must never be treated as registered-device identity.
- New device-related records and DTOs must carry `device_id`; `device_name` may be included only as a display snapshot/label alongside it. Repository methods for registered devices must accept IDs or `Device` objects, never names.
- Use `docs/DEVICE_CARD_ARCHITECTURE.md` as the canonical design for device-card APIs and client/backend responsibilities. Update that document whenever the design changes.
