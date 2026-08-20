# OpenUSD Connect Plugin Developer Notes

End-user installation/usage docs live in [`README.md`](README.md). This file documents the
plugin's internals: architecture, threading, protocol, and the rough edges that future
work will need to address.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                            UUSDConnectSubsystem                              │
│                       (UTickableWorldSubsystem, game thread)                 │
│                                                                              │
│  Initialize() ─► defers Connect() to first safe Tick (bPendingAutoConnect)   │
│                                                                              │
│  Tick() ─► waits for World->bIsWorldInitialized                              │
│         ─► fires deferred Connect()                                          │
│         ─► finds AUsdStageActor → AttachToStageActor() subscribes to         │
│            FUsdListener::OnObjectsChanged                                    │
│         ─► pops validated frames with bSuppressEmit=true while applying      │
│                                                                              │
│  OnObjectsChanged() ─► queues exact changed Sdf paths                         │
│  Tick()             ─► drains those paths unless bSuppressEmit is true       │
│                     ─► reads TRS / visibility / shader inputs                │
│                     ─► encodes Txn frames and pushes to FEmitClient          │
│                                                                              │
└──────────────────┬─────────────────────────────────┬─────────────────────────┘
                   │                                 │
       ┌───────────▼──────────┐         ┌────────────▼────────────┐
       │     FSyncClient      │         │      FEmitClient        │
       │     (FRunnable)      │         │      (FRunnable)        │
       │  role = "receiver"   │         │   role = "emitter"      │
       │                      │         │                         │
       │  TCP recv loop:      │         │  TCP loop:              │
       │   – read framed FB   │         │   – claim shared frames │
       │   – verify once      │         │     from native outbox  │
       │   – enqueue bytes +  │         │   – wake on enqueue     │
       │     trusted metadata │         │   – peek with           │
       │   – direct TryPop    │         │     HasPendingData      │
       │     on game thread   │         │     for inbound results │
       │                      │         │     and rate limits     │
       └──────────────────────┘         └─────────────────────────┘
                  │                                  ▲
                  ▼                                  │
                  └──────── TCP 127.0.0.1:7200 ──────┘
                                  │
                       Python OpenUSDConnect server
```

Both threads share a stable project/machine `ClientId` and one endpoint-scoped
producer session ID. The session ID is sent as diagnostic `origin` on both
sockets and as `producer_session_id` by the emitter. The receiver uses it to
attribute this plugin instance's broadcasts; the server publishes every committed
record to every receiver, and the subsystem suppresses notice emission while it
applies received frames.

---

## Module map

| File | Class / Symbol | Role |
|------|----------------|------|
| `Public/USDConnectSettings.h` | `UUSDConnectSettings` | UDeveloperSettings exposed at *Edit → Project Settings → Plugins → OpenUSD Connect*. |
| `Public/USDConnectSubsystem.h` | `UUSDConnectSubsystem` | UTickableWorldSubsystem that owns both clients and the stage-actor attachment; it drains the receiver session on the game thread. |
| `OpenUSDConnectPXR/Public/USDConnectProtocol.h` | `namespace OUC` | Wraps the generated FlatBuffers bindings with framing limits and small Unreal helpers. |
| `Private/SyncClient.h/.cpp` | `FSyncClient` | Receiver TCP thread. Handles HELLO, verifies each frame once, and queues bytes with trusted sequence/event metadata. |
| `Private/EmitClient.h/.cpp` | `FEmitClient` | Emitter TCP thread. Claims shared immutable frames, wakes immediately on enqueue, and peeks for inbound results via `HasPendingData`. |
| `native/client_core` (repository root) | `OrderedProducerSession`, `OrderedReceiverSession`, `FrameDecoder` | Canonical C++ ordering, reconnect generation, recovery, replay, queue, and framing state shared by nanobind and the staged Unreal build. |
| `Private/TxnBuilder.h/.cpp` | `BuildXformTxnFrame`, `BuildVisibilityTxnFrame`, `BuildConnectableInputTxnFrame` | FlatBuffers Txn frame builders for the supported emitter event kinds. |
| `OpenUSDConnectPXR/Private/OpenUSDConnectPXR.cpp` | `IMPLEMENT_MODULE` | Registers the PXR dynamic module with Unreal's module manager. A successful link does not replace this runtime entry point. |
| `OpenUSDConnectPXR/Public/USDEventApplier.h`, `Private/USDEventApplier.cpp` | `FUSDEventApplier::ApplyValidatedFrame` | Applies a boundary-verified BroadcastEvent without repeating FlatBuffers verification; the subsystem manages `pxr::SdfChangeBlock` runs from queued metadata. |
| `OpenUSDConnectPXR/Public/USDStageBridge.h`, `Private/USDStageBridge.cpp` | `FUSDStageBridge` | Keeps direct pxr stage reads and writes out of the no-RTTI UObject module. |
| `OpenUSDConnectPXR/Public/USDMaterialXMaterializer.h`, `Private/USDMaterialXMaterializer.cpp` | `FUSDMaterialXMaterializer` | Maintains Unreal-local MaterialX documents for inline networks. |
| `OpenUSDConnect.uplugin`, `Source/*/*.Build.cs` | - | Registers the runtime and PXR modules and their engine dependencies. |

---

## Wire protocol

```
Frame: [4-byte big-endian uint32 length][N bytes FlatBuffers Envelope]
```

The authoritative message and event definitions are
`openusdconnect/schema/messages.fbs` and `events.fbs`. Avoid copying numeric
union discriminants into documentation; generated bindings expose the named
enums and the values can change when the schema grows.

| Payload | Direction | Meaning |
|----------------|-----------|---------|
| `Hello` / `HelloOk` | both | role, protocol/schema compatibility, authentication, layer mode, and producer-session handshake |
| `AuthRejected` / `HelloRejected` | S→C | authentication or capability rejection |
| `Txn` | C→S | ordered producer transaction |
| `TransactionResult` | S→C | cumulative durable acknowledgement or deterministic rejection |
| `BroadcastEvent` | S→C | one sequenced authoritative event |
| `ReplayComplete` | S→C | exact replay-to-live boundary |
| `Resync` | S→C | discard the old replay position and rebuild |
| `Ping` / `RateLimited` | S→C | connection health and backpressure |

### `EventWrapper { event_type: uint8; event: EventPayload union }`

The event union is generated from `events.fbs`. The subset implemented in each
direction is summarized in [`README.md`](README.md#supported-events).

### Generated FlatBuffers bindings

The shared native core includes the flatc-generated C++ bindings under
`include/openusdconnect/client/schema/`. `protocol_codec.h` provides transport-neutral,
borrowed receive views and caller-owned builders. `USDConnectProtocol.h` adds only
Unreal-friendly string and array helpers. `TxnBuilder.cpp` converts Unreal-native
values into the shared stateless event builders; receive code classifies verified
handshake and control messages through the shared views.

Run `scripts/generate_flatbuffers.sh` after changing either schema and commit
the regenerated Python and C++ bindings together. The generated C++ header pins
the FlatBuffers runtime version with a `static_assert`; keep
`setup_flatbuffers.py` on the same version so plugin builds fetch compatible
headers.

### Threading & framing rules learned the hard way

- The frame-length prefix is **big-endian** (`struct.pack(">I", ...)` on the server),
  but the FlatBuffers payload itself is little-endian as always.
- Emitter builders call `FinishSizePrefixed`, rewrite only that four-byte prefix
  to big-endian, and detach FlatBuffers' allocation into `FWireFrame`. The outbox
  shares that immutable allocation through reconnect and acknowledgement; do not
  materialize a second `TArray<uint8>`.
- The frame size limit is **16 MiB** (`OUC::kMaxFrameSize`) match the server's value.
- Emitter and receiver each open their own TCP socket. `client_id` is the stable
  authentication and producer identity. `origin` is diagnostic metadata, while
  the emitter's `producer_session_id` provides exactly-once transaction identity.
  The plugin reuses one endpoint-scoped producer session across ordinary
  reconnects and creates a new one after changing endpoint or department.

---

## Editor-vs-PIE behaviour

Two non-obvious facts:

1. **`UTickableWorldSubsystem::IsTickableInEditor()` defaults to `false`.** Without
   overriding it, the subsystem only ticks during PIE. We override it to `true` so
   sync works in edit mode.
2. **`AUsdStageActor` only generates Unreal scene components when its `StageState` is
   `OpenedAndLoaded`.** With `Opened`, the prims exist in the pxr stage but there are
   no `USceneComponent`s to receive transform updates. The early-return at
   `USDStageActor.cpp:1320–1340` is the authoritative source for this behaviour.

The subsystem `Initialize()` is intentionally lightweight (just generates IDs and sets
`bPendingAutoConnect`). The real connect happens on the first tick that observes
`World->bIsWorldInitialized && !World->bIsTearingDown`. Spawning `FRunnableThread`s
from within `Initialize()` was previously found to race with editor startup and
deadlock loading at ~90 %.

---

## Echo / feedback-loop guards

Two independent guards keep changes from bouncing forever:

1. **`FSyncClient`** compares the incoming BroadcastEvent's `origin` against its own
   `SessionOrigin` and drops the frame if they match. This catches the server's
   echo of our own emits.
2. **`UUSDConnectSubsystem::bSuppressEmit`** (a `std::atomic<bool>`) is set while
   `DrainAndApply()` and plugin-owned USD authoring are running. The attached
   `FUsdListener::OnObjectsChanged` callback ignores notices during that window,
   preventing received changes and local MaterialX support opinions from being
   emitted back to the server.

The listener reports exact Sdf paths. They are coalesced in `PendingEmitPaths`
and drained once per tick, avoiding the ancestor roll-up behavior of
`AUsdStageActor::OnPrimChanged`.

---

## Build configuration

`OpenUSDConnectPXR.Build.cs` configures the USD SDK through the engine helper:

```csharp
UnrealBuildTool.Rules.UnrealUSDWrapper.CheckAndSetupUsdSdk(Target, this);
```

That call configures USD SDK availability and memory-overload definitions.
`OpenUSDConnectPXR` owns the pxr-facing implementation and enables RTTI,
matching Unreal Engine's pure C++ USD modules. `OpenUSDConnect` contains the
UObject subsystem and settings and remains on Unreal's default no-RTTI,
exceptions-disabled build. Keeping that boundary is required on Clang platforms
because Unreal's UObject base classes do not export C++ RTTI. The portable core
reports boundary failures through status values and uses assertions only for
internal invariants.

The canonical implementation lives at `native/client_core` in the repository.
`producer_session.h` and `receiver_session.h` are payload-generic templates:
Python instantiates them with owned references to immutable Python `bytes`, while
Unreal instantiates them with `TSharedPtr<const FWireFrame>` and
`FValidatedReceiverFrame`. This removes Python boundary copies, preserves
Unreal's zero-copy producer frame and move-only receiver queue, and avoids
maintaining a second connection-state implementation.
`protocol_codec.h` also owns the shared handshake/control classification and
transaction envelope construction. It borrows receive buffers and operates on a
caller-supplied FlatBuffers builder, leaving transport, allocation, threading,
and offset storage to the integration.
The Unreal packaging harness copies it into the temporary plugin source tree
before `BuildPlugin`; the staged copy is an artifact and is never maintained as
a second source. Repository CMake compiles the canonical files directly into
the nanobind extension.

The PXR module's `PublicSystemIncludePaths` exposes the pinned, plugin-local
FlatBuffers headers installed by `setup_flatbuffers.py` to both modules.

---

## Known gaps / future work

- **Emitter coverage is narrower than receiver coverage.** Unreal currently
  emits TRS, visibility, and edited connectable input values. Geometry,
  connection topology, prim lifecycle, composition arcs, and variants flow
  server to Unreal but are not authored back from Unreal yet.
- **Single stage actor.** `TActorIterator<AUsdStageActor>` picks the first one in
  the world. If multiple stage actors are present (e.g. one per layer file), only
  the first gets live sync. Future: match by `RootLayer` against the connected
  server's base file.

---

## Diagnostics

Default-on log categories carry only state-change events (connect, handshake,
disconnect, stage-actor attach). Per-event chatter is at `Verbose`. To see traffic in
the Output Log:

```
Log LogUSDConnect            Verbose
Log LogUSDConnectSubsystem   Verbose
Log LogUSDEmit               Verbose
Log LogUSDEventApplier       Verbose
```

The server-side `--dashboard-port 8080` web UI is invaluable for confirming events are
flowing through the server when debugging client-side silence.
