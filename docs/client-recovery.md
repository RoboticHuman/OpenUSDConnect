# Client recovery

Network disconnects normally recover without application involvement. The
sender retains an unacknowledged transaction and retries it with the same
producer session and transaction ID.

A deterministic producer rejection requires an explicit policy. The rejected
transaction and its ordered suffix are quarantined because later IDs cannot
safely pass the gap.
`ManagedClient` and `SharedStageClient` report this through
`client.status.phase == ClientPhase.RECOVERY_REQUIRED`,
`client.status.recovery`, and `client.recovery_artifact`.

Ordinary `update()` calls report the condition without raising. Explicit
recovery commands may raise `RecoveryError`, `TimeoutError`, or
`ConnectionError`.

## Managed client

`ManagedClient` owns one transient `authoring_layer`, so it can either discard
the active layer in favor of server state or replace the rejected transaction.

### Use the server state

`recover_use_server()` preserves the rejected local layer, clears the active
authoring layer, abandons the rejected producer session, and starts a fresh
session:

```python
update = client.update()
if update.recovery is not None:
    result = client.recover_use_server(timeout=10)
    result.preserved_authoring_layer.Export("rejected-work.usda")
```

The preserved layer remains available through `client.last_recovery_result`
until `dismiss_recovery_result()` is called or the client closes. Recovery
attempts to reconnect the producer within the supplied timeout; the normal
`update()` loop continues reconnecting if that attempt does not finish.

### Repair the rejected transaction

If the application can rebuild the rejected operation against current
authoritative state, call:

```python
client.repair_and_resume(replacement_events)
```

The replacement reuses the rejected transaction ID, then releases the retained
ordered suffix. The application is responsible for ensuring the replacement is
semantically valid.

## Shared-stage client

Shared-stage recovery cannot clear application-owned layers automatically.
A detached layer can be reattached later, and exporting rejected work does not
remove the conflicting opinions from the source layer.

Disable authoring when the client enters `RECOVERY_REQUIRED`, then obtain an
assessment on the stage-owning thread:

```python
assessment = client.refresh_recovery_assessment(timeout=5)

for item in assessment.layers:
    print(
        item.rejected_layer_key,
        item.current_layer_key,
        item.reachable,
        item.source_layer,
    )
```

The assessment classifies layers as:

- `unchanged_mapping_layers`: still reachable through the same protocol key
- `remapped_layers`: reachable through a different key
- `detached_layers`: no longer reachable in the current graph
- `source_unavailable_layers`: no local source layer could be associated

Each assessment item retains the application-owned `source_layer` when
available and an anonymous `rejected_snapshot` containing the quarantined local
state. Keep the assessment for as long as those objects are needed.

### Use the server state with a clean stage

This operation requires an equivalent clean stage whose loaded
`Sdf.Layer` identifiers do not overlap the rejected stage:

```python
clean_stage = open_clean_equivalent_stage()
assessment = client.recover_use_server(clean_stage=clean_stage, timeout=5)

for index, snapshot in enumerate(assessment.rejected_snapshots):
    snapshot.Export(f"rejected-work-{index}.usda")

replace_stage_in_host(client.stage)
```

Opening the same asset path again in the same process is usually not enough.
OpenUSD's layer registry may return the same loaded `Sdf.Layer` objects.
Filesystem integrations should open an isolated copy of the clean root and
sublayer tree while preserving relative authored paths. Resolver-backed
integrations should open an immutable/versioned snapshot with distinct resolved
layer identifiers.

The clean stage must represent the collaboration baseline, not a workspace
that already contains the rejected edits. OpenUSDConnect then replays the
authoritative server history over it.

### Application-defined reconciliation

An integration may merge or reset the affected source layers itself:

```python
assessment = client.refresh_recovery_assessment(timeout=5)

for item in assessment.layers:
    if item.source_layer is None:
        handle_unavailable_source(item)
        continue
    reconcile_source_layer(item.source_layer, item.rejected_snapshot)

client.complete_recovery(assessment)
```

Reconcile every listed source layer, including detached layers. A later
topology edit can reattach a detached layer and expose unreconciled opinions.
`complete_recovery()` checks the incident identity and a fresh replay/topology
checkpoint, but it cannot judge the semantics of application-defined USD
merging.

Do not call the normal `update()` loop between the final assessment and
`complete_recovery()`. Doing so can advance the sequence or graph revision and
make the assessment stale. The receive thread may continue queueing newer
records; they are applied after recovery completes.

### Repair in place

If the rejected shared-layer operation can be rebuilt against the current
mapping, use the original ordered ID:

```python
client.repair_and_resume(replacement_events, layer=current_layer)
```

The target layer must still be reachable through the current graph. The client
does not infer a merge policy or redirect the repair to another layer.

## Error handling

Explicit recovery operations use `RecoveryError.code` for expected policy
failures:

```python
from openusdconnect import RecoveryError

try:
    assessment = client.recover_use_server(
        clean_stage=open_clean_equivalent_stage(),
        timeout=5,
    )
except RecoveryError as exc:
    show_recovery_error(exc.code, str(exc))
except (TimeoutError, ConnectionError):
    show_retry_later()
```

Stable codes include `no_incident`, `wrong_recovery_kind`,
`stale_assessment`, `stage_not_synchronized`, `invalid_clean_stage`,
`shared_loaded_layers`, `invalid_repair_target`, `local_changes_pending`,
`transactions_pending`, `stage_unavailable`, and `edit_target_changed`.

## UI guidance

Drive editing state from `client.status.phase`:

- `READY`: enable authoring
- `CONNECTING` or `REPLAYING`: keep calling `update()`, but disable authoring
- `RECOVERY_REQUIRED`: disable authoring and present Use Server, repair, or
  application-specific merge choices
- `REJECTED`: show the authentication or layer-mode reason

Always offer a way to export quarantined work before discarding or replacing
application state.
