# Shared stage mode architecture

Shared stage mode synchronizes the authored root-layer graph between processes
that each open an equivalent portable, non-anonymous USD stage. Where managed mode applies
semantic event kinds over server-owned collaboration layers, shared stage mode
treats each client's application-owned layers, root layer and sublayers alike, as the
synchronized data. `SharedStageClient` is the entry point, with the same
`start()` / `connect()` / `update()` / `close()` lifecycle as
`ManagedClient`.

## What shared stage does

Every process opens its own equivalent stage and resolver context. Layers may
come from a filesystem or custom `ArResolver`; the authored root and recursive
sublayer contents must be equivalent and editable by participating authors.
The server runs an authoritative mirror of the same graph. Edits made on any client's
layers are detected, sent as exact Sdf-spec deltas, canonicalized and sequenced
by the server, broadcast, and applied to the matching local layers on every
participant, including the authoring participant's authoritative echo.

Shared-stage mode does not publish or compare a complete initial baseline.
Opaque keys prove layer routing, not content identity. If two resolver contexts
return different untouched values, edited fields can synchronize while the
remaining stage still differs. Production deployments must establish baseline
identity through immutable versions, resolver policy, or an external content
check before enabling edits.

Both kinds of state replicate:

- Topology: the ordered sublayer list of every layer, including sublayer paths,
  offsets, and scales. Composition arc edits (add, remove, reorder, retarget a
  sublayer) are first-class events.
- Authored SDF opinions on the supported layer, prim, attribute, relationship,
  variant-set, and variant spec types, including their metadata.

Local identifiers and resolved filesystem paths never cross the wire. Opaque
`layer:{uuid}` keys route each event to the correct local `Sdf.Layer`, so each
process may resolve the same authored path to a different concrete file.

The portable tracker uses `Sdf.Notice.LayersDidChange` plus
`Usd.Notice.ObjectsChanged` to seed candidate deltas, which are accepted only
when replaying them onto its previous snapshot reproduces the current authored
layer. When notice data is incomplete, for example inactive variants or muted
layers, it falls back to a full snapshot diff. This tracker keeps complete
in-memory layer snapshots.

The optional native `SdfLayerStateDelegate` bridge captures old field values at
the mutation boundary and avoids those baseline snapshots during ordinary
tracking. It must be built against the exact OpenUSD ABI loaded by the host;
the portable Python tracker remains the default fallback.

## How it differs from managed mode

Managed mode (`ManagedClient`, `UsdPublisher`, `UsdReceiver`) has the server own
the collaboration data model. Client edits land in server-owned collaboration
layers stacked above the app's base stage, and flow over semantic event kinds
(`ensure_prim`, `set_xform_trs`, `set_material_binding`, ...) that DCC adapters,
the dashboard, the MCP server, and department policies consume. The emitter
diffs composed stage state, and the dispatcher pipelines receive-side
application.

Shared stage mode skips that machinery. There is no emitter, no dispatcher, no
collaboration layer stack. Edits author directly into the app's existing file
layers, and the protocol carries Sdf-spec deltas: which spec changed, which
fields, and the new field values as a USDA fragment. The server validates,
canonicalizes topology, assigns sequence numbers, and broadcasts, but it does
not reinterpret content. `set_sdf_spec_fields` is shared with managed mode as
the fallback channel for authored fields with no high-level event kind; the
other two shared-stage kinds (`replace_sdf_layer_content`, `set_sublayers`) are
exclusive to this mode, and shared-stage clients must not request layered replay
or departments.

## When to use each

Choose shared stage mode when all processes open equivalent versioned assets
through a filesystem, VFS, or resolver and field-level Sdf fidelity matters.
Edits replicate across the application-owned layers, including sublayer
topology, per-layer variant opinions, and layer metadata. Saving those layers
to their backing stores remains an application decision.

Choose managed mode when the server owns the collaboration data model: semantic
event kinds must reach non-USD consumers (DCC adapters, the dashboard, the MCP
server), collaboration content should live above the base stage rather than in
it, or features like departments and playback leadership are
required. Managed mode favors semantic, consumer-friendly events and a
server-owned layer topology over reproducing each application's original layer
graph. Its generic Sdf event still preserves supported authored fields that do
not have a specialized semantic event.

## Data flow

Each client `update()` runs the same shape:

1. Prepare freeze. `prepare_local_changes()` snapshots the current local state
   of every dirty layer into prepared batches: validated candidate deltas where
   possible, full snapshot diffs after a resync.
2. Apply authoritative replay. Incoming records are decoded and applied to
   their routed target layers under an `EditContext` targeting that layer and an
   `atomic_apply` rollback. Layer-graph baselines are applied first, then
   records; records for layers not yet mapped by the baseline are stashed as
   pending. Applying is done with the tracker suppressed so the server's echo
   cannot re-emit. Each accepted record advances the tracker's comparison
   baseline via `accept_authoritative_event`, so the next diff skips changes the
   server already knows.
3. Restore prepared. The frozen local batches are re-applied on top of the
   authoritative replay, because older authoritative records may have
   overwritten the local edits while the batch was frozen. This
   prepare-freeze, replay-authoritative, restore-prepared cycle is what makes
   concurrent local edits and inbound replay safe in one tick.
4. Publish. Prepared batches whose layer now has an authoritative key are sent
   with that `layer_key`; a successful send advances the baseline
   (`mark_prepared_sent`) so the batch is not re-emitted.

Server side, one shared transaction is serialized by a commit lock, validated
(`validate_spec_delta`, `validate_layer_content_replacement`), and applied to
the mirror stage under `atomic_apply` plus a graph transaction that rolls back
routing state on failure. Topology events are canonicalized first: the server
assigns authoritative child layer keys, advances the edited parent layer's
topology revision, and discovers routing state for newly reachable descendant
layers. Every canonical event is then seq-assigned, persisted, and broadcast to
all clients in order.

## Protocol design

Layer keys. The server's authoritative graph assigns each layer an opaque key
of the form `layer:{uuid4.hex}`. Sublayer entries on the wire carry
`authored_path`, `offset`, `scale`, and once mapped, the child `layer_key`.
Keys are the only layer identity on the wire; `Sdf.Layer.identifier` stays
local. The authoritative server persists its identifier-to-key assignments in
a normalized SQLite table. This registry retains detached layers, so the same
logical layer receives the same key if it is later reattached or the server
restarts. Identifier strings remain server-local and are never sent to clients.

Generation and revision. The graph is versioned by a `generation` /
`revision` pair for baseline ordering, while every mapped parent layer also has
its own positive topology revision. A normal server restart restores the
generation, revisions, and stable key registry from the database. Log
compaction creates a new generation and resets the reachable per-parent
revisions because it replaces the old history with a new topology baseline and
compacted authored-content events; the durable layer keys do not change.
Per-parent revisions let edits to
unrelated layer stacks commit independently without weakening conflict
detection for two concurrent edits to the same parent.

Event kinds. Three kinds carry shared-stage content:

- `set_sdf_spec_fields`: an exact field delta for one Sdf spec (prim,
  attribute, relationship, variant set, variant, property, or the layer
  pseudo-root). Carries `spec_path`, `spec_kind`, the changed `fields` list, a
  USDA fragment with the new values, and a `removed` flag for spec deletion.
  Removals sort before creates, and creates before their children, so one
  transaction replays deterministically.
- `replace_sdf_layer_content`: the complete authored content of one layer,
  excluding sublayer topology. Used when a clean full replacement beats a
  field-by-field diff, such as after resync or complex re-organization.
- `set_sublayers`: replaces one parent layer's authored sublayer list. Carries
  `generation` and a client-side `revision` of 0; the server canonicalizes it
  to the targeted parent's authoritative `revision + 1` with child keys
  assigned, and re-broadcasts the canonical event. At most one per transaction.

Baseline. A new shared-stage database begins with one `layer_graph_state`
message: a sequenced snapshot of the reachable graph's topology and routing,
including `generation`, the baseline `revision`, `root_layer_key`, and each
layer's sublayer entries and parent revision. Compaction atomically replaces
the log with a new-generation topology baseline followed by compacted events
that reconstruct the server's current authored layer contents. This preserves
unsaved synchronized edits across replay, but it does not compare or distribute
the participants' untouched initial asset contents. Clients bind
`root_layer_key` to their local root layer, materialize sublayer entries without
resolving assets, and only then can route and apply records.
