# USD-native integration contract

`ManagedClient` is the primary entry point for Python applications that
already use `pxr.Usd` and need bidirectional synchronization with server-owned
collaboration layers. `UsdPublisher` (send-only) and `UsdReceiver`
(receive-only) remain available as low-level building blocks for hosts that
need only one direction. All three provide lifecycle and retry behavior while
leaving stage ownership and the application event loop under the host's
control.

All high-level clients use the same lifecycle. Context entry calls the
nonblocking `start()` operation; call `connect(timeout)` when the application
needs the applicable transport handshakes to complete or fail explicitly.
Receiving clients can be connected while still replaying because authoritative
records must be applied by `update()` on the stage-owning thread.

`client.status` returns an immutable `ClientStatus` snapshot. Its phase is one
of `OFFLINE`, `CONNECTING`, `REPLAYING`, `READY`, `RECOVERY_REQUIRED`,
`REJECTED`, or `CLOSED`. `receiver_connected` and `sender_connected` expose
partial bidirectional connectivity and are `None` when that role is absent.
Durability counters are cumulative in the status snapshot; the acknowledged
count returned by `SyncUpdate` is the delta consumed by that update call.

## Receive into a stage

```python
from pxr import Usd

from openusdconnect import UsdReceiver

stage = Usd.Stage.Open("shot.usda")

with UsdReceiver(stage, app_name="my-viewer") as receiver:
    if not receiver.connect(timeout=5):
        raise ConnectionError("OpenUSDConnect server is unavailable")

    while application_is_running():
        receiver.update()
```

`UsdReceiver` always requests layered replay from sequence 1. It reconstructs
the server's logical collaboration layers as receiver-owned anonymous
`Sdf.Layer` objects at the strong end of the stage's session-layer stack. If
the server does not negotiate layered replay, the receiver closes and raises
instead of falling back to flat application.

Call `update()` on the thread that owns the stage. The network reader runs in
the background, but it never mutates the stage itself.
`connect()` starts the client and confirms the handshake only. Replay is applied by later
`update()` calls, so startup code should wait for the stage condition it needs.
If the host replaces its stage, call `rebind_stage(new_stage)` on the owning
thread. Managed layers move to the replacement and detach from the old stage.

Callbacks driven by event application (`on_imported`, `on_resync`,
`on_applied`, and `on_applied_events`) run synchronously inside `update()` on
the calling thread. Transport callbacks, including metadata/playback updates
and token issuance, may run on the background receiver or on the thread making
a sender connection; UI integrations must marshal those callbacks explicitly.

The supplied stage must be opened from the original base scene. A generated
live snapshot already contains server state and is rejected because replaying
the full logical history over that baseline would leave duplicate, stale
opinions under the managed layers.

## Publish normal USD edits

Construct the publisher before making edits so its `Usd.Notice` listener sees
them:

```python
from pxr import Gf, Usd, UsdGeom

from openusdconnect import UsdPublisher

stage = Usd.Stage.Open("shot.usda")
stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))

with UsdPublisher(
    stage,
    app_name="my-editor",
    department="layout",
) as publisher:
    if not publisher.connect(timeout=5):
        raise ConnectionError("OpenUSDConnect server is unavailable")
    sphere = UsdGeom.Sphere.Define(stage, "/World/Sphere")
    UsdGeom.Xformable(sphere).AddTranslateOp().Set(Gf.Vec3d(1, 2, 3))
    publisher.update()
```

The publisher sends opinions authored in the stage's current edit-target
layer. Composed values supplied only by references, payloads, or weaker layers
are not copied. A local override on a composed descendant is sent because that
override is an authored opinion in the current edit target.

Call `update()` before switching edit targets. One emitted transaction cannot
own opinions from multiple USD layers.

`update()` is a nonblocking submission call: its returned event count means the
bounded sender outbox owns those events, not that the server has committed
them. A socket failure after an ambiguous write leaves the exact encoded
transaction in that outbox. Reconnecting the same publisher resends it with the
same producer session and transaction ID; the server either commits it once or
returns its already-durable cumulative highwater.

`prepared_event_count` is local work that has not entered the sender outbox.
`pending_event_count` is submitted work not yet durably acknowledged, and
`acknowledged_event_count` is the cumulative acknowledged event count. Use
`flush(timeout)` only at an explicit durability checkpoint such as save,
publish, or orderly shutdown. It reconnects and replays while time remains,
returns `False` on timeout, and raises `TransactionRejectedError` after a
deterministic rejection. `close()` does not silently turn every interactive
update into a durability wait; call `flush()` first when shutdown must be
lossless.

The publisher does not block an application tick to reconnect automatically;
call `connect()` again after a disconnect. Edits made while a transaction is
pending form a later transaction and may continue to be submitted. The bounded
outbox applies backpressure rather than growing without limit.

Use `publish_current_edit_target()` when attaching a publisher after the stage
was already authored. It publishes authored opinions in the current edit
target, not a flattened composed stage. If an earlier transport batch is
retained, retry it with `update()` before requesting this full publication. A
failed full-publication write is retained and retried by `update()` in the same
way.

## Bidirectional managed client

`ManagedClient` runs the publisher and receiver over one application-owned
stage:

```python
from pxr import Gf, Usd, UsdGeom

from openusdconnect import ManagedClient

stage = Usd.Stage.Open("shot.usda", session_layer)
stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))

with ManagedClient(
    stage,
    app_name="my-editor",
    department="layout",
) as client:
    if not client.connect(timeout=5):
        raise ConnectionError("OpenUSDConnect server is unavailable")

    sphere = UsdGeom.Sphere.Define(stage, "/World/Sphere")
    translate = UsdGeom.Xformable(sphere).AddTranslateOp()

    while application_is_running():
        translate.Set(Gf.Vec3d(1, 2, 3))
        update = client.update()  # apply incoming, then publish local edits
```

`update()` applies the authoritative batch first, then publishes the
application's own edits, and returns the event counts for each direction.
The dispatcher suppresses the emitter while authoritative records apply and
invalidates its diff cache afterward, so the server's echo of the
application's own edits is not re-published. The session root itself is
stronger than all session sublayers, so `ManagedClient` redirects an initial
session-root edit target into `client.authoring_layer`, a transient session
sublayer below the receiver-owned managed block. Author into that layer, the
root layer, or another application layer weaker than the managed block. The
client rejects publishing from the strong session root or from a
receiver-owned managed layer.

`SyncUpdate.submitted_events` is newly submitted work,
`acknowledged_events` is work covered by cumulative acknowledgements since the
previous `update()`, and `pending_events` is the current unacknowledged event
count. `applied_events` is authoritative incoming work applied by this call.
`update()` never waits for an ACK. It also
does not publish while receive replay is unsynchronized: after reconnect it
first applies through the server's replay boundary, then resumes submission.
This keeps viewport drags and animation streaming responsive without allowing a
producer that has rolled back or fallen behind to keep authoring blindly.

For high-frequency default-time transforms, set
`transform_coalesce_seconds` to a small host-appropriate window. Only repeated
TRS opinions for the same prim and time code are merged; structural edits,
distinct time samples, and other event kinds remain ordering barriers.

## Bidirectional hosts

The two-stage pattern applies to the low-level `UsdPublisher` +
`UsdReceiver` combination:

```text
native edits -> author stage -> UsdPublisher -> server
                                                |
native scene <- adapter      <- mirror stage <- UsdReceiver
```

Do not attach a layered `UsdReceiver` and a `UsdPublisher` to the same stage
directly. The local edit would remain in its authoring layer and the echoed
authoritative record would also be reconstructed in a managed collaboration
layer. The composed value can look correct while opinion ownership is
duplicated. `ManagedClient` is the supported single-stage form; it places local
authoring below the authoritative managed block and wires the emitter into the
dispatcher so echoes are suppressed and cached instead.

Blender follows this contract today: it authors into one USD stage, rebuilds
server layers in an independent mirror stage, and projects the mirror's
composed result into Blender. A receive-only USD application such as usdview
can apply directly to its displayed stage because that stage is not also its
authoring source.

Native Unreal currently uses the lower-level flat path around its
`USDStageActor`. Flat replay is a single-layer compatibility path: servers with
department policy, multiple collaboration layers, or a muted collaboration
layer reject it. Native Unreal therefore requires a single-layer server until
its integration reconstructs the logical layer stack.

## Shared file-layer editing

`SharedStageClient` is an opt-in contract for applications that open equivalent
copies of the same USD root document and want edits routed back to the original
root or sublayer. It is distinct from managed collaboration-layer replay.

| Contract | Managed `UsdPublisher` / `UsdReceiver` | `SharedStageClient` |
| --- | --- | --- |
| Layer ownership | Receiver-owned collaboration layers | Existing root and recursive sublayers |
| Bidirectional stage | Separate author and mirror stages | One application-owned stage |
| Routing | Logical layer keys and departments | Opaque keys for equivalent local layers |
| Baseline | Base stage plus server event history | Equivalent asset contents on every process |
| VFS | Supported by the managed server | Not supported |

Start a dedicated server in shared-stage mode:

```bash
uv run openusdconnect-server \
  --base /projects/show/shot/shot.usda \
  --layer-mode shared_stage
```

Each client opens its local equivalent under its normal resolver context:

```python
from pxr import Usd

from openusdconnect import SharedStageClient

stage = Usd.Stage.Open("asset://show/shot/shot.usda", resolver_context)

with SharedStageClient(stage, app_name="shot-editor") as client:
    if not client.connect(timeout=5):
        raise ConnectionError("OpenUSDConnect server is unavailable")

    while application_is_running():
        result = client.update()
```

The default tracker is pure Python and keeps complete local layer snapshots so
it works with any compatible OpenUSD Python package. Native USD applications
can build the optional delegate bridge against the exact OpenUSD installation
loaded by the host:

```bash
uv run python -m openusdconnect.build_sdf_notice_bridge
```

The build installs to ~/.openusdconnect/ for auto-discovery.  To override:

```python
with SharedStageClient(
    stage,
    app_name="shot-editor",
    delegate_bridge_path="/custom/path/to/bridge.dll",
) as client:
    ...
```

The extension uses a C ABI but links to OpenUSD's C++ ABI. Its generated
manifest must match the host's OpenUSD version, platform, and architecture;
an incompatible build is rejected before loading. The native path consumes
`SdfLayerStateDelegate` directly and captures old values inline without snapshots.
If its bounded local queue fills, pending records are coalesced into one exact
current-content replacement per affected layer. That rare recovery transaction
has whole-layer conflict scope; ordinary edits remain field-level.

`connect()` completes both receive and send handshakes, including an
in-memory TOFU token handoff when token persistence is disabled. Code that does
not call `connect()` may use `start()` to launch only the background receiver;
the first `update()` retries the sender best-effort after the receive handshake.

Call `update()` on the thread that owns the stage. It freezes local authored
changes, applies sequenced server records, restores concurrent local changes,
and publishes one transaction per changed layer. Changes are expressed as
exact Sdf fields, so layer and prim metadata, custom attributes,
relationships, targets, connections, time samples, variants, and removals do
not require specialized event types.

`SharedStageClient.update()` returns the same `SyncUpdate` type. Shared-stage
publication is also gated on both the
accepted layer graph and the receiver replay boundary. `flush(timeout)` is the
explicit durability checkpoint.

The synchronized graph begins at `stage.GetRootLayer()` and follows recursive
`subLayerPaths`. The session layer and layers introduced only by references or
payloads are outside this graph. Use `client.is_layer_mapped(layer)` before
authoring into a newly attached sublayer. The file's existing contents become
the shared baseline when the topology is accepted; edits made after mapping
produce deltas.

Authored sublayer and asset identifiers remain unchanged. `./` and `../`
continue to anchor to each process's corresponding document, while search-path
identifiers and custom URIs resolve through that process's `ArResolver` plugin
and stage context. Local identifiers and resolved filesystem paths never cross
the wire. If an asset appears after startup, refresh the server mapping and
call `client.refresh_asset_dependency()` where necessary. Unresolved keyed
events remain pending on the client.

The server event log remains authoritative for unsaved edits, but neither the
server nor `SharedStageClient` calls `Sdf.Layer.Save()`. Applications retain
control over file or database persistence and conflict policy. A shared-stage
server requires a portable root layer and does not accept department routing,
managed layered replay, proposals, VFS composition, or purge.

## Asset and resolver requirements

Publisher, server, and receiver should open equivalent base composition and
resolver contexts. Authored asset identifiers remain USD identifiers on the
wire. Relative paths are anchored through their owning file-backed layer;
custom URI and database identifiers remain the responsibility of each
process's `ArResolver` plugin and context.

Use `receiver.refresh_asset_dependency()` after resolver state or an asset
mapping changes. Use `receiver.pending_asset_dependencies` to inspect paths
that still do not resolve.

## Identity and authentication

`app_name` creates a stable client identity. Use the same `app_name`, or pass
the same explicit `client_id`, when publisher and receiver are the two roles
of one integration. Their per-process `origin` values remain distinct unless
you provide them explicitly.

TOFU tokens are loaded and saved by default. Set `persist_token=False` for
ephemeral tools and tests, or pass `token=` when the host owns credential
storage.

## Low-level control

The existing `NoticeEmitter`, `EventSender`, `ReceiverThread`, and
`EventDispatcher` APIs remain public for DCC adapters, flat live-snapshot
continuation on single-layer servers, playback control, proposals, and custom
scheduling. `ReceiverThread` requests layered replay by default; callers must
pass `layered_replay=False` to select the constrained flat contract. New
USD-native clients should begin with `ManagedClient` unless they need a
send-only (`UsdPublisher`) or receive-only (`UsdReceiver`) surface, or one of
those lower-level policies. Construct the low-level objects directly rather
than mutating components inside the high-level wrappers.
