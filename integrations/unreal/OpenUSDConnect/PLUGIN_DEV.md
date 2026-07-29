# OpenUSD Connect — Plugin Developer Notes

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
│            OnPrimChanged                                                     │
│         ─► drains EventQueue with bSuppressEmit=true while applying          │
│                                                                              │
│  OnPrimChanged() ─► (game thread, via UE delegate)                           │
│                  ─► skipped if bSuppressEmit is true                         │
│                  ─► reads TRS / visibility from pxr stage                    │
│                  ─► encodes Txn frame and pushes to FEmitClient              │
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

Both threads share the same `ClientId` and `SessionOrigin` so the server can correlate
the two sockets as one logical Unreal client. `SessionOrigin` is regenerated per
subsystem instance — i.e. per world — so the editor world and a PIE world are distinct
clients to the server.

---

## Module map

| File | Class / Symbol | Role |
|------|----------------|------|
| `Public/USDConnectSettings.h` | `UUSDConnectSettings` | UDeveloperSettings exposed at *Edit → Project Settings → Plugins → OpenUSD Connect*. |
| `Public/USDConnectSubsystem.h` | `UUSDConnectSubsystem` | UTickableWorldSubsystem that owns both clients, the event queue, and the stage-actor attachment. |
| `OpenUSDConnectPXR/Public/USDConnectProtocol.h` | `namespace OUC` | Wraps the generated FlatBuffers bindings with framing limits and small Unreal helpers. |
| `Private/SyncClient.h/.cpp` | `FSyncClient` | Receiver TCP thread. Handles HELLO, framed reads, echo suppression, ping/rate-limit/resync. |
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

### `Envelope { payload_type: uint8; payload: Payload union; schema_version: uint16 = 1 }`

| `payload_type` | Direction | Meaning |
|----------------|-----------|---------|
| `1 Hello`           | C→S | Handshake (role = `"receiver"` or `"emitter"`) |
| `2 HelloOk`         | S→C | Auth accepted |
| `3 AuthRejected`    | S→C | Token failed TOFU check |
| `4 Txn`             | C→S | Batch of events (used by emitter) |
| `5 BroadcastEvent`  | S→C | Single event with monotonic seq (used by receiver) |
| `6 Resync`          | S→C | Reset client seq → server replays from 0 |
| `8 Ping`            | S→C | Idle heartbeat |
| `12 RateLimited`    | S→C | `retry_after: float` seconds |

### `EventWrapper { event_type: uint8; event: EventPayload union }`

`event_type` values are listed at the top of `USDConnectProtocol.h`. The handful currently implemented in
either direction is summarised in [`README.md`](README.md#supported-events).

### FlatBuffers without generated FlatBuffers code

The plugin does **not** invoke `flatc --cpp` and ship its full generated readers/writers
(allocator override + Unity-build + `dynamic_cast` macro conflicts make that painful in UE).
Instead, reading is done in `OUC::FB::*` via raw vtable arithmetic, and writing uses the
`FlatBufferBuilder` directly with fields placed at hard-coded VT offsets defined in
`OUC::VT::*`.

Vtable offset rule: the *N*th declared field of a table has VT offset `4 + 2*N`. Union
fields take two slots — the type-discriminant byte (lower VT) and the offset to the
referenced table (next VT). All offsets and discriminant constants in `USDConnectProtocol.h`
are **auto-generated** from `openusdconnect/schema/{messages,events}.fbs` by
`scripts/generate_unreal_protocol.py` (also invoked from `scripts/generate_flatbuffers.sh`).
Regenerate and re-commit the header after any schema change — the C++ call sites use
`TableName_FieldName` constants and will fail to compile if a field is renamed/removed,
which is the point.

### Threading & framing rules learned the hard way

- The frame-length prefix is **big-endian** (`struct.pack(">I", ...)` on the server),
  but the FlatBuffers payload itself is little-endian as always.
- The frame size limit is **16 MiB** (`OUC::kMaxFrameSize`) — match the server's value.
- Emitter and receiver each open their own TCP socket. The server keys clients by
  `(client_id, origin)`; with the same `ClientId` + `SessionOrigin` we appear as one
  logical client (one emitter + one receiver). Different worlds → different
  `SessionOrigin` → distinct clients.

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

The server-side `--dashboard 8080` web UI is invaluable for confirming events are
flowing through the server when debugging client-side silence.
