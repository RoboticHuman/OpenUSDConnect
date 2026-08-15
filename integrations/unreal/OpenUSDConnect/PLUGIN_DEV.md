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
│         ─► drains EventQueue with bSuppressEmit=true while applying          │
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
       │   – read framed FB   │         │   – drain SendQueue     │
       │   – echo-suppress    │         │     (SPSC TQueue)       │
       │     by SessionOrigin │         │   – peek with           │
       │   – enqueue raw      │         │     HasPendingData      │
       │     bytes for        │         │     for inbound         │
       │     game-thread      │         │     (RateLimited, …)    │
       │     drain            │         │                         │
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
recognize this plugin instance's own broadcasts; the server still publishes
every committed record to every receiver.

---

## Module map

| File | Class / Symbol | Role |
|------|----------------|------|
| `Public/USDConnectSettings.h` | `UUSDConnectSettings` | UDeveloperSettings exposed at *Edit → Project Settings → Plugins → OpenUSD Connect*. |
| `Public/USDConnectSubsystem.h` | `UUSDConnectSubsystem` | UTickableWorldSubsystem that owns both clients, the event queue, and the stage-actor attachment. |
| `OpenUSDConnectPXR/Public/USDConnectProtocol.h` | `namespace OUC` | Wraps the generated FlatBuffers bindings with framing limits and small Unreal helpers. |
| `Private/SyncClient.h/.cpp` | `FSyncClient` | Receiver TCP thread. Handles HELLO, complete commit-stream reads, ping/rate-limit/resync. |
| `Private/EmitClient.h/.cpp` | `FEmitClient` | Emitter TCP thread. Drains a SPSC SendQueue, peeks for inbound corrections via `HasPendingData`. |
| `Private/TxnBuilder.h/.cpp` | `BuildXformTxnFrame`, `BuildVisibilityTxnFrame`, `BuildConnectableInputTxnFrame` | FlatBuffers Txn frame builders for the supported emitter event kinds. |
| `OpenUSDConnectPXR/Private/OpenUSDConnectPXR.cpp` | `IMPLEMENT_MODULE` | Registers the PXR dynamic module with Unreal's module manager. A successful link does not replace this runtime entry point. |
| `OpenUSDConnectPXR/Public/USDEventApplier.h`, `Private/USDEventApplier.cpp` | `FUSDEventApplier::ApplyFrame` | Decodes a BroadcastEvent frame and runs the matching pxr USD operation inside a `pxr::SdfChangeBlock`. |
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

The plugin includes the flatc-generated C++ bindings under
`OpenUSDConnectPXR/Public/Schema/`. `USDConnectProtocol.h` adds only framing,
verification, version checks, and Unreal-friendly helpers. `TxnBuilder.cpp`
uses the generated `Create*` functions; receive code uses the generated table
accessors.

Run `scripts/generate_flatbuffers.sh` after changing either schema and commit
the regenerated Python and C++ bindings together. The generated C++ header pins
the FlatBuffers runtime version with a `static_assert`; keep
`setup_flatbuffers.py` on the same version so plugin builds fetch compatible
headers.

### Threading & framing rules learned the hard way

- The frame-length prefix is **big-endian** (`struct.pack(">I", ...)` on the server),
  but the FlatBuffers payload itself is little-endian as always.
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
UObject subsystem and settings and remains on Unreal's default no-RTTI build.
Keeping that boundary is required on Clang platforms because Unreal's UObject
base classes do not export C++ RTTI. Neither module enables C++ exceptions
because the plugin code does not require them.

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
