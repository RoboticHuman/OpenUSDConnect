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
`ClientStatus.acknowledged_events_total` is cumulative for the client instance;
`prepared_events` and `pending_events` are current gauges.
`SyncUpdate.acknowledged_events_delta` is the delta consumed by that update
call.

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
application's own edits is not re-published. `ManagedClient` always creates
`client.authoring_layer`, a transient session sublayer below the
receiver-owned managed block, and makes it the stage's edit target. Keep that
edit target while the client is active. This single owned layer makes
authoritative recovery safe and predictable. Applications that intentionally
author persistent or multiple custom layers should compose the lower-level
`UsdPublisher` and `UsdReceiver` APIs instead.

`SyncUpdate.submitted_events` is newly submitted work,
`acknowledged_events_delta` is work covered by cumulative acknowledgements
since the previous `update()`, and `pending_events` is the current
unacknowledged event count. `ClientStatus.acknowledged_events_total` is the
cumulative acknowledged count. `applied_events` is authoritative incoming work
applied by this call.
`update()` never waits for an ACK. It also
does not publish while receive replay is unsynchronized: after reconnect it
first applies through the server's replay boundary, then resumes submission.
This keeps viewport drags and animation streaming responsive without allowing a
producer that has rolled back or fallen behind to keep authoring blindly.

For high-frequency default-time transforms, set
`transform_coalesce_seconds` to a small host-appropriate window. Only repeated
TRS opinions for the same prim and time code are merged; structural edits,
distinct time samples, and other event kinds remain ordering barriers.

A deterministic rejection is reported through `update.recovery` and
`client.status`; it does not interrupt an interactive update loop. The
baseline **Use Server** operation preserves the rejected local layer, clears
that transient layer, starts a fresh producer session, and attempts to restore
producer connectivity within the supplied timeout:

```python
update = client.update()
if update.recovery is not None:
    result = client.recover_use_server(timeout=10)
    result.preserved_authoring_layer.Export("rejected-work.usda")

    # Usually READY immediately; update() retries if reconnect could not finish.
    if not client.connected:
        show_reconnecting()
```

An integration that semantically rebuilds the rejected operation can instead
call `repair_and_resume(events)`, which reuses the rejected transaction ID and
retains its ordered suffix.

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

Each client opens its local equivalent under its normal resolver context.
`connect()` establishes both transports; replay may still be in progress, so
gate shared editing on the high-level phase rather than individual transport
or graph flags:

```python
from pxr import Usd

from openusdconnect import ClientPhase, SharedStageClient

stage = Usd.Stage.Open("asset://show/shot/shot.usda", resolver_context)

with SharedStageClient(stage, app_name="shot-editor") as client:
    if not client.connect(timeout=5):
        raise ConnectionError("OpenUSDConnect server is unavailable")

    while application_is_running():
        result = client.update()
        status = client.status

        if status.phase is ClientPhase.READY:
            enable_shared_editing()
        elif status.phase is ClientPhase.RECOVERY_REQUIRED:
            disable_shared_editing()
            show_recovery_choice(status.recovery, status.reason)
        else:
            disable_shared_editing()
            show_connection_state(status.phase, status.reason)
```

Construction rejects an anonymous root, an initial edit target outside the
root/sublayer graph, and non-portable `subLayerPaths` in the currently
resolvable graph. Errors name the offending layer. Missing sublayers are not
rejected: OpenUSD may resolve them later after an asset or resolver refresh,
and their keyed events remain pending.

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
`ClientPhase.READY` is the single normal-operation signal: both transports are
connected and the authoritative replay boundary has been applied. The lower
level `connected` and `synchronized` properties remain useful for diagnostics,
but integrations should not reconstruct readiness from them.

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

### Shared-stage transaction recovery

A deterministic rejection sets `client.status.phase` to `RECOVERY_REQUIRED`
and quarantines the rejected transaction plus its ordered suffix. Integrations
can inspect the UI-safe `client.status.recovery`, structured
`client.status.failure`, and exact `client.recovery_artifact` without reaching
through the transport.

Call `refresh_recovery_assessment()` on the stage-owning thread to replay
through a fresh server checkpoint and classify each quarantined layer:

```python
assessment = client.refresh_recovery_assessment(timeout=5)

assessment.detached_layers
assessment.unchanged_mapping_layers
assessment.remapped_layers
assessment.source_unavailable_layers
assessment.all_layers_detached

for item in assessment.layers:
    item.source_layer       # application-owned Sdf.Layer, when available
    item.rejected_snapshot  # preserved pre-recovery opinions
```

`unchanged_mapping_layers` remain reachable through the same protocol key.
`remapped_layers` are reachable under a different key, while
`source_unavailable_layers` could not be associated with a local source layer
and should not be confused with an `ArResolver` lookup failure.
Assessments retain direct references to their source layers and anonymous
rejected snapshots so integrations operate on the exact OpenUSD objects rather
than repeating a registry lookup. Those references are released when the
assessment is dropped; the client also releases its active assessment
after completion or `close()`.

Detachment is a fact, not permission to discard work: a later topology change
can reattach the same file layer. The library therefore does not overwrite or
clear application-owned layers in place. The baseline **Use Server** operation
requires a clean equivalent stage and replays authoritative history onto it:

```python
clean_stage = open_equivalent_stage_with_distinct_layer_identifiers()
assessment = client.recover_use_server(clean_stage=clean_stage, timeout=5)

for snapshot in assessment.rejected_snapshots:
    snapshot.Export("rejected-work.usda")
```

Opening the same asset path again in the same process is generally not a clean
stage: OpenUSD's layer registry can return the same loaded `Sdf.Layer`
objects. For filesystem assets, use an isolated copy of the root/sublayer tree
that preserves relative authored paths. Resolver-backed integrations should
open an equivalent snapshot whose resolved layer identifiers do not overlap
the rejected stage. `recover_use_server()` rejects overlap rather than
silently clearing application-owned layers.

A filesystem-backed integration can make that requirement explicit without
putting copy policy into OpenUSDConnect:

```python
from pathlib import Path
import shutil

from pxr import Usd


def open_filesystem_recovery_stage(baseline_tree, root_relative, destination):
    baseline_tree = Path(baseline_tree).resolve()
    destination = Path(destination).resolve()
    shutil.copytree(baseline_tree, destination)
    return Usd.Stage.Open(str(destination / root_relative))


clean_stage = open_filesystem_recovery_stage(
    "/projects/show/shot",
    "shot.usda",
    recovery_workspace / "authoritative-snapshot",
)
assessment = client.recover_use_server(clean_stage=clean_stage, timeout=5)
replace_stage_in_host(client.stage)
```

`baseline_tree` must be the clean collaboration baseline, not a workspace to
which rejected edits have already been saved. The server log is then replayed
over that baseline to reconstruct the authoritative state.

For a custom resolver, snapshot selection belongs in the resolver-facing
integration. The snapshot must produce distinct layer identifiers, not merely
open the live URI again under a second Python variable:

```python
checkpoint = request_authoritative_snapshot(client.last_seq)
snapshot_root = make_snapshot_identifier(live_root_identifier, checkpoint)
snapshot_context = make_snapshot_resolver_context(checkpoint)
clean_stage = Usd.Stage.Open(snapshot_root, snapshot_context)

assessment = client.recover_use_server(clean_stage=clean_stage, timeout=5)
replace_stage_in_host(client.stage)
```

Here `make_snapshot_identifier()` and `make_snapshot_resolver_context()` are
application/resolver functions. For example, a resolver may map a versioned
`studio://snapshots/<checkpoint>/shot.usda` identifier to an immutable asset
revision. Keeping the checkpoint in the identifier also prevents the OpenUSD
layer registry from returning the rejected live layer.

An integration can implement its own merge policy instead of replacing the
whole stage. Run the following on the stage-owning thread, after authoring has
been disabled. Do not call `update()` again until `complete_recovery()` returns:

```python
assessment = client.refresh_recovery_assessment(timeout=5)

for item in assessment.layers:
    if item.source_layer is None:
        handle_unavailable_source(item)  # Application policy.
        continue

    reconcile_source_layer(             # Application-defined merge or reset.
        item.source_layer,
        item.rejected_snapshot,
    )

client.complete_recovery(assessment)
```

`complete_recovery()` verifies incident identity and receiver
synchronization, but deliberately cannot infer whether application-specific
USD reconciliation was semantically correct. To rebuild the rejected
transaction at its original ordered ID instead, call
`repair_and_resume(events, layer=current_layer)`. The assessment retains the
rejected snapshots, so keep it for as long as those snapshots are needed.
Baseline Use Server recovery attempts producer reconnection before
returning; if it cannot finish within the remaining timeout, recovery remains
resolved and the normal `update()` loop retries connectivity. After specialized
recovery completes, resume the normal `update()` loop.

The receive thread remains active during that checkpoint. Records committed
after the final assessment stay queued and are applied by the first resumed
`update()`. Calling `update()` before completion instead advances the applied
sequence or graph revision, so completion rejects the assessment as stale and
the integration must assess and reconcile again.

Reconcile every source layer listed by the final assessment, including
`detached_layers`. Detachment only removes an edge from the current layer
graph; it does not clear or transfer the application-owned `Sdf.Layer`. The
server can later reattach that asset under its stable layer key, at which point
any unreconciled local opinions become visible again and may be published as
new edits. Exporting rejected work is not itself reconciliation: replace,
merge, or clear the rejected opinions according to application policy before
calling `complete_recovery()`.

Explicit recovery commands report expected policy failures through one public
exception with stable codes; normal `update()` calls continue to report a
required recovery through `status.recovery` without raising it:

```python
from openusdconnect import RecoveryError

try:
    assessment = client.recover_use_server(clean_stage=clean_stage, timeout=5)
except RecoveryError as exc:
    if exc.code == "shared_loaded_layers":
        clean_stage = open_isolated_stage_copy()
    else:
        show_recovery_error(exc.code, str(exc))
except (TimeoutError, ConnectionError):
    show_retry_later()
```

A complete UI flow can stay phase-driven and reserve exceptions for explicit
button actions:

```python
def tick(client):
    update = client.update()  # does not raise for an ordinary rejection
    status = client.status
    set_sync_badge(status.phase, status.pending_events)

    if status.phase is ClientPhase.READY:
        set_authoring_enabled(True)
    elif status.phase is ClientPhase.RECOVERY_REQUIRED:
        set_authoring_enabled(False)
        show_recovery_panel(
            incident=status.recovery,
            reason=status.reason,
            on_inspect=lambda: client.refresh_recovery_assessment(timeout=5),
        )
    else:
        set_authoring_enabled(False)
    return update


def choose_use_server(client):
    try:
        clean_stage = open_clean_equivalent_snapshot()
        assessment = client.recover_use_server(clean_stage=clean_stage, timeout=5)
    except RecoveryError as exc:
        show_recovery_error(exc.code, str(exc))
        return
    except (TimeoutError, ConnectionError):
        show_retry_later()
        return

    for index, snapshot in enumerate(assessment.rejected_snapshots):
        offer_export(snapshot, f"rejected-work-{index}.usda")
    replace_stage_in_host(client.stage)
```

Specialized integrations can replace the Use Server button with merge/export
logic based on `refresh_recovery_assessment()`, then call
`complete_recovery(assessment)`. That call resolves the rejected producer
session only after the integration has reconciled its application stage. It
does not attempt to judge whether an application-specific USD merge was
semantically correct.

The stable recovery codes are `no_incident`, `wrong_recovery_kind`,
`stale_assessment`, `stage_not_synchronized`,
`invalid_clean_stage`, `shared_loaded_layers`, `invalid_repair_target`,
`local_changes_pending`, `transactions_pending`, `stage_unavailable`,
and `edit_target_changed`.

The synchronized graph begins at `stage.GetRootLayer()` and follows recursive
`subLayerPaths`. The session layer and layers introduced only by references or
payloads are outside this graph. Use `client.is_layer_reachable(layer)` before
authoring into a newly attached sublayer. The file's existing contents become
the shared baseline when the topology is accepted; edits made after mapping
produce deltas.

Authored sublayer and asset identifiers remain unchanged. `./` and `../`
continue to anchor to each process's corresponding document, while search-path
identifiers and custom URIs resolve through that process's `ArResolver` plugin
and stage context. Local identifiers and resolved filesystem paths never cross
the wire. If an asset appears after startup, refresh the server mapping and
call `client.refresh_layer_graph()` where necessary. Unresolved keyed
events remain pending on the client.

The server event log remains authoritative for unsaved edits, but neither the
server nor `SharedStageClient` calls `Sdf.Layer.Save()`. Applications retain
control over file or database persistence and conflict policy. A shared-stage
server requires a portable root layer and does not accept department routing,
managed layered replay, VFS composition, or purge.

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
continuation on single-layer servers, playback control, and custom
scheduling. `ReceiverThread` requests layered replay by default; callers must
pass `layered_replay=False` to select the constrained flat contract. New
USD-native clients should begin with `ManagedClient` unless they need a
send-only (`UsdPublisher`) or receive-only (`UsdReceiver`) surface, or one of
those lower-level policies. Construct the low-level objects directly rather
than mutating components inside the high-level wrappers.
