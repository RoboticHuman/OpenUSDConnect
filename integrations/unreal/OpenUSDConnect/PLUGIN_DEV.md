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
| `Private/USDConnectProtocol.h` | `namespace OUC` | **Single source of truth** for VT offsets, payload-union constants, and FlatBuffers raw-read helpers. `inline constexpr` / inline functions keep it safe under Unreal's Unity Build. |
| `Private/SyncClient.h/.cpp` | `FSyncClient` | Receiver TCP thread. Handles HELLO, framed reads, echo suppression, ping/rate-limit/resync. |
| `Private/EmitClient.h/.cpp` | `FEmitClient` | Emitter TCP thread. Drains a SPSC SendQueue, peeks for inbound corrections via `HasPendingData`. |
| `Private/TxnBuilder.h/.cpp` | `BuildXformTxnFrame`, `BuildVisibilityTxnFrame` | FlatBuffers Txn frame builders for the supported emitter event kinds. |
| `Private/USDEventApplier.h/.cpp` | `FUSDEventApplier::ApplyFrame` | Decodes a BroadcastEvent frame and runs the matching pxr USD operation inside a `pxr::SdfChangeBlock`. |
| `OpenUSDConnect.uplugin`, `Source/OpenUSDConnect/OpenUSDConnect.Build.cs` | – | Plugin descriptor and module rules. Calls `UnrealBuildTool.Rules.UnrealUSDWrapper.CheckAndSetupUsdSdk(Target, this)` — the official engine helper that wires RTTI/exceptions, `USE_USD_SDK`, and pxr linkage. |

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

### FlatBuffers without generated code

The plugin does **not** invoke `flatc` and ship generated headers. Reading is done in
`OUC::FB::*` via raw vtable arithmetic; writing uses the `FlatBufferBuilder` directly,
adding fields at hard-coded VT offsets defined in `OUC::VT::*`.

Vtable offset rule: the *N*th declared field of a table has VT offset `4 + 2*N`. Union
fields take two slots — the type-discriminant byte (lower VT) and the offset to the
referenced table (next VT). All offsets in `USDConnectProtocol.h` are authored to
mirror the `.fbs` schemas in `openusdconnect/schema/`.

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
   `DrainAndApply()` is running, so any `OnPrimChanged` callbacks that fire
   *synchronously* from within `ApplyFrame()` are filtered. This catches the loop
   "apply received event → notice fires → stage actor calls OnPrimChanged → emitter
   would re-send".

> ⚠ Both guards assume `OnPrimChanged` is dispatched synchronously inside
> `ApplyFrame`. If the stage actor in `OpenedAndLoaded` mode defers any callback to a
> later tick, `bSuppressEmit` will already be `false` by then and the echo escapes.
> This is the leading suspect for the current "emitter sends `/World` after every
> received event" symptom. See *Known gaps* below.

---

## Build configuration

`OpenUSDConnect.Build.cs` does the minimum needed and leans on the engine helper:

```csharp
UnrealBuildTool.Rules.UnrealUSDWrapper.CheckAndSetupUsdSdk(Target, this);
```

That call sets `USE_USD_SDK`, RTTI, exception handling, and USD memory-overload
defines correctly. Do **not** set `bUseRTTI` / `bEnableExceptions` manually here —
let the helper own those decisions.

`PublicSystemIncludePaths` adds the FlatBuffers headers shipped with the engine at
`<EngineDirectory>/Source/ThirdParty/flatbuffers/flatbuffers-24.3.25/include/`.

---

## Known gaps / future work

- **Emitter echo from received events.** `OnPrimChanged` may not always fire
  synchronously inside `ApplyFrame`; when it lands on a later tick the
  `bSuppressEmit` guard is already cleared and we re-emit the change we just applied.
  Needs investigation — likely a short-lived per-prim "recently-applied" set with a
  small TTL is the right fix, or stopping `AUsdStageActor::OnPrimChanged` listening
  around the apply window.
- **`OnPrimChanged` prim path granularity.** In current testing it fires with
  `/World` rather than the specific edited prim. Need to confirm whether this is
  the stage actor rolling changes up to the root, or our own attachment logic
  losing the inner prim path.
- **`RenamePrim` is a stub.** Renaming a prim properly requires
  `pxr::SdfCopySpec` to move the prim within its layer. Today the handler logs a
  warning and creates an empty override at the new path.
- **`SetGprimAttrs` not implemented.** Bulk mesh data (points, normals, primvars)
  requires reading the typed numeric arrays from FlatBuffers and calling the
  matching `pxr::UsdGeomMesh` attribute setters.
- **Shader sync not implemented.** `SetConnectableInput` and `SetConnectableConnection`
  map UsdPreviewSurface / MaterialX node graphs. Will need `UsdShadeShader` /
  `UsdShadeConnectableAPI` work.
- **Emitter only sends TRS + visibility.** Other event kinds (delete, deactivate,
  reference, payload, variant) flow only server → Unreal at the moment.
- **Auth token not persisted.** Every connect goes through first-time-trust (empty
  token) so the server re-issues. A returning client should cache and present the
  issued token.
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
