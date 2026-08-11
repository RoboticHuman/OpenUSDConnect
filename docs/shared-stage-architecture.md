# Shared stage mode architecture

Shared stage mode synchronizes the authored root-layer graph between processes
that each open an equivalent file-backed USD stage. Where managed mode applies
semantic event kinds over server-owned collaboration layers, shared stage mode
treats each client's own file layers, root layer and sublayers alike, as the
synchronized data. `SharedStageClient` is the entry point, with the same
`start()` / `connect()` / `update()` / `close()` lifecycle as
`ManagedClient`.

## What shared stage does

Every process opens its own equivalent stage and resolver context from the same
assets on a shared filesystem or VFS. The server runs an authoritative mirror of
the same graph. Edits made on any client's layers are detected, diffed against
per-layer snapshots, sent as exact Sdf-spec deltas, canonicalized and sequenced
by the server, broadcast, and applied to the matching local layers on every
other client.

Both kinds of state replicate:

- Topology: the ordered sublayer list of every layer, including sublayer paths,
  offsets, and scales. Composition arc edits (add, remove, reorder, retarget a
  sublayer) are first-class events.
- Authored SDF opinions: every authored field on every spec in the graph, from
  prim specifiers and attributes to relationships, variant sets, variants, and
  layer metadata.

Local identifiers and resolved filesystem paths never cross the wire. Opaque
`layer:{uuid}` keys route each event to the correct local `Sdf.Layer`, so each
process may resolve the same authored path to a different concrete file.

Change detection uses `Sdf.Notice.LayersDidChange` plus `Usd.Notice.ObjectsChanged`
to seed candidate deltas, which are accepted only when replaying them onto a
snapshot exactly reproduces the authored layer (byte-equal export). When notice
data is incomplete, for example inactive variants or muted layers, the tracker
falls back to a full snapshot diff. The native Sdf delegate bridge
(`NativeSdfLayerChangeTracker`) is the preferred production tracker; the Python
`SdfLayerChangeTracker` is the fallback.

## How it differs from managed mode

Managed mode (`ManagedClient`, `UsdPublisher`, `UsdReceiver`) has the server own
the collaboration data model. Client edits land in server-owned collaboration
layers stacked above the app's base stage, and flow over semantic event kinds
(`ensure_prim`, `set_xform_trs`, `set_material_binding`, ...) that DCC adapters,
the dashboard, the MCP server, departments, and proposals consume. The emitter
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

Choose shared stage mode when all processes open the same assets from a shared
filesystem or VFS and exact SDF fidelity matters: the layers on disk are the
source of truth, and edits must replicate field-for-field, including sublayer
topology, per-layer variant opinions, and layer metadata. It is the right fit
for editors working directly on production layers where the file content itself
is the deliverable.

Choose managed mode when the server owns the collaboration data model: semantic
event kinds must reach non-USD consumers (DCC adapters, the dashboard, the MCP
server), collaboration content should live above the base stage rather than in
it, or features like departments, proposals, and playback leadership are
required. Managed mode trades exact SDF fidelity for semantic, consumer-friendly
events.

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
assigns authoritative child layer keys, bumps the graph revision, and discovers
routing state for newly reachable descendant layers. Every canonical event is
then seq-assigned, persisted, and broadcast to all clients in order.

## Protocol design

Layer keys. The server's authoritative graph assigns each layer an opaque key
of the form `layer:{uuid4.hex}`. Sublayer entries on the wire carry
`authored_path`, `offset`, `scale`, and once mapped, the child `layer_key`.
Keys are the only layer identity on the wire; `Sdf.Layer.identifier` stays
local.

Generation and revision. The graph is versioned by a `generation` /
`revision` pair. `generation` is a fresh random identity per server start;
`revision` is a positive integer bumped by one for every canonical topology
event. The pair scopes topology state so a restarted server can renumber
revisions without a receiver misreading an old event as current.

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
  to the authoritative `revision + 1` with child keys assigned, and re-broadcasts
  the canonical event. At most one per transaction.

Baseline. The server appends one `layer_graph_state` message to the log at
startup: a sequenced snapshot of the whole reachable graph with `generation`,
`revision`, `root_layer_key`, and each layer's sublayer entries. Every replay
therefore begins with a fresh baseline. Clients bind `root_layer_key` to their
local root layer, materialize sublayer entries without resolving assets, and
only then can route and apply records.
