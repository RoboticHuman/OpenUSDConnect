# Native client core

Four C++17 targets, each building on the previous one:

- `OpenUSDConnect::ClientCore`: framing, receiver ordering and replay, the producer outbox and
  its rejection policy, and the client phase. No FlatBuffers or OpenUSD dependency.
- `OpenUSDConnect::ClientProtocol`: the generated FlatBuffers schema plus transport-neutral
  handshake, control-message, and transaction construction helpers (`protocol_codec.h`).
- `OpenUSDConnect::ClientEngine`: sans-IO endpoints (`engine/receiver_endpoint.h`,
  `engine/producer_endpoint.h`) that own the connection protocol from handshake through
  reconnect backoff. They start no threads and open no sockets.
- `OpenUSDConnect::ClientDriver`: an optional reference host loop (`driver/`) that drives one
  endpoint on one thread with blocking Winsock or BSD sockets. The Python module links it; a
  host with its own I/O loop and scheduler drives the endpoints directly instead.

Either way the host builds transactions, decodes and applies drained frames on its
stage-owning thread, stores tokens, and handles notifications.

Add the directory with `add_subdirectory` and link the highest target you use. If a
`flatbuffers::flatbuffers` target exists, the protocol target links it; otherwise CMake fetches
the pinned FlatBuffers headers with `FetchContent`, which needs network access on the first
configure unless `FETCHCONTENT_SOURCE_DIR_FLATBUFFERS` names a copy containing
`include/flatbuffers`.

## Protocol layer

The protocol layer owns no transport, threads, queues, event-offset storage, or serialized
buffers. Builders operate on a caller-owned `flatbuffers::FlatBufferBuilder`: build event
offsets with the stateless helpers or the generated schema API, call `FinishTransactionFrame`
with the contiguous offset range, then send from the builder or `Release()` its allocation
without copying. On receive, call `DecodeEnvelope` once at the untrusted-buffer boundary;
`HandshakeResponseView` and `ControlMessageView` then classify the verified envelope without
further validation or copies. Views borrow the receive buffer and are valid only while it is
alive and unchanged.

## Endpoints

An endpoint is thread-safe, never blocks, and never calls into the host. The host feeds it
socket events and time and applies what it returns:

1. Call `ReceiverEndpoint::Start(now)`; a `ProducerEndpoint` connects only when asked
   (`RequestConnect` or `Connect`).
2. Apply `TakeActions()` in order, on the thread that reports socket events: `ConnectAction`
   opens a socket, `SendAction` writes, `CloseAction` closes, `LogAction` logs.
3. Report `OnConnected(token)`, each read with `OnBytes`, the end of a socket or attempt with
   `OnDisconnected`, the time with `OnTick` once `NextWake()` passes, and on the receiver a
   read that waited `SocketTimeout` with `OnReadTimeout`. Apply the actions again after each
   report.
4. Drain the `NotificationQueue` on any thread. The stage-owning thread drains receiver frames
   with `DrainFrames` and reports progress as the header describes; a producer host appends
   complete length-prefixed `Txn` frames with `Append`.

The headers state each method's contract.

## Reference driver

`ThreadedReceiverDriver` and `ThreadedProducerDriver` run that loop for one endpoint on one
thread from `Start()`. The host supplies a `SocketFactory` (`TcpSocketFactory` or its own) and
optional `DriverCallbacks`: the token for each handshake, a hook for an issued token, a sink
that receives notifications after every endpoint call (without one, the host drains the
queue), and a log sink. Callbacks run on the driver thread with no lock held and must not
destroy the driver.

- After an endpoint call from another thread that queues actions (`Append`, `QueueControl`,
  `RequestConnect`, `CancelConnect`, `Disconnect`, `RequestReplayFrom`, `MarkReplayApplied`),
  call `Wake()` so the loop applies them.
- `Stop()` never blocks: it stops the endpoint and interrupts a pending connect or read.
  `Join(timeout)` waits for the thread; destroying the driver stops and joins it.
- The receiver's `WaitConnected` and `WaitSynchronized` and the producer's `Connect` and
  `Flush` block, so they run on a thread other than the loop.
