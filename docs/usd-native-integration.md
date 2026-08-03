# USD-native integration contract

`UsdPublisher` and `UsdReceiver` are the high-level entry points for Python
applications that already use `pxr.Usd`. They provide lifecycle and retry
behavior while leaving stage ownership and the application event loop under
the host's control.

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

## Bidirectional hosts

Use separate USD stages for authoring and authoritative receive state:

```text
native edits -> author stage -> UsdPublisher -> server
                                                |
native scene <- adapter      <- mirror stage <- UsdReceiver
```

Do not attach a layered `UsdReceiver` and a `UsdPublisher` to the same stage.
The local edit would remain in its authoring layer and the echoed authoritative
record would also be reconstructed in a managed collaboration layer. The
composed value can look correct while opinion ownership is duplicated.

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
USD-native clients should begin with `UsdPublisher` and `UsdReceiver` unless
they need one of those lower-level policies. Construct the low-level objects
directly rather than mutating components inside the high-level wrappers.
