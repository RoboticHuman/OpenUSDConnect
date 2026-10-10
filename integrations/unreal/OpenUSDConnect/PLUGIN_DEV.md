# Unreal plugin developer notes

See [README.md](README.md) for installation and usage. This document describes
the plugin's modules, threading model, protocol implementation, and known gaps.

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
│         ─► drains notifications and receiver frames, applying with           │
│            bSuppressEmit=true                                                │
│                                                                              │
│  OnObjectsChanged() ─► queues exact changed Sdf paths                         │
│  Tick()             ─► drains those paths unless bSuppressEmit is true       │
│                     ─► reads TRS / visibility / shader inputs                │
│                     ─► encodes Txn frames and appends them to the producer   │
│                                                                              │
└──────────────────┬─────────────────────────────────┬─────────────────────────┘
                   │                                 │
       ┌───────────▼──────────┐         ┌────────────▼────────────┐
       │   FEndpointRunner    │         │    FEndpointRunner      │
       │  <ReceiverEndpoint>  │         │   <ProducerEndpoint>    │
       │     (FRunnable)      │         │      (FRunnable)        │
       │                      │         │                         │
       │  applies the         │         │  applies the            │
       │  endpoint's actions  │         │  endpoint's actions     │
       │  with an FSocket and │         │  with an FSocket; an    │
       │  reports bytes,      │         │  Append wakes it        │
       │  timeouts, and time  │         │                         │
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

## Module map

| File | Class / Symbol | Role |
|------|----------------|------|
| `Public/USDConnectSettings.h` | `UUSDConnectSettings` | UDeveloperSettings exposed at *Edit → Project Settings → Plugins → OpenUSD Connect*. |
| `Public/USDConnectSubsystem.h` | `UUSDConnectSubsystem` | UTickableWorldSubsystem that owns both endpoints, their runners, and the stage-actor attachment; it drains notifications and receiver frames on the game thread. |
| `OpenUSDConnectPXR/Public/USDConnectProtocol.h` | `namespace OUC` | Wraps the generated FlatBuffers bindings with framing limits and small Unreal helpers. |
| `Private/EndpointRunner.h/.cpp` | `FEndpointRunner<Endpoint>` | One `FRunnable` per role. Applies the endpoint's actions with an `FSocket` and reports bytes, read timeouts, and time; it holds no protocol state. |
| `native/client_core` (repository root) | `ReceiverEndpoint`, `ProducerEndpoint` | Sans-IO connection protocol (handshake, replay, outbox, recovery, reconnect) shared with the Python module, built here as the `OpenUSDConnectClientCore` module. |
| `Private/TxnBuilder.h/.cpp` | `BuildXformTxnFrame`, `BuildVisibilityTxnFrame`, `BuildConnectableInputTxnFrame` | FlatBuffers Txn frame builders for the supported emitter event kinds. |
| `OpenUSDConnectPXR/Private/OpenUSDConnectPXR.cpp` | `IMPLEMENT_MODULE` | Registers the PXR dynamic module with Unreal's module manager. A successful link does not replace this runtime entry point. |
| `OpenUSDConnectPXR/Public/USDEventApplier.h`, `Private/USDEventApplier.cpp` | `FUSDEventApplier::ApplyValidatedFrame` | Applies a boundary-verified BroadcastEvent without repeating FlatBuffers verification; the subsystem manages `pxr::SdfChangeBlock` runs from queued metadata. |
| `OpenUSDConnectPXR/Public/USDStageBridge.h`, `Private/USDStageBridge.cpp` | `FUSDStageBridge` | Keeps direct pxr stage reads and writes out of the no-RTTI UObject module. |
| `OpenUSDConnectPXR/Public/USDMaterialXMaterializer.h`, `Private/USDMaterialXMaterializer.cpp` | `FUSDMaterialXMaterializer` | Maintains Unreal-local MaterialX documents for inline networks. |
| `OpenUSDConnect.uplugin`, `Source/*/*.Build.cs` | - | Registers the client core, runtime, and PXR modules and their engine dependencies. |

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
values into the shared stateless event builders. The endpoints handle handshake and
control messages; the subsystem only tells `BroadcastEvent` from `Resync` in the
frames it drains.

Run `scripts/generate_flatbuffers.sh` after changing either schema and commit
the regenerated Python and C++ bindings together. The generated C++ header pins
the FlatBuffers runtime version with a `static_assert`; keep
`setup_flatbuffers.py` on the same version so plugin builds fetch compatible
headers.

### Threading and framing rules

- The frame-length prefix is big-endian (`struct.pack(">I", ...)` on the server),
  but the FlatBuffers payload itself is little-endian as always.
- Emitter builders finish frames with the core's `FinishTransactionFrame`, which
  prepends the big-endian length, and copy each once into the vector
  `ProducerEndpoint::Append` takes. The outbox shares that allocation through
  reconnect and acknowledgement.
- The frame size limit is 16 MiB (`OUC::kMaxFrameSize`) and must match the
  server.
- Emitter and receiver each open their own TCP socket. `client_id` is the stable
  authentication and producer identity. `origin` is diagnostic metadata, while
  the emitter's `producer_session_id` provides exactly-once transaction identity.
  The plugin reuses one endpoint-scoped producer session across ordinary
  reconnects and creates a new one after changing endpoint or department.

## Editor and PIE behavior

Two engine constraints shape startup:

1. **`UTickableWorldSubsystem::IsTickableInEditor()` defaults to `false`.** Without
   overriding it, the subsystem only ticks during PIE. We override it to `true` so
   sync works in edit mode.
2. **`AUsdStageActor` only generates Unreal scene components when its `StageState` is
   `OpenedAndLoaded`.** With `Opened`, the prims exist in the pxr stage but there are
   no `USceneComponent`s to receive transform updates. The early-return at
   `USDStageActor.cpp:1320–1340` is the authoritative source for this behaviour.

`Initialize()` only generates IDs and sets `bPendingAutoConnect`. Connection
starts on the first tick that observes
`World->bIsWorldInitialized && !World->bIsTearingDown`. Spawning `FRunnableThread`s
from within `Initialize()` was previously found to race with editor startup and
deadlock loading at ~90 %.

## Echo / feedback-loop guards

`UUSDConnectSubsystem::bSuppressEmit` (a `std::atomic<bool>`) is set while
`DrainAndApply()` and plugin-owned USD authoring are running. The attached
`FUsdListener::OnObjectsChanged` callback ignores notices during that window,
preventing received changes and local MaterialX support opinions from being
emitted back to the server.

The listener reports exact Sdf paths. They are coalesced in `PendingEmitPaths`
and drained once per tick, avoiding the ancestor roll-up behavior of
`AUsdStageActor::OnPrimChanged`.

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
The Unreal packaging harness stages its `include/`, `src/frame_codec.cpp`, and
`src/engine/` into `Source/OpenUSDConnectClientCore` before `BuildPlugin`; the
staged copy is an artifact and is never maintained as a second source. That
module builds the endpoints into their own DLL and exports them through
`OPENUSDCONNECT_CLIENT_API`. It links Core so the containers that cross into the
other modules share Unreal's allocator. Repository CMake compiles the canonical
files directly into the nanobind extension.

The client core module's `PublicSystemIncludePaths` exposes the pinned FlatBuffers
headers that `setup_flatbuffers.py` installs under `OpenUSDConnectPXR/ThirdParty`
to every module that depends on it.

## Known gaps

- **Emitter coverage is narrower than receiver coverage.** Unreal currently
  emits TRS, visibility, and edited connectable input values. Geometry,
  connection topology, prim lifecycle, composition arcs, and variants flow
  server to Unreal but are not authored back from Unreal yet.
- **Single stage actor.** `TActorIterator<AUsdStageActor>` picks the first one in
  the world. If multiple stage actors are present (e.g. one per layer file), only
  the first gets live sync. A multi-stage implementation should match
  `RootLayer` against the connected server's base file.

## Diagnostics

Default-on log categories carry only state-change events (connect, handshake,
disconnect, stage-actor attach). Per-event chatter is at `Verbose`. To see traffic in
the Output Log:

```
Log LogUSDConnect            Verbose
Log LogUSDConnectSubsystem   Verbose
Log LogUSDEventApplier       Verbose
```

Use the server dashboard (`--dashboard-port 8080`) to distinguish a silent
client from a transaction that never reached the server.
