# USD-native integration contract

`ManagedClient` is the primary entry point for Python applications that
already use `pxr.Usd` and need bidirectional synchronization with server-owned
collaboration layers. `UsdPublisher` (send-only) and `UsdReceiver`
(receive-only) remain available as low-level building blocks for hosts that
need only one direction. All three provide lifecycle and retry behavior while
leaving stage ownership and the application event loop under the host's
control.

## Receive into a stage

```python
from pxr import Usd

from openusdconnect import UsdReceiver

stage = Usd.Stage.Open("shot.usda")

with UsdReceiver(stage, app_name="my-viewer") as receiver:
    if not receiver.wait_connected(timeout=5):
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
`wait_connected()` confirms the handshake only. Replay is applied by later
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
    sphere = UsdGeom.Sphere.Define(stage, "/World/Sphere")
    UsdGeom.Xformable(sphere).AddTranslateOp().Set(Gf.Vec3d(1, 2, 3))
    publisher.update()
```

The publisher sends opinions authored in the stage's current edit-target
layer. Composed values supplied only by references, payloads, or weaker layers
are not copied. A local override on a composed descendant is sent because that
override is an authored opinion in the current edit target.

Flush with `update()` before switching edit targets. One emitted transaction
cannot own opinions from multiple USD layers.

`update()` retains an exact prepared batch until its socket write succeeds.
Edits made while that batch is pending form the next batch. The publisher does
not block an application tick to reconnect automatically; call `connect()`
again after a disconnect. `prepared_event_count` reports only the retained
transport batch; dirty edits that have not been built are not included.
The returned event count confirms a transport write, not an authoritative
server acknowledgement. Observe the corresponding receiver replay when an
application needs end-to-end confirmation.

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
    if not client.wait_connected(timeout=5):
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

### Adapter destination contract

`DCCAdapter.targets_stage()` selects the layered receive path. An adapter that
writes directly into USD must return the exact `Usd.Stage` instance it mutates;
`UsdStageAdapter` implements this contract. When that object is also the
receiver's mirror stage, OpenUSD performs composition and the dispatcher skips
composed projection.

An adapter that writes into an external scene, such as Blender objects, returns
`None`. The dispatcher then authors incoming records into its USD mirror and
projects the mirror's composed result into the adapter. Returning a different
`Usd.Stage` also selects projection because it is a separate destination, even
if it was opened from equivalent layers. The comparison uses object identity,
not layer or identifier equivalence. Custom stage-backed adapters must override
`targets_stage()`; inheriting `None` opts into external-scene projection.

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
    if not client.wait_connected(timeout=5):
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

`wait_connected()` completes both receive and send handshakes, including an
in-memory TOFU token handoff when token persistence is disabled. Code that does
not call `wait_connected()` may connect the sender explicitly with
`start_sender()` or let the first `update()` retry it best-effort.

Call `update()` on the thread that owns the stage. It freezes local authored
changes, applies sequenced server records, restores concurrent local changes,
and publishes one transaction per changed layer. Changes are expressed as
exact Sdf fields, so layer and prim metadata, custom attributes,
relationships, targets, connections, time samples, variants, and removals do
not require specialized event types.

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

### Runtime resolver-context changes

Managed native projection does not incrementally synchronize topology changes
caused only by refreshing a context-dependent `ArResolver` context. Its
previous-state stage shares the immutable base root layer with the live stage
to keep startup, memory use, and normal transaction latency low. A resolver
refresh can therefore recompose both stages before the dispatcher has observed
the former topology.

After changing a context-dependent resolver mapping, the dispatcher logs an
error, sets `native_scene_rebuild_required`, and raises
`NativeSceneRebuildRequired` before draining more receiver input. Recreate
the receiver/dispatcher or rebind a replacement stage, and reconstruct the
native adapter scene from the current composed stage. A destructive full native
resync is equivalent; after it succeeds, call
`acknowledge_native_scene_rebuilt()` to resume delivery. Restarting the
integration is the simplest fallback when it does not expose either operation.
An ordinary network reconnect that retains the same dispatcher does not clear
the guard. Ordinary USD edits, filesystem asset appearance, and explicit
reference or payload changes continue to use their normal incremental paths;
this restriction applies only to composition changes caused solely by a live
resolver-context refresh.

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
