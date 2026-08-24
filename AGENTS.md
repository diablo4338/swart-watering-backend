# Smart Watering project instructions

## Target architecture

- Backward compatibility is not required while implementing the new architecture. Prefer a clean replacement over compatibility adapters unless the user explicitly asks for a staged migration.
- The backend owns device workflows, operation state machines, queues, recovery, timeouts, retries, and the decision about which actions are currently available.
- The Android client must not interpret backend operation types or operation lifecycle statuses and must not restore or track operations itself.
- The Android client renders server-provided device-card blocks. It owns visual composition for known card profiles and supported component types, but business state and available actions come from the backend.
- Dynamic controls use stable, versioned `control_type` and optional `preset` identifiers resolved by a native Android registry. Each action or field commit submits only its explicitly declared value binding; never implicitly submit an entire block.
- Use `docs/DEVICE_CARD_ARCHITECTURE.md` as the canonical design for device-card APIs and client/backend responsibilities. Update that document whenever the design changes.
