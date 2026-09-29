# Changelog

Notable changes to OpenUSDConnect are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor
release may contain breaking changes; they are listed under **Changed** with
migration notes.

The release version covers the Python package and the Blender add-on
(`openusdconnect/_version.py`). The Unreal plugin carries its own version in
`OpenUSDConnect.uplugin`. Each entry states wire compatibility separately: the
protocol version (`PROTOCOL_VERSION`) and wire schema version
(`SCHEMA_VERSION`) decide whether clients and servers from different releases
can connect.

## [0.5.0] - Unreleased

Wire compatibility: protocol 13 and wire schema 10 are unchanged. The
handshake and transaction results gained optional fields (receiver replay
identity, server instance, post-commit checkpoints); 0.4.0 peers still connect
but do not receive replay-identity validation or checkpoints.

### Changed

- **Breaking:** `UsdReceiver.update()` and `UsdPublisher.update()` return a
  `SyncUpdate` instead of an `int`, like the bidirectional clients. Read
  `applied_events` or `submitted_events`.
- **Breaking:** `UsdPublisher.update()` raises before `start()`. While
  disconnected it schedules a background handshake instead of doing nothing;
  `disconnect()` pauses those attempts until `connect()`.
- **Breaking:** `publish_current_edit_target()` on `UsdPublisher` and
  `ManagedClient` keeps the snapshot prepared while disconnected or replaying
  and submits it on a later `update()`, instead of returning 0 without
  preparing it.
- **Breaking:** `ManagedClient.rebind_stage()` refuses unsent or
  unacknowledged work. Finish with `submit_and_wait()`, or pass
  `discard_unsent=True` to drop unsent edits.
- **Breaking:** `connect()` and `flush()` on the high-level clients default to
  a 10 second timeout instead of waiting indefinitely. Pass `timeout=None` to
  wait without a limit.
- **Breaking:** the high-level clients take one `observer=ClientObserver`
  instead of nine `on_*` callback arguments; every observer method runs during
  `update()`. Migrate `on_applied`, `on_applied_events`, and `on_imported` to
  `on_applied(batch)` (`batch.prim_paths`, `batch.events`,
  `batch.imported_paths`, `batch.seq`), and `on_playback_claimed` /
  `on_playback_rejected` to `on_playback_claim(result)`. Metadata and playback
  payloads are typed (`StageMetadata`, `PlaybackState`, `PlaybackClaim`), and
  `client.stage_metadata` returns `StageMetadata`. The low-level
  `EventDispatcher`, `ReceiverThread`, and `EventSender` keep plain callables.
- `EventDispatcher` starts its cursor at `receiver.sync_from - 1` instead of
  0, so integrations no longer need to seed `last_seq` for continuation.
- `ManagedClient` and `UsdReceiver` report `ClientPhase.PARKED` while no stage
  is bound.
- Server state is split into scene, transaction, journal, collaboration, and
  maintenance owners; emitter, codec, and composed projection internals are
  simplified. No public behavior change.

### Removed

- `UsdReceiver.applying_seq`; use `AppliedBatch.seq` inside `on_applied`.

### Added

- `ClientObserver`, `AppliedBatch`, `StageMetadata`, `PlaybackState`, and
  `PlaybackClaim` at the package root.
- `wait_until_ready(timeout)` on every high-level client and
  `submit_and_wait(timeout)` on every publishing client. Both default to a
  10 second timeout, return `False` only when it expires, and raise
  `PermissionError`, `ConnectionError`, `TransactionRejectedError`, or
  `RuntimeError` for states further updates cannot resolve.
- `ClientStatus.has_unsent_changes`, `deferred_events`, `deferred_layer_keys`,
  `edit_target_is_shared`, and `recovery_stage_pending`, with matching client
  properties.
- `SharedStageClient.resume_recovery()` continues a Use Server recovery whose
  replacement replay did not finish, and the `no_pending_recovery_stage`
  recovery error code.
- `SharedStageClient.auth_rejected`, `connection_rejected`,
  `claim_playback()`, and `send_playback_control()`;
  `UsdPublisher.connection_rejected`.
- `update(max_messages=)` on `ManagedClient` and `SharedStageClient` bounds
  one call's receive work so a reconnect backlog spreads over frames;
  `EventDispatcher.backlog_pending` reports a truncated drain.
- `ClientStatus.can_author` combines readiness, role, and edit-target scope.
- `background_send=True` on `EventSender` and the publishing clients moves
  transaction writes and reconnect replay to a worker thread while preserving
  transaction order. It stays opt-in: the worker needs the GIL, which adds
  about 5 ms per write while the host's main thread runs Python.
- `EventSender(token_provider=)` resolves missing credentials once per
  connection attempt, on the connecting thread.
- Receiver replay identity (`server_instance`, replay epoch) and optional
  post-commit transaction checkpoints.

### Fixed

- Receivers continuing from a live-open snapshot without seeding the
  dispatcher cursor replayed the full history over the snapshot.
- MCP writes were confirmed before the mirror applied them; confirmation now
  waits for a durable replay checkpoint.
- Replay completion markers were lost when applied progress reset during a
  resync.
- Property edits absorbed by a prim resync in the same change block were
  dropped by the emitter.
- Bidirectional clients read the token file on every `update()` while their
  sender was reconnecting.
- Connection attempts to an unreachable server logged a traceback per retry;
  they log one line.
- `NoticeEmitter.rebind_stage()` compared stage metadata against the previous
  stage, and `cleanup()` kept pending stage-metadata changes.
- A `ManagedClient` constructed with invalid options left its authoring
  sublayer in the session layer and the edit target switched.

## [0.4.0] - 2026-09-18

Wire compatibility: protocol 13 (from 12), wire schema 10. Clients and
servers must both be 0.4.0 or later.

### Added

- `ServerRuntime`, `ServerConfig`, and `start_server()` embed the server in an
  application without signal handlers; `run_server()` remains the blocking
  command-line runner.
- `ClientPhase`, `ClientStatus`, and `SyncUpdate` in
  `openusdconnect.client_types`, `client.status`, and `client.client_id`.
- `EventSender.request_connect()` and `cancel_connect()` for nonblocking
  handshakes; bidirectional clients reconnect their sender from `update()`.
- `openusdconnect.shader_mapping` for shader mapper interfaces and
  `openusdconnect.usd_authoring` helpers (`set_connectable_input_value`,
  `resolve_shader_port_type`).
- Distributable server packages and configurable OpenUSD runtimes.
- Targeted time-sample erasure with native Sdf tracking.

### Changed

- The native Sdf notice bridge ABI is version 2; rebuild it with
  `openusdconnect-build-sdf-notice-bridge`.

### Fixed

- Time-sample deletions keep their order through application and log
  compaction.

## [0.3.0] - 2026-08-30

Wire compatibility: protocol 12, wire schema 10.

### Added

- Shared native C++ client core (framing, producer outbox, receiver inbox)
  used by the Python client through nanobind and by the Unreal plugin.
- `UsdReceiver(adapter=...)` projects composed changes into non-USD
  native scenes.
- Cross-platform OpenUSD runtime configuration and managed OpenUSD source
  builds.

### Changed

- `uv sync` builds the native client extension, which requires CMake and a
  64-bit C++17 toolchain. The Blender add-on vendors a build for Blender's
  Python.

## [0.2.0] - 2026-08-18

First versioned release. Protocol 12, wire schema 10. The release version is
defined once in `openusdconnect/_version.py` and mirrored by the Blender
add-on.
