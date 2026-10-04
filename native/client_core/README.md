# Native client core

The native core is split into two composable C++17 targets:

- `OpenUSDConnect::ClientCore` provides framing, receiver ordering/replay, producer outbox state,
  and the client phase and rejection policy (`engine/status.h`). It has no FlatBuffers or OpenUSD
  dependency.
- `OpenUSDConnect::ClientProtocol` adds the generated FlatBuffers schema plus transport-neutral
  handshake, control-message, and transaction construction helpers.

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

When included with `add_subdirectory`, link `OpenUSDConnect::ClientProtocol`. If
`flatbuffers::flatbuffers` already exists, the target links it. Otherwise CMake fetches the pinned
FlatBuffers release headers with `FetchContent`; for offline builds, set
`FETCHCONTENT_SOURCE_DIR_FLATBUFFERS` to an existing copy that contains `include/flatbuffers`.
