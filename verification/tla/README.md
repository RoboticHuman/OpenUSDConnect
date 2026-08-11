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
client selects that authoritative state.

The model checks that the abandoned suffix never commits, the old session
cannot advance after abandonment, the recovery artifact stays complete, and
new-session transactions commit exactly once in order. Weak fairness also
checks that recovery reaches the ready state and the new session completes.

### `ReceiverSynchronization.tla`

Replay and live frames flowing through a bounded receiver queue into the
stage-owning apply thread. It includes disconnect, detected gaps, overflow,
ReplayComplete ordering, and a forced application failure followed by replay.

The primary and tight-queue configurations verify that synchronization is
published only after the advertised replay head has applied successfully.

### `TransactionCoordinator.tla`

Two producer sessions in one managed transaction group. It explores direct
group commit, infrastructure-triggered rollback/fallback, duplicate requests,
and an invalid middle transaction whose valid neighbors must still commit.

### `SharedLayerGraphRace.tla`

A shared-stage topology edit races a revision or generation change between
preparation and commit. It checks atomic graph/stage/log updates, mutation-free
stale rejection, repair against the current graph identity, and session
abandonment only after no unreconciled local opinion intersects the live graph.

### `TwoClientConvergence.tla`

Two clients concurrently author the same logical field while the server picks
the durable order. The model includes disconnect/replay, makes remote USD
notice suppression explicit, and separates freezing a local delta from its
later publication. Under the production contract, it checks that flat and
layered clients retain prepared edits across authoritative application, receive
a contiguous complete stream, converge to the server value, and never turn
remote application into a producer submission.

Run with the official `tla2tools.jar`:

```powershell
java -XX:+UseParallelGC -cp C:\path\to\tla2tools.jar tlc2.TLC `
  -deadlock -workers auto `
  -config verification\tla\TransactionRecovery.cfg `
  verification\tla\TransactionRecovery.tla
```

`-deadlock` disables TLC's deadlock report because the completed state is
intentionally quiescent and the temporal specification permits stuttering.

TLC 2.19 results from 2026-08-11:

| Model and scenario | Generated | Distinct | Depth | Result |
|---|---:|---:|---:|---|
| Transaction recovery: reject transaction 1 | 1,669 | 634 | 25 | No error |
| Transaction recovery: reject transaction 3 | 929 | 372 | 25 | No error |
| Recovery session rollover: reject transaction 2 | 13 | 12 | 11 | No error |
| Receiver: three-frame queue, live apply failure | 1,771 | 516 | 22 | No error |
| Receiver: one-frame queue, replay apply failure | 1,168 | 352 | 22 | No error |
| Coordinator: valid group or infrastructure fallback | 235 | 152 | 11 | No error |
| Coordinator: invalid middle transaction fallback | 106 | 64 | 11 | No error |
| Shared-layer revision/generation race and recovery | 201 | 142 | 9 | No error |
| Two-client convergence with complete commit stream | 4,421 | 1,492 | 28 | No error |

These checks are exhaustive for their configured finite models, not unbounded
proofs. They assume SQLite atomically persists each supplied event/progress
batch and that TCP preserves frame order within one live socket. They do not
model USD composition semantics, database corruption, process-memory
corruption, performance, or administrative replacement of durable producer
progress.
