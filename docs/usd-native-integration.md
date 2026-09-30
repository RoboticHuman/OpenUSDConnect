# Python client and host-integration API

These APIs attach OpenUSDConnect to an application-owned `pxr.Usd.Stage`.
Call `update()` from the stage-owning thread. Socket reads and reconnects run
on background threads; encoding, USD work, and (by default) transaction writes
run on the calling thread.

## Choose an API

| API | Direction | Layer model | Typical use |
| --- | --- | --- | --- |
| `ManagedClient` | Bidirectional | Server-owned collaboration layers plus one client-owned authoring layer | USD editor or DCC integration |
| `UsdReceiver` | Receive | Server-owned collaboration layers | USD viewer or `DCCAdapter`-backed native scene |
| `UsdPublisher` | Send | Current edit-target layer | Producer with a separate receive stage, or send-only tool |
| `SharedStageClient` | Bidirectional | Existing portable root and recursive sublayers | Exact production-layer editing |

Use `ManagedClient` by default. Choose a directional client for send-only or
receive-only work, or `SharedStageClient` when the application must edit its
existing authored-layer graph. These bidirectional clients observe a USD stage;
they do not capture edits to a host's native objects. Native-scene integrations
use an adapter-backed receiver and their own outbound capture bridge, as
described in [Adapter destination contract](#adapter-destination-contract).

Managed clients must open equivalent base content and resolve referenced assets
compatibly. The server synchronizes collaboration opinions and their ordered
history; it does not distribute the base asset itself. The server-provided
snapshot workflow is a separate flat continuation path used by the Blender and
Unreal host integrations.

## Lifecycle and status

All high-level clients share one lifecycle:

1. `start()` returns immediately; entering a context manager calls it.
   `UsdPublisher` opens its socket on the first `update()`.
2. `wait_until_ready(timeout)` pumps `update()` until the client is `READY`.
   `connect(timeout)` only completes the handshakes.
3. `update(max_messages=None)` applies incoming work and submits local work.
   While a sender is disconnected it schedules a background handshake with
   backoff; rejections stop the retries.
4. `submit_and_wait(timeout)` publishes pending edits and waits until they are
   durable. `flush(timeout)` only waits for work already submitted.
5. `close()` stops networking without flushing.

Blocking calls (`connect`, `flush`, `wait_until_ready`, `submit_and_wait`)
default to a 10 second timeout and return `False` only when it expires; the
work stays queued. States that more updates cannot fix raise:

| State | Exception |
| --- | --- |
| Authentication rejected | `PermissionError` |
| Handshake rejected | `ConnectionError` |
| Transaction rejected | `TransactionRejectedError` |
| Closed, parked, or native-scene rebuild required | `RuntimeError` |

`client.status` is an immutable `ClientStatus` and the one place to read client
state; the clients themselves expose data and operations. Its `phase` is `OFFLINE`,
`CONNECTING`, `REPLAYING`, `READY`, `RECOVERY_REQUIRED`, `REJECTED`, `PARKED`
(no bound stage), or `CLOSED`. `status.can_author` tells a UI whether edits to
the current edit target will be published now. The status also reports unsent
(`has_unsent_changes`) and unacknowledged (`pending_events`) work, and
`auth_rejected` separates an authentication rejection from a protocol one. Per-role
connection fields are `None` for a role the client lacks. `ClientPhase`,
`ClientStatus`, and `SyncUpdate` are importable from the package root.

`update()` returns a `SyncUpdate` for the work done by that call:

- `applied_events`: authoritative events applied during this call
- `submitted_events`: local events accepted by the sender outbox
- `acknowledged_events_delta`: newly consumed durable acknowledgements
- `pending_events`: currently submitted but unacknowledged events
- `recovery`: a deterministic rejection that requires application action

### Observing the client

Pass one `ClientObserver` subclass as `observer=` and override only what the
host needs. Every method runs inside `update()` on the calling thread, and the
client wires only overridden methods, so unused notifications cost nothing:

```python
class HostObserver(ClientObserver):
    def on_applied(self, batch):             # AppliedBatch: seq, events, prim_paths
        refresh_host_ui(batch.prim_paths)

    def on_playback_state(self, state):      # PlaybackState
        set_host_time(state.time)

client = ManagedClient(stage, app_name="my-editor", observer=HostObserver())
```

`on_applied` and `on_resync` are part of delivery: raising rolls the batch back
and replays it, so they must be safe to retry. `on_stage_metadata`,
`on_playback_state`, `on_playback_claim`, and `on_token_issued` only observe:
raising propagates out of `update()` and later notifications wait for the next
call. Methods that do not apply to a client never fire; `UsdPublisher` reports
only tokens and stage metadata, and `SharedStageClient` has no delivery
methods.

An adapter-backed `UsdReceiver` enters `RECOVERY_REQUIRED` when resolver
recomposition makes incremental projection unsafe. Rebuild the native scene,
then call `acknowledge_native_scene_rebuilt()`.

### Host loop

GUI hosts drive the client from a timer instead of waiting:

```python
client = ManagedClient(stage, app_name="my-editor").start()

def on_timer():
    if client.status.edit_target_is_shared:  # ManagedClient publishes only its authoring layer
        client.update(max_messages=256)
    set_editing_enabled(client.status.can_author)
```

Pass `max_messages` in interactive hosts. Without it, `update()` applies the
whole queued backlog in one call: about 20 µs per transform event, so a
reconnect with 10,000 queued events stalls one frame for about 200 ms. With a
budget the backlog spreads over frames at the same total cost, and local edits
are held until the backlog queued before them has been applied.
`SharedStageClient` accepts any edit target (session-layer edits stay local),
so its loop calls `update()` unconditionally.

`background_send=True` moves transaction writes to a worker so a full socket
buffer cannot block the UI thread. The worker needs the GIL: while the host's
main thread runs Python, each write waits for Python's thread switch interval
(about 5 ms), so keep the default for latency-sensitive editing on fast links.

Before closing, stop authoring and call `submit_and_wait()`. Success means the
edits are durable, not that their echo has been applied locally.

## Receive into a stage

```python
from pxr import Usd

from openusdconnect import ClientPhase, UsdReceiver

stage = Usd.Stage.Open("shot.usda")

with UsdReceiver(stage, app_name="my-viewer") as receiver:
    if not receiver.wait_until_ready(timeout=5):
        raise TimeoutError("OpenUSDConnect replay did not finish in time")

    while application_is_running():
        receiver.update()
        show_loading(receiver.status.phase is not ClientPhase.READY)
```

Ordered replay remains pending until all messages preceding the server's
synchronization watermark have been applied, however `max_messages` splits it.

`UsdReceiver` always requests managed layered replay from sequence 1. It owns
anonymous collaboration layers at the strong end of the stage's session-layer
stack. A server that cannot provide layered replay rejects the connection
instead of silently degrading to flat replay.

Open the original base scene. A generated live-open snapshot already contains
composed server state and is rejected because replaying the complete managed
history over it would duplicate opinions. Snapshot continuation is a separate
flat integration path used by the live-open host plugins.

Use `rebind_stage(new_stage)` when a host replaces its stage. Passing `None`
parks stage application (phase `PARKED`) while the network queue continues to
receive data.

### Receive into an application-owned scene

For a host that does not use a `Usd.Stage` as its scene, pass a `DCCAdapter` to
the same high-level receiver. The stage becomes a composition mirror; only the
adapter mutates the host scene:

```python
from pxr import Usd

from openusdconnect import UsdReceiver

mirror_stage = Usd.Stage.Open("shot.usda")
adapter = MyHostAdapter(document)

with UsdReceiver(
    mirror_stage,
    app_name="my-host",
    adapter=adapter,
    observer=MyHostObserver(adapter),  # on_resync resets the adapter
) as client:
    while application_is_running():
        client.update()  # call from the host's scene/UI thread
```

The client owns transport, replay, logical collaboration layers, and composed
projection. The integration owns the adapter's mapping to native objects,
stable object identity, units and axes, undo policy, and UI-thread scheduling.
It must open equivalent base content in the mirror so OpenUSD composition sees
the same references and weaker opinions as other participants.

## Publish USD edits

Construct `UsdPublisher` before making edits so its `Usd.Notice` listener sees
them:

```python
from pxr import Gf, Usd, UsdGeom

from openusdconnect import UsdPublisher

stage = Usd.Stage.Open("shot.usda")
stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))

with UsdPublisher(stage, app_name="layout") as publisher:
    if not publisher.wait_until_ready(timeout=5):
        raise TimeoutError("OpenUSDConnect server is unavailable")

    sphere = UsdGeom.Sphere.Define(stage, "/World/Sphere")
    UsdGeom.Xformable(sphere).AddTranslateOp().Set(Gf.Vec3d(1, 2, 3))

    if not publisher.submit_and_wait(timeout=5):
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

`disconnect()` pauses automatic reconnection until the next `connect()`.

Use `publish_current_edit_target()` when attaching to a layer that was already
authored before the publisher existed. It publishes authored opinions, not a
flattened composed stage, and waits for a connection if needed. Retry any
retained batch with `update()` first.

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
    if not client.wait_until_ready(timeout=5):
        raise TimeoutError("OpenUSDConnect replay did not finish in time")

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

`publish_current_edit_target()` queues a snapshot of the authoring layer for
the next `update()` that can publish; a zero return can mean it is still queued.
`rebind_stage()` refuses unsent or unacknowledged work: call
`submit_and_wait()` first, or pass `discard_unsent=True` to drop unsent edits.
`rebind_stage(None)` parks the client while networking stays active.

`close()` detaches the collaboration layers but leaves the authoring layer on
the stage, so the composed scene can change. To keep what the user sees,
flatten first: `Usd.Stage.Open(client.stage.Flatten())`.

Use separate `UsdPublisher` and `UsdReceiver` stages when the host intentionally
authors persistent layers or changes edit targets. Attaching those two
low-level roles directly to the same stage duplicates opinion ownership;
`ManagedClient` is the supported single-stage form.

A deterministic server rejection moves the client to `RECOVERY_REQUIRED`
without raising from an ordinary interactive `update()`. See
[Client recovery](client-recovery.md) before designing the host UI.

## Adapter destination contract

`DCCAdapter` is the receiving boundary for an application-owned scene. It is
not the outbound document-change observer. A bidirectional host separately
captures its native edits and publishes them, commonly through an authoring
stage and `UsdPublisher`.

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

Shader mapping interfaces live in `openusdconnect.shader_mapping`; existing
imports from `openusdconnect.adapters` remain supported. Integrations that
author shader inputs directly can use `set_connectable_input_value` and
`resolve_shader_port_type` from `openusdconnect.usd_authoring`. They operate
under the stage's current edit target and do not send network events.

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
    if not client.wait_until_ready(timeout=5):
        raise TimeoutError("OpenUSDConnect replay did not finish in time")

    while application_is_running():
        client.update()
        set_editing_enabled(client.status.phase is ClientPhase.READY)
```

Every process opens its own equivalent root document under its normal
`ArResolver` context. Opaque layer keys route exact Sdf field and sublayer
topology changes; local identifiers and resolved paths never cross the wire.
The session layer and layers introduced only by references or payloads are not
part of the synchronized graph.

The contract assumes equivalent initial authored contents. OpenUSDConnect does
not compare or publish a complete baseline, so unchanged fields can
differ silently if resolver contexts or deployed asset versions disagree.
Production integrations should use immutable/versioned assets or an explicit
baseline identity policy before enabling edits.

Construction rejects an anonymous root, an edit target outside the synchronized
graph, and non-portable sublayer paths in the resolvable graph. Missing
sublayers may resolve later; call `refresh_layer_graph()` after resolver or
asset availability changes. Use `is_layer_reachable(layer)` before authoring
into a newly attached layer.

`READY` does not wait for unresolved layers: their records are counted in
`status.deferred_events` / `deferred_layer_keys` and applied by
`refresh_layer_graph()`. A remote topology edit that removes the current edit
target selects the root layer instead.

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
unsaved synchronized edits, including through compaction, while saving file or
resolver-backed layers remains application policy. The compacted log begins
with topology/routing state and authored-content events; it does not establish
that untouched initial assets are identical across clients. Shared-stage
rejection recovery has additional clean-stage requirements described in
[Client recovery](client-recovery.md).

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
`native_scene_rebuild_required` and stops incremental delivery. The high-level
receiver reports it as `RECOVERY_REQUIRED` in `client.status`. Rebuild the
native destination and call
`client.acknowledge_native_scene_rebuilt()` before resuming. An ordinary
reconnect does not clear this guard.

## Identity and authentication

`app_name` creates a stable client identity. Publisher and receiver roles that
belong to one integration should use the same `app_name` or explicit
`client_id`.

TOFU tokens are loaded and saved by default. Set `persist_token=False` for
ephemeral tools or tests, pass `token=` when the host owns credential storage,
and override `ClientObserver.on_token_issued` to integrate with a host-specific
store.

## Low-level APIs

`NoticeEmitter`, `EventSender`, `ReceiverThread`, and `EventDispatcher` remain
public for integrations whose scheduling or continuation requirements cannot
use the high-level clients. `ReceiverThread` requests layered replay by default;
passing `layered_replay=False` selects the single-layer flat contract. Ordinary
native-scene integrations should use `UsdReceiver(adapter=...)` instead of
assembling these components.

Construct low-level objects directly. Components exposed by a high-level
client are diagnostic handles; mutating them bypasses the wrapper's lifecycle
invariants.

Low-level hosts can call `EventSender.request_connect(timeout=2.0)` from a
timer to schedule a handshake without waiting. It returns whether an attempt
was scheduled, not whether the connection succeeded; inspect `connected` and
rejection/recovery status on subsequent ticks. `cancel_connect()` invalidates
pending attempts and reports whether they have finished; `disconnect()` also
closes an established connection. Neither discards the transaction outbox.

## Embed a server

Use `ServerRuntime` when the application owns startup and shutdown:

```python
from openusdconnect import ServerConfig, ServerRuntime

with ServerRuntime(ServerConfig(port=7200, log_path="events.db")) as server:
    print(server.server_address)
    run_application()
```

The context starts the TCP service in the background and stops its services
and workers on exit. Alternatively, `start_server(config)` returns an already
started handle; call `stop()` when finished. A handle is single-use; repeated
`start()` while running and repeated `stop()` are harmless. Use `port=0` for
an OS-selected TCP port, available through `server_address` after startup.
The embedded API installs no process signal handlers. `run_server(config)`
remains the blocking runner used by the command line.
