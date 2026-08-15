# USD-native Python API

This guide covers the high-level APIs for applications that already own a
`pxr.Usd.Stage`. OpenUSDConnect performs network I/O in background threads, but
the application remains responsible for calling `update()` on the thread that
owns the stage.

## Choose an API

| API | Direction | Layer model | Typical use |
| --- | --- | --- | --- |
| `ManagedClient` | Bidirectional | Server-owned collaboration layers plus one client-owned authoring layer | USD editor or DCC integration |
| `UsdReceiver` | Receive | Server-owned collaboration layers | Viewer or read-only service |
| `UsdPublisher` | Send | Current edit-target layer | Producer with a separate receive stage, or send-only tool |
| `SharedStageClient` | Bidirectional | Existing portable root and recursive sublayers | Exact production-layer editing |

Start with `ManagedClient` unless the application needs only one direction,
intentionally authors multiple custom layers, or must synchronize the original
authored-layer graph.

## Lifecycle and status

The high-level clients share the same stage-thread and status conventions, with
role-specific networking:

1. Construction validates the stage and initializes the role-specific stage
   state.
2. `start()` returns immediately. It starts the background receiver for
   `UsdReceiver`, `ManagedClient`, and `SharedStageClient`; `UsdPublisher`
   merely enters its nonblocking lifecycle. Entering a context manager calls
   `start()`.
3. `connect(timeout)` waits for the applicable handshakes. For receiving
   clients, it does not apply queued replay.
4. `update()` applies incoming work and, for bidirectional clients, submits
   local work without waiting for a durable acknowledgement.
5. `flush(timeout)` waits for already submitted work. Call `update()` first if
   the stage may still contain unsent edits.
6. `close()` stops networking. It does not implicitly turn every pending edit
   into a blocking flush.

`client.status` is an immutable `ClientStatus`. Its `phase` is one of
`OFFLINE`, `CONNECTING`, `REPLAYING`, `READY`, `RECOVERY_REQUIRED`, `REJECTED`,
or `CLOSED`. Bidirectional applications should enable editing only in `READY`.
The directional connection fields distinguish partial connectivity from a
role that is not present.

`ManagedClient.update()` and `SharedStageClient.update()` return `SyncUpdate`:

- `applied_events`: authoritative events applied during this call
- `submitted_events`: local events accepted by the sender outbox
- `acknowledged_events_delta`: newly consumed durable acknowledgements
- `pending_events`: currently submitted but unacknowledged events
- `recovery`: a deterministic rejection that requires application action

## Receive into a stage

```python
from pxr import Usd

from openusdconnect import ClientPhase, UsdReceiver

stage = Usd.Stage.Open("shot.usda")

with UsdReceiver(stage, app_name="my-viewer") as receiver:
    if not receiver.connect(timeout=5):
        raise ConnectionError("OpenUSDConnect server is unavailable")

    while application_is_running():
        receiver.update()
        show_loading(receiver.status.phase is not ClientPhase.READY)
```

`UsdReceiver` always requests managed layered replay from sequence 1. It owns
anonymous collaboration layers at the strong end of the stage's session-layer
stack. A server that cannot provide layered replay rejects the connection
instead of silently degrading to flat replay.

Open the original base scene. A generated live-open snapshot already contains
composed server state and is rejected because replaying the complete managed
history over it would duplicate opinions. Snapshot continuation is a separate
flat integration path used by the live-open host plugins.

Use `rebind_stage(new_stage)` when a host replaces its stage. Passing `None`
parks application while the network queue continues to receive data.

Application callbacks such as `on_applied` and `on_applied_events` run inside
`update()` on the calling thread. Transport callbacks, including token,
metadata, and playback notifications, may run on a background connection
thread and must be marshalled before touching a UI.

## Publish USD edits

Construct `UsdPublisher` before making edits so its `Usd.Notice` listener sees
them:

```python
from pxr import Gf, Usd, UsdGeom

from openusdconnect import UsdPublisher

stage = Usd.Stage.Open("shot.usda")
stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))

with UsdPublisher(stage, app_name="layout") as publisher:
    if not publisher.connect(timeout=5):
        raise ConnectionError("OpenUSDConnect server is unavailable")

    sphere = UsdGeom.Sphere.Define(stage, "/World/Sphere")
    UsdGeom.Xformable(sphere).AddTranslateOp().Set(Gf.Vec3d(1, 2, 3))
    publisher.update()

    if not publisher.flush(timeout=5):
        raise TimeoutError("changes were not durably acknowledged")
```

The publisher sends opinions authored in the current edit-target layer. It
does not copy composed values supplied only by references, payloads, or weaker
layers. Call `update()` before switching edit targets because one emitted
transaction cannot own opinions from multiple layers.

`update()` transfers a prepared batch to a bounded sender outbox. A socket
failure after an ambiguous write retains the exact transaction and resends it
with the same producer session and transaction ID after reconnection. The
server either commits it once or reports the existing durable high-water mark.

`UsdPublisher.update()` does not initiate a reconnect. While disconnected it
returns zero and leaves noticed edits dirty; call `connect()` and then
`update()` to submit them.

Use `publish_current_edit_target()` when attaching to a layer that was already
authored before the publisher existed. It publishes authored opinions, not a
flattened composed stage. Retry any retained batch with `update()` first.

For high-frequency default-time transforms, set
`transform_coalesce_seconds` to a small host-appropriate window. Only repeated
TRS updates for the same prim and time code are merged. Structural events,
other event kinds, and distinct animation samples remain ordering barriers.

## Bidirectional managed client

`ManagedClient` combines the publisher and receiver on one application stage:

```python
from pxr import Gf, Usd, UsdGeom

from openusdconnect import ClientPhase, ManagedClient

stage = Usd.Stage.Open("shot.usda")

with ManagedClient(
    stage,
    app_name="shot-editor",
    department="layout",
    transform_coalesce_seconds=0.02,
) as client:
    if not client.connect(timeout=5):
        raise ConnectionError("OpenUSDConnect server is unavailable")

    translate = None

    while application_is_running():
        client.update()
        if client.status.phase is ClientPhase.READY:
            if translate is None:
                sphere = UsdGeom.Sphere.Define(stage, "/World/Sphere")
                translate = UsdGeom.Xformable(sphere).AddTranslateOp()
            translate.Set(Gf.Vec3d(1, 2, 3))
```

Construction creates `client.authoring_layer`, inserts it below the
authoritative managed block, and makes it the edit target. Keep that target
while the client is active. `update()` freezes local edits, applies the queued
authoritative prefix, then submits the frozen local batch. The dispatcher
suppresses and invalidates the emitter while applying server records, so
authoritative echoes do not become new local submissions.

Use separate `UsdPublisher` and `UsdReceiver` stages when the host intentionally
authors persistent layers or changes edit targets. Attaching those two
low-level roles directly to the same stage duplicates opinion ownership;
`ManagedClient` is the supported single-stage form.

A deterministic server rejection moves the client to `RECOVERY_REQUIRED`
without raising from an ordinary interactive `update()`. See
[Client recovery](client-recovery.md) before designing the host UI.

## Adapter destination contract

Layered receivers always reconstruct authoritative state in a USD mirror. What
happens next depends on `DCCAdapter.targets_stage()`:

- `UsdStageAdapter` returns the exact stage it mutates. When that object is the
  mirror, OpenUSD composition already produced the destination state and no
  composed projection is needed.
- An adapter for an external scene, such as Blender objects, returns `None`.
  The dispatcher projects changes from the composed mirror into adapter events.
- Returning a different `Usd.Stage` also selects projection. The comparison is
  object identity, not matching layer identifiers.

Custom stage-backed adapters must override `targets_stage()` explicitly.

Native projection can express only the adapter event vocabulary. Generic Sdf
opinions remain correct in the mirror even when the native scene has no
equivalent operation.

## Shared authored-layer editing

`SharedStageClient` synchronizes authored state in an existing portable root
and recursive sublayer graph. Filesystem paths and custom resolver identifiers
are valid when each participant can resolve an equivalent, editable graph:

```bash
uv run openusdconnect-server --base shot.usda --layer-mode shared_stage
```

```python
from pxr import Usd

from openusdconnect import ClientPhase, SharedStageClient

stage = Usd.Stage.Open("shot.usda")

with SharedStageClient(stage, app_name="layer-editor") as client:
    if not client.connect(timeout=5):
        raise ConnectionError("OpenUSDConnect server is unavailable")

    while application_is_running():
        result = client.update()
        set_editing_enabled(client.status.phase is ClientPhase.READY)
```

Every process opens its own equivalent root document under its normal
`ArResolver` context. Opaque layer keys route exact Sdf field and sublayer
topology changes; local identifiers and resolved paths never cross the wire.
The session layer and layers introduced only by references or payloads are not
part of the synchronized graph.

The contract assumes equivalent initial authored contents. OpenUSDConnect does
not currently compare or publish a complete baseline, so unchanged fields can
differ silently if resolver contexts or deployed asset versions disagree.
Production integrations should use immutable/versioned assets or an explicit
baseline identity policy before enabling edits.

Construction rejects an anonymous root, an edit target outside the synchronized
graph, and non-portable sublayer paths in the resolvable graph. Missing
sublayers may resolve later; call `refresh_layer_graph()` after resolver or
asset availability changes. Use `is_layer_reachable(layer)` before authoring
into a newly attached layer.

The portable Python tracker keeps full in-memory layer snapshots. Native hosts
can build an optional bridge against the exact OpenUSD installation they load:

```bash
uv run openusdconnect-build-sdf-notice-bridge
```

The bridge consumes `SdfLayerStateDelegate` changes and avoids full baseline
snapshots during ordinary tracking. Its manifest must match the host's OpenUSD
version, platform, and architecture; incompatible builds are rejected before
loading. Pass `delegate_bridge_path=` to select a specific build.

`SharedStageClient` never calls `Sdf.Layer.Save()`. The server log preserves
unsaved collaboration history, while saving file or resolver-backed layers
remains application policy. Shared-stage rejection recovery has additional
clean-stage requirements described in [Client recovery](client-recovery.md).

The implementation details and protocol event shapes are documented in
[Shared-stage architecture](shared-stage-architecture.md).

## Assets and resolver contexts

Authored asset identifiers remain USD identifiers on the wire:

- `./` and `../` paths are anchored through their owning layer identifier.
- Bare search paths and custom URIs resolve under each process's resolver and
  stage context.
- Anonymous layers provide no document anchor for relative asset paths.

All endpoints must load compatible resolver plugins and configuration. For a
managed receiver, call `refresh_asset_dependency(path)` after an asset becomes
available or its resolver mapping changes; omit the path to retry all pending
dependencies.

A context-only resolver remap is a special case for adapters targeting a
non-USD native scene. It can recompose both the live and previous-state stages
before projection observes the old topology. The dispatcher then sets
`native_scene_rebuild_required` and stops incremental delivery. Recreate or
rebind the receiver/dispatcher, or rebuild the native destination and call
`acknowledge_native_scene_rebuilt()` before resuming. An ordinary reconnect
does not clear this guard.

## Identity and authentication

`app_name` creates a stable client identity. Publisher and receiver roles that
belong to one integration should use the same `app_name` or explicit
`client_id`.

TOFU tokens are loaded and saved by default. Set `persist_token=False` for
ephemeral tools or tests, pass `token=` when the host owns credential storage,
and use `on_token_issued` to integrate with a host-specific store.

## Low-level APIs

`NoticeEmitter`, `EventSender`, `ReceiverThread`, and `EventDispatcher` remain
public for custom scheduling, DCC adapters, and constrained flat snapshot
continuation. `ReceiverThread` requests layered replay by default; passing
`layered_replay=False` selects the single-layer flat contract.

Prefer constructing low-level objects directly over mutating the components
inside a high-level client. The high-level wrappers expose their components for
diagnostics, but their lifecycle invariants remain owned by the wrapper.
