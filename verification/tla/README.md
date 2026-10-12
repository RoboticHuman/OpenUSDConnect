# OpenUSDConnect TLA+ models

These bounded models cover the protocol and ordering boundaries that are not
well represented by example-based tests. They model state-machine behavior,
not OpenUSD payload semantics.

## Models

### `TransactionRecovery.tla`

One ordered producer session, including durable commit, cumulative ACK,
ambiguous disconnect, duplicate replay, recoverable rejection, suffix
quarantine, and same-ID repair.

The model checks:

- durable commits form a contiguous transaction-ID prefix;
- each ID is applied at most once;
- cumulative client acknowledgement never exceeds durable server progress;
- reconnect replay remains ordered;
- deterministic rejection closes the transport and quarantines the suffix;
- the rejected ID is exactly the uncommitted gap;
- same-ID repair restores that gap before later transactions;
- no submitted suffix becomes permanently unfillable;
- all transactions eventually commit and become acknowledged after the
  network stabilizes, repair is supplied, and weakly fair processing resumes.

Two configurations exercise a five-transaction session:

- `TransactionRecoveryFirst.cfg`: transaction 1 is rejected;
- `TransactionRecovery.cfg`: transaction 3 is rejected after earlier commits.

### `RecoverySessionRollover.tla`

Authoritative-state recovery after a deterministic rejection. The rejected
transaction and its queued suffix are preserved as an artifact while their
producer session is permanently abandoned. A new producer session can begin
at transaction 1 only after replay reaches a fresh server checkpoint and the
client selects that authoritative state. The checkpoint can advance while
replay is in progress, forcing another catch-up before activation.

The model checks that the abandoned suffix never commits, the old session
cannot advance after abandonment, the recovery artifact stays complete, and
new-session transactions commit exactly once in order. Weak fairness also
checks that recovery reaches the ready state and the new session completes.

### `ProducerConnection.tla`

The producer endpoint's connection attempts against one server, with the host
loop that applies its actions. The application thread starts, cancels, and
disconnects attempts at any time; the I/O loop takes actions in batches,
blocks while connecting, and can report a socket's end before applying a
close the endpoint queued for it. The Hello carries the server's committed
highwater, which is checked before the outbox replays.

The model checks that the host never opens a second socket or keeps one the
endpoint has forgotten, that a cancelled handshake never publishes or
quarantines the session, that the session is ready exactly while connected,
that replay never skips a transaction, and that acknowledgement never
regresses or covers an unsubmitted transaction. It covers the stale-close
hazard: an attempt the host has not taken is withdrawn instead of closed, and
a reported end voids the frames and close still queued for that socket.

`ProducerConnection.cfg` uses an honest server and also checks that every
transaction is eventually acknowledged; `ProducerConnectionDivergence.cfg`
lets the server's progress for the session regress or run ahead once, and
checks that only that divergence fails the Hello highwater check.

### `ReceiverSynchronization.tla`

Replay and live frames flowing through a bounded receiver queue into the
stage-owning apply thread. It includes disconnect, detected gaps, overflow,
ReplayComplete ordering, stale connection-generation markers, and a forced
application failure followed by replay.

The primary and tight-queue configurations verify that synchronization is
published only after the advertised replay head has applied successfully.

### `ReceiverReplayIdentity.tla`

The receiver's Hello and replay-identity flow across sequence domains:
compaction, purge, or snapshot replacement on a live connection, server
restarts, server-side sequence gaps, queue overflow, a consumer apply failure,
and a reconnect between draining a frame and reporting it applied. The server
resumes a Hello whose prefix claim matches its domain and otherwise resets.

The model checks that the stage only ever holds a contiguous prefix of one
domain, that a replay marked applied is reflected in the stage, that the
receiver queues its own reset only ahead of a replay from one, and that the
receiver converges once the network stabilizes. It covers the live-reset
hazard: the applied cursor may still count the old domain, so a replay it
positions claims the applied identity and the server resets instead of
resuming. Injected gaps are always followed by a frame that reveals them.

A replay request keeps the received identity unless a reset is pending,
queued or drained but not yet reported applied; only then does the claim
fall back to the applied identity. `ResetPendingTracksResets` checks the
inbox's flag against the frames it stands for.

Two invariants tie the identities to the prefix they describe.
`ReceivedNamesHeldPrefix`: the received identity names the domain of the
newest held prefix (the frames after the last pending reset, or the stage
and pending frames), except after a replay request with a reset pending
whose consumer started a newer domain from an empty stage since its last
mark; the claim then names that older applied replay and the server resets.
`AppliedNamesStage`: the applied identity names the stage until the stage is
next empty.

`NoSpuriousResetWhenKnown`: once the network is stable and the server's
domain has not changed since the receiver last connected, a receiver that
knew the domain of everything it kept when its connection ended is resumed.

`NoSpuriousReset` asks the same of any receiver holding a prefix of the
current domain and is not checked. Two causes still reset it: a host-loaded
snapshot prefix is never claimed, and a live reset whose ReplayComplete never
arrived leaves its frames unproven because Resync carries no epoch. The
Python receiver shares both.

`ReceiverReplayIdentity.cfg` starts a fresh receiver and allows two domain
changes; `ReceiverReplayIdentitySnapshot.cfg` starts from a host-loaded
snapshot cursor with a one-frame queue.

### `TransactionCoordinator.tla`

Two producer sessions in one managed transaction group. It explores direct
group commit, infrastructure-triggered rollback/fallback, duplicate requests,
and an invalid middle transaction whose valid neighbors must still commit.
Group application, durable publication, and rollback are separate transitions,
so rollback after a successful USD mutation is checked rather than assumed.

### `SharedLayerGraphRace.tla`

Shared-stage topology edits race same-parent and unrelated-parent commits,
generation-changing compaction, persistence failure, detach, repair,
and abandonment. The model checks that unrelated parents do not create false
conflicts, same-parent edits still conflict, durable logical layer keys survive
detach and generation changes, graph/stage/log/identity updates are atomic, and
rejected work either repairs against the current parent identity or is safely
abandoned.

### `SharedLayerRestartRecovery.tla`

Crash/restart recovery for the shared-layer graph checkpoint. A crash destroys
the volatile generation, revision, topology, key set, sequence, and log; restart
must reconstruct them from one durable checkpoint. Attach, detach, and
compaction can all happen before the crash.

### `TwoClientConvergence.tla`

Two clients concurrently author the same logical field while the server picks
the durable order. The model includes disconnect/replay, makes remote USD
notice suppression explicit, and separates freezing a local delta from its
later publication. Under the modeled synchronization contract, it checks that
flat and layered clients retain prepared edits across authoritative application,
receive a contiguous complete stream, converge to the server value, and never
turn remote application into a producer submission. Echo submission is updated
by the notice-handling transition rather than being a fixed zero counter.

Run every scenario with the repository runner:

```powershell
uv run python scripts\run_tla_models.py --download --workers auto
```

The runner downloads the official TLA+ tools v1.8.0 release when requested,
verifies its pinned SHA-256, runs every configuration with action coverage, and
fails if a required adversarial transition is not exercised. Set
`TLA2TOOLS_JAR` or pass `--jar` to use an existing verified copy.

`-deadlock` is set by the runner because completed states are intentionally
quiescent and the temporal specifications permit stuttering.

## Last verified snapshot

The following results describe the model and configuration files in the commit
that contains this snapshot. Regenerate the table after changing a `.tla` or
`.cfg` file, or when adopting a different TLC version.

TLC2 2026.08.11.125311 results from 2026-10-04:

| Model and scenario | Generated | Distinct | Depth | Result |
|---|---:|---:|---:|---|
| Transaction recovery: reject transaction 1 | 1,669 | 634 | 25 | No error |
| Transaction recovery: reject transaction 3 | 929 | 372 | 25 | No error |
| Recovery session rollover: reject transaction 2 | 28 | 24 | 14 | No error |
| Producer connection: honest server, eventual acknowledgement | 5,341 | 3,387 | 42 | No error |
| Producer connection: server progress diverges once | 10,694 | 6,732 | 47 | No error |
| Receiver: three-frame queue, live apply failure | 15,041 | 3,792 | 27 | No error |
| Receiver: one-frame queue, replay apply failure | 3,723 | 1,024 | 25 | No error |
| Replay identity: fresh receiver, two domain changes | 5,857,627 | 1,170,182 | 53 | No error |
| Replay identity: snapshot cursor, one-frame queue | 282,901 | 79,024 | 47 | No error |
| Coordinator: valid group or infrastructure fallback | 237 | 153 | 11 | No error |
| Coordinator: invalid middle transaction fallback | 106 | 64 | 11 | No error |
| Shared-layer parent revision, stable identity, and recovery | 3,262 | 2,288 | 13 | No error |
| Shared-layer crash/restart checkpoint recovery | 46 | 45 | 6 | No error |
| Two-client convergence with complete commit stream | 4,421 | 1,492 | 28 | No error |

These checks are exhaustive for their configured finite models, not unbounded
proofs. They assume SQLite atomically persists each supplied event/progress
batch and that TCP preserves frame order within one live socket. They do not
model USD composition semantics, database corruption, process-memory
corruption, performance, or administrative replacement of durable producer
progress.

The models intentionally use abstract transaction IDs, sequence numbers,
revisions, and topology sets. Those are the protocol algorithms and failure
boundaries. Python object layout, function names, locks, SQL statements, and
Blender/USD data structures remain in implementation tests rather than TLA+.

External shared-stage recovery also assumes that the integration reconciles
every reported source layer before abandoning the rejected session. The Python
client verifies incident identity and a fresh sequence/topology checkpoint, but
cannot prove that an application-specific USD merge removed every conflicting
opinion. The model treats that semantic reconciliation as an environment action.
