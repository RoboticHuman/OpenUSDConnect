# Python client and host-integration API

These APIs attach OpenUSDConnect to an application-owned `pxr.Usd.Stage`.
Call `update()` from the stage-owning thread. Socket readers, automatic
reconnect attempts, and (by default, `background_send=True`) transaction socket
writes run on background threads. Encoding and USD work stay on the calling
thread; an update is not a bounded-time task.

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

All high-level clients use the same lifecycle:

1. Construction validates the stage and initializes the role-specific stage
   state.
2. `start()` returns immediately. It starts the background receiver for
   `UsdReceiver`, `ManagedClient`, and `SharedStageClient`; `UsdPublisher`
   opens no socket until its first `update()`. Entering a context manager
   calls `start()`.
3. `wait_until_ready(timeout)` starts the client and pumps `update()` until
   it reaches `READY`. `connect(timeout)` only waits for the handshakes; for
   receiving clients it does not apply queued replay.
4. `update()` applies incoming work and submits local work without waiting
   for a durable acknowledgement. Every call returns a `SyncUpdate`. While a
   sender is disconnected, `update()` schedules a background handshake (after
   the receiver has connected, for bidirectional clients). Repeated calls use a
   single attempt with retry backoff. Auth, protocol, and recovery rejections
   stop automatic retries.
5. `flush(timeout)` waits for already submitted work. It can succeed while
   unsubmitted stage edits remain. `submit_and_wait(timeout)` publishes noticed
   edits, then waits for durability.
6. `close()` stops networking. It does not implicitly turn every pending edit
   into a blocking flush.

`client.status` is an immutable `ClientStatus`; `phase` is one of
`OFFLINE`, `CONNECTING`, `REPLAYING`, `READY`, `RECOVERY_REQUIRED`, `REJECTED`,
`PARKED`, or `CLOSED`. `PARKED` means a `ManagedClient` or `UsdReceiver` has no
bound stage, even if its networking is connected. Enable synchronized editing
only in `READY`. The directional connection fields distinguish partial
connectivity from a role that is not present.

The blocking helpers `wait_until_ready()` and `submit_and_wait()` default to a
10 second timeout and return `False` only when it expires. States that more
updates cannot resolve raise instead, matching `connect()` and `flush()`:
`PermissionError` for authentication rejection, `ConnectionError` for a
rejected handshake, `TransactionRejectedError` for a rejected transaction, and
`RuntimeError` for a closed client, a parked stage, or a required native-scene
rebuild. GUI integrations should use the timer-driven pattern below instead of
waiting.

`status.has_unsent_changes` includes dirty edits not yet prepared and retained
publication batches. `status.pending_events` counts submitted edits awaiting
acknowledgement. Neither count alone establishes that local work is finished.
`status.edit_target_is_shared` indicates whether the current USD edit target
participates in this client's publication scope, independently of readiness.

`ClientPhase`, `ClientStatus`, and `SyncUpdate` are available from
`openusdconnect.client_types` and the package root. `client.client_id` exposes
the connection identity without accessing an underlying transport object.

Keep lifecycle and stage operations on the host's owning thread. Every
application callback runs during `update()` on that thread: apply callbacks
(`on_imported`, `on_resync`, `on_applied`, `on_applied_events`) and, by
default, token, metadata, and playback notifications, which the client queues
from its network threads. Internal credential sharing and persistence remain
immediate. A raising callback propagates out of `update()`; notifications
queued after it are delivered by the next `update()`. Closing discards
undelivered notifications. Pass `callbacks_on_update=False` only when the
callbacks are thread-safe and must observe network events without an update
pump; they then run on the thread handling the handshake or message.
Inside a receiver's apply callback, `receiver.applying_seq` is the candidate
batch tail; `last_seq` advances only after the complete apply succeeds.

An adapter-backed `UsdReceiver` also enters `RECOVERY_REQUIRED` when resolver
recomposition makes incremental projection unsafe. Rebuild the native scene,
then call `acknowledge_native_scene_rebuilt()`.

`update()` returns a `SyncUpdate` describing the work done by that call;
directional clients report zero for the direction they lack. Read state such
as unsent work or deferred records from `client.status`.

- `applied_events`: authoritative events applied during this call
- `submitted_events`: local events accepted by the sender outbox
- `acknowledged_events_delta`: newly consumed durable acknowledgements
- `pending_events`: currently submitted but unacknowledged events
- `recovery`: a deterministic rejection that requires application action

### GUI scheduling

The host owns its timer and USD thread. Construction and `start()` do not wait
for synchronization:

```python
client = ManagedClient(stage, app_name="my-editor").start()

def on_timer():
    if not client.edit_target_is_shared:
        # ManagedClient.update() refuses to publish from another layer.
        set_editing_enabled(False)
        show_error("restore client.authoring_layer as the edit target")
        return
    client.update()
    status = client.status
    set_editing_enabled(status.phase is ClientPhase.READY)
    show_sync_status(status)
```

`SharedStageClient` accepts any edit target: session-layer edits stay local,
and `status.edit_target_is_shared` lets its UI distinguish them from
synchronized edits, so its loop calls `update()` unconditionally.

Background transaction sending uses the existing bounded outbox and preserves
transaction order and reconnect replay. It does not make encoding, USD replay,
explicit `connect()`, recovery, or playback commands asynchronous. Bidirectional
updates still apply the complete queued authoritative prefix before publishing;
large replays can take multiple frame budgets of CPU time. Pass
`background_send=False` to write transactions on the calling thread instead.

### Finish local work and close

Stop authoring before a blocking finish operation, and call it on the stage's
owning thread:

```python
if not client.submit_and_wait(timeout=5):
    show_unfinished_changes(client.status)
else:
    client.close()
```

The helper keeps pumping replay and retained local batches, releases
transform coalescing, and waits for durable acknowledgements. It returns
`False` only on timeout, leaving work retained for retry, and raises for the
blocked states listed above. The budget bounds waiting between updates; it
cannot interrupt USD work or synchronous writes when background sending is
disabled. Callbacks must not continue creating edits indefinitely while
finishing.

Completion guarantees publication durability, not that the receive stage has
already applied the acknowledgement's authoritative echo or resolved every
asset. In a GUI that must remain interactive during shutdown, disable authoring
and keep its timer running until both unsent and pending work are empty, then
close. Present rejection and timeout choices to the user instead of discarding
outstanding work implicitly.

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

Interactive receive-only applications may bound one tick's work with
`receiver.update(max_messages=500)`. Ordered replay remains pending until all
messages preceding the server's synchronization watermark have been applied.
Bidirectional clients intentionally drain their complete queued prefix before
publishing local edits, so this budget applies only to `UsdReceiver`.

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
    on_resync=adapter.reset,
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

While disconnected, `UsdPublisher.update()` leaves noticed edits dirty and
schedules a background handshake; a later `update()` submits them.
`disconnect()` closes the socket and pauses those automatic attempts until the
next `connect()`.

Use `publish_current_edit_target()` when attaching to a layer that was already
authored before the publisher existed. It publishes authored opinions, not a
flattened composed stage. While disconnected, the snapshot stays prepared until
an `update()` can submit it. Retry any retained batch with `update()` first.

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

`publish_current_edit_target()` explicitly captures the current authoring layer
and follows the same replay/readiness gate as ordinary updates. A zero return
may mean the snapshot is retained until a later update can submit it.

`rebind_stage(new_stage)` and `rebind_stage(None)` refuse unfinished local work.
Use `submit_and_wait()` before switching documents. If the host deliberately
abandons unsent edits, pass `discard_unsent=True`; submitted transactions still
need acknowledgement before rebinding. A new stage receives a fresh authoring
layer, and `None` parks stage application while networking remains active.

Closing a managed client detaches its authoritative collaboration layers. It
leaves the local authoring layer and selected edit target on the old stage, so
the composed scene may reveal older local opinions. To preserve the composition
currently visible to the user, explicitly create an independent snapshot before
closing, then let the host adopt or export it:

```python
visible_snapshot = Usd.Stage.Open(client.stage.Flatten())
client.close()
replace_stage_in_host(visible_snapshot)
```

This deliberately flattens the currently composed scene; it is not a replacement
for preserving authored layer structure or finishing outstanding publications.
`SharedStageClient.close()` leaves application-owned authored layers in place.

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

`READY` means the replay checkpoint has been processed, even if some records
must wait for unresolved layers. `status.deferred_events` and
`status.deferred_layer_keys` expose that incomplete content without preventing
editing of available layers. `refresh_layer_graph()` retries these records.
Remote topology edits can select the root edit target if the previous target
is no longer in the layer stack; read `stage.GetEditTarget()` when refreshing
the host's layer UI. Both clients expose `claim_playback()` and
`send_playback_control()` alongside the playback callbacks.

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
client exposes this through `client.native_scene_rebuild_required` and
`client.status`. Rebuild the native destination and call
`client.acknowledge_native_scene_rebuilt()` before resuming. An ordinary
reconnect does not clear this guard.

## Identity and authentication

`app_name` creates a stable client identity. Publisher and receiver roles that
belong to one integration should use the same `app_name` or explicit
`client_id`.

TOFU tokens are loaded and saved by default. Set `persist_token=False` for
ephemeral tools or tests, pass `token=` when the host owns credential storage,
and use `on_token_issued` to integrate with a host-specific store.

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
