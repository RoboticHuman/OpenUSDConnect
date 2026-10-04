# Native client core

The native core is split into three composable C++17 targets:

- `OpenUSDConnect::ClientCore` provides framing, receiver ordering/replay, producer outbox state,
  and the client phase and rejection policy (`engine/status.h`). It has no FlatBuffers or OpenUSD
  dependency.
- `OpenUSDConnect::ClientProtocol` adds the generated FlatBuffers schema plus transport-neutral
  handshake, control-message, and transaction construction helpers.
- `OpenUSDConnect::ClientEngine` adds sans-IO endpoints (`engine/receiver_endpoint.h`) that own
  the connection protocol: handshake and negotiation, replay identity, control messages, and
  reconnect policy. They start no threads and open no sockets.

The protocol layer deliberately does not own transport, threads, queues, event-offset storage, or
serialized buffers. Decoded views borrow the caller's receive buffer. Builders operate on a
caller-owned `flatbuffers::FlatBufferBuilder`, so an integrator may supply a custom allocator,
construct schema events directly, keep offsets in its native container, and send from the builder
or detach its allocation without copying serialized bytes.

Typical construction is:

1. Create a `flatbuffers::FlatBufferBuilder` with the desired allocator and initial capacity.
2. Build event offsets with the stateless helpers or the generated schema API.
3. Call `FinishTransactionFrame` with the caller-owned contiguous offset range.
4. send `builder.GetBufferPointer()` / `builder.GetSize()`, or call `builder.Release()` to transfer
   the exact allocation.

On receive, call `DecodeEnvelope` once at the untrusted-buffer boundary. `HandshakeResponseView`
and `ControlMessageView` then classify the verified envelope without further validation or copies.
All borrowed pointers remain valid only while the original receive buffer remains alive and
unchanged.

A host drives `ReceiverEndpoint` from its own I/O loop and scheduler:

1. Call `Start(now)`, then apply `TakeActions()` in order: `ConnectAction` opens a socket,
   `SendAction` writes, `CloseAction` closes it, `WakeAction` schedules `OnTick`, and `LogAction`
   goes to the host's log.
2. Report `OnConnected(token)`, every read with `OnBytes`, each read that waited `SocketTimeout`
   with `OnReadTimeout`, and the end of a socket or connect attempt with `OnDisconnected`. Apply
   the actions again after each report.
3. The stage-owning thread drains frames with `DrainFrames` and reports progress as the header
   describes. Notifications arrive in the `NotificationQueue` the host drains.

Every member is thread-safe, never blocks, and never calls into the host.

When included with `add_subdirectory`, link `OpenUSDConnect::ClientProtocol` or
`OpenUSDConnect::ClientEngine`. If `flatbuffers::flatbuffers` already exists, the protocol target
links it. Otherwise CMake fetches the pinned FlatBuffers release headers with `FetchContent`; for
offline builds, set `FETCHCONTENT_SOURCE_DIR_FLATBUFFERS` to an existing copy that contains
`include/flatbuffers`.
