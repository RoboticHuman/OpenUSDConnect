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

### `ReceiverSynchronization.tla`

Replay and live frames flowing through a bounded receiver queue into the
stage-owning apply thread. It includes disconnect, detected gaps, overflow,
ReplayComplete ordering, stale connection-generation markers, and a forced
application failure followed by replay.

The primary and tight-queue configurations verify that synchronization is
published only after the advertised replay head has applied successfully.

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

TLC2 2026.08.11.125311 results from 2026-08-16:

| Model and scenario | Generated | Distinct | Depth | Result |
|---|---:|---:|---:|---|
| Transaction recovery: reject transaction 1 | 1,669 | 634 | 25 | No error |
| Transaction recovery: reject transaction 3 | 929 | 372 | 25 | No error |
| Recovery session rollover: reject transaction 2 | 28 | 24 | 14 | No error |
| Receiver: three-frame queue, live apply failure | 15,041 | 3,792 | 27 | No error |
| Receiver: one-frame queue, replay apply failure | 3,723 | 1,024 | 25 | No error |
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
