"""Atomic USD mutation and durable transaction commits.

The caller holds journal.commit_scope() through commit and live publication.
This component owns rollback and durable producer progress; it has no sockets,
worker threads, or reference back to the server facade.
"""

from __future__ import annotations

from dataclasses import dataclass

from pxr import Sdf, Usd

from ..codec import encode_message
from ..event_store import LayerIdentity, ProducerProgress
from ..protocol_constants import (
    K_RENAME_PRIM,
    K_SET_SUBLAYERS,
    MSG_LAYER_GRAPH_STATE,
    NON_COLLABORATION_KINDS,
    LayerMode,
)
from ..shared_layer_graph import PreparedSublayers, StaleLayerGraphError
from .journal import EncodedEvents, EventJournal
from .scene import SceneState, include_removed_spec_fields
from .transactions import TransactionRequest
from .types import TransactionCommit, TransactionOutcome, TransactionRejectedError


@dataclass(slots=True)
class PreparedTransaction:
    events: list[dict]
    target_layer: Sdf.Layer
    collaboration_paths: set[str]
    has_session_events: bool
    encoded: EncodedEvents
    progress: ProducerProgress | None


def _managed_rollback_paths(events: list[dict]) -> set[str]:
    """Return every prim path a managed transaction can structurally change."""
    paths = {
        event["prim"]
        for event in events
        if event.get("k") not in NON_COLLABORATION_KINDS and event.get("prim")
    }
    for event in events:
        if event["k"] != K_RENAME_PRIM:
            continue
        source = Sdf.Path(event["prim"])
        paths.add(str(source.GetParentPath().AppendChild(event["new_name"])))
    return paths


class TransactionCommitter:
    def __init__(self, scene: SceneState, journal: EventJournal):
        self.scene = scene
        self.journal = journal

    def commit(self, request: TransactionRequest) -> TransactionCommit:
        """Commit one admitted request inside the caller's journal commit scope."""

        committed_through = self.journal.committed_through(
            request.client_id,
            request.session_id,
        )
        if request.txn_id <= committed_through:
            return TransactionCommit("duplicate", committed_through)

        expected = committed_through + 1
        if request.txn_id != expected:
            raise TransactionRejectedError(
                "unexpected_id",
                f"expected transaction {expected}, received {request.txn_id}",
                expected_txn_id=expected,
            )

        records = self.commit_events(
            request.events,
            client_id=request.client_id,
            origin=request.origin,
            client_addr=request.client_addr,
            layer=request.layer,
            layer_key=request.layer_key,
            transaction_identity=(request.session_id, request.txn_id),
        )
        self.journal.remember_progress(
            {
                (request.client_id, request.session_id): request.txn_id,
            }
        )
        commit = TransactionCommit(
            "committed",
            request.txn_id,
            tuple(records),
        )
        return commit

    def commit_group(
        self,
        requests: list[TransactionRequest],
    ) -> list[TransactionOutcome]:
        """Return one outcome per request, in input order, without completing them.

        The caller holds the journal commit scope through subsequent publication.
        Accepted managed transactions share one persistence and rollback boundary.
        """
        accepted: list[tuple[int, TransactionRequest]] = []
        outcomes: dict[int, TransactionOutcome] = {}
        next_by_session: dict[tuple[str, str], int] = {}

        for index, request in enumerate(requests):
            producer = (request.client_id, request.session_id)
            committed_through = next_by_session.get(producer)
            if committed_through is None:
                committed_through = self.journal.committed_through(*producer)
            if request.txn_id <= committed_through:
                outcomes[index] = TransactionCommit("duplicate", committed_through)
                continue

            expected = committed_through + 1
            if request.txn_id != expected:
                outcomes[index] = TransactionRejectedError(
                    "unexpected_id",
                    f"expected transaction {expected}, received {request.txn_id}",
                    expected_txn_id=expected,
                )
                continue
            next_by_session[producer] = request.txn_id
            accepted.append((index, request))

        if not accepted:
            return [outcomes[index] for index in range(len(requests))]

        with self.journal.reserve_sequences():
            prepared = [
                self._prepare_managed_transaction(
                    request.events,
                    client_id=request.client_id,
                    origin=request.origin,
                    client_addr=request.client_addr,
                    layer=request.layer,
                    layer_key=request.layer_key,
                    transaction_identity=(request.session_id, request.txn_id),
                )
                for _index, request in accepted
            ]
            self._persist_managed_transactions(prepared)

        self.journal.remember_progress(next_by_session)
        for (index, request), transaction in zip(accepted, prepared, strict=True):
            records = tuple(transaction.encoded.records)
            commit = TransactionCommit(
                "committed",
                request.txn_id,
                records,
            )
            outcomes[index] = commit
        return [outcomes[index] for index in range(len(requests))]

    def _prepare_managed_transaction(
        self,
        events: list[dict],
        *,
        client_id: str | None,
        origin: str | None,
        client_addr: str | None,
        layer: Sdf.Layer | None,
        layer_key: str,
        transaction_identity: tuple[str, int] | None,
    ) -> PreparedTransaction:
        """Check routing and encode previously validated managed events.

        Requires the journal commit scope and a sequence reservation so failures
        in this method or subsequent persistence release the reserved sequences.
        """
        if layer_key:
            raise ValueError("managed transactions cannot select an arbitrary layer key")
        target_layer = layer or self.scene.edit_layer
        layer_key = self.scene.layer_stack.key_for_layer(target_layer)
        if layer_key is None and any(event["k"] not in NON_COLLABORATION_KINDS for event in events):
            raise ValueError("transaction target is not a managed collaboration layer")

        collaboration_paths = _managed_rollback_paths(events)
        has_session_events = any(event["k"] in NON_COLLABORATION_KINDS for event in events)
        encoded = self.journal.encode_events(
            (
                (layer_key or "", event)
                if event["k"] not in NON_COLLABORATION_KINDS
                else ("", event)
                for event in events
            ),
            client_id=client_id,
            origin=origin,
            client_addr=client_addr,
        )
        progress = None
        if transaction_identity is not None:
            if not client_id:
                raise ValueError("idempotent transaction persistence requires client_id")
            session_id, txn_id = transaction_identity
            progress = ProducerProgress(client_id, session_id, txn_id)
        return PreparedTransaction(
            events,
            target_layer,
            collaboration_paths,
            has_session_events,
            encoded,
            progress,
        )

    def _persist_managed_transactions(self, prepared: list[PreparedTransaction]) -> None:
        """Apply and persist a group atomically within the journal commit scope.

        Snapshot only touched collaboration prims, plus session state when
        needed. Internal commits also persist synchronously: rollback must
        cover a storage failure even without producer progress.
        """
        if len(prepared) == 1:
            transaction = prepared[0]
            layer_paths = (
                ((transaction.target_layer, transaction.collaboration_paths),)
                if transaction.collaboration_paths
                else ()
            )
            snapshot_session = transaction.has_session_events
            records = transaction.encoded.store_rows
            producer_progress = (transaction.progress,) if transaction.progress is not None else ()
        else:
            paths_by_layer: dict[str, tuple[Sdf.Layer, set[str]]] = {}
            snapshot_session = False
            records = []
            progress_by_producer = {}
            for transaction in prepared:
                if transaction.collaboration_paths:
                    layer_id = transaction.target_layer.identifier
                    if layer_id not in paths_by_layer:
                        paths_by_layer[layer_id] = (
                            transaction.target_layer,
                            set(transaction.collaboration_paths),
                        )
                    else:
                        _layer, paths = paths_by_layer[layer_id]
                        paths.update(transaction.collaboration_paths)
                snapshot_session |= transaction.has_session_events
                records.extend(transaction.encoded.store_rows)
                if (progress := transaction.progress) is not None:
                    progress_by_producer[(progress.client_id, progress.session_id)] = progress
            layer_paths = paths_by_layer.values()
            producer_progress = tuple(progress_by_producer.values())

        with self.scene.atomic_edit(layer_paths, include_session=snapshot_session):
            for transaction in prepared:
                self.scene.apply_validated(
                    transaction.events,
                    layer=transaction.target_layer,
                    update_tracking=False,
                )
            self.journal.append_batch(
                records,
                producer_progress=producer_progress,
                synchronous=True,
            )
            for transaction in prepared:
                self.scene.update_prim_tracking(transaction.events)

    def commit_events(
        self,
        events: list[dict],
        *,
        client_id: str | None = None,
        origin: str | None = None,
        client_addr: str | None = None,
        layer: Sdf.Layer | None = None,
        layer_key: str = "",
        transaction_identity: tuple[str, int] | None = None,
    ) -> list[tuple[dict, bytes]]:
        """Apply validated events and persist them in the caller's commit scope.

        Each mode owns sequence rollback if preparation or persistence fails.
        Managed writes and writes with producer progress persist synchronously.
        Shared-stage internal writes follow the journal's durability policy.

        Returns encoded broadcast records in input order. Callers that only
        need authoritative state plus a populated log may ignore the result.

        Collaboration records derive their portable layer key from the actual
        edit target. Client policy selects that target before this method is
        called; cached client metadata is not authoritative for persistence.
        """
        if self.scene.layer_mode is LayerMode.SHARED_STAGE:
            if layer is not None:
                raise ValueError("managed layer routing is unavailable in shared-stage mode")
            return self._process_shared_txn(
                events,
                layer_key=layer_key,
                client_id=client_id,
                origin=origin,
                client_addr=client_addr,
                transaction_identity=transaction_identity,
            )
        with self.journal.reserve_sequences():
            transaction = self._prepare_managed_transaction(
                events,
                client_id=client_id,
                origin=origin,
                client_addr=client_addr,
                layer=layer,
                layer_key=layer_key,
                transaction_identity=transaction_identity,
            )
            self._persist_managed_transactions([transaction])
        return transaction.encoded.records

    def _process_shared_txn(
        self,
        events: list[dict],
        *,
        layer_key: str,
        client_id: str | None,
        origin: str | None,
        client_addr: str | None,
        transaction_identity: tuple[str, int] | None = None,
    ) -> list[tuple[dict, bytes]]:
        """Apply one validated authored-layer transaction against the current graph."""
        from ..event_apply import apply_events, atomic_apply

        graph = self.scene.shared_layer_graph
        if graph is None:
            raise RuntimeError("shared-stage transaction requires a layer graph")
        if not layer_key:
            raise ValueError("shared-stage transactions require layer_key")
        target = graph.layer_for(layer_key)
        if target is None or layer_key not in graph.reachable_layer_keys():
            raise TransactionRejectedError(
                "stale_layer_graph",
                f"unknown or unresolved shared layer key {layer_key!r}",
            )

        prepared: PreparedSublayers | None = None
        canonical_events = []
        try:
            for event in events:
                if event["k"] == K_SET_SUBLAYERS:
                    prepared = graph.canonicalize_sublayers(layer_key, event)
                    canonical = prepared.event
                else:
                    canonical = dict(event)
                canonical_events.append(canonical)
        except StaleLayerGraphError as exc:
            raise TransactionRejectedError("stale_layer_graph", str(exc)) from exc

        routed_events = [(layer_key, event) for event in canonical_events]
        try:
            with (
                self.journal.reserve_sequences(),
                self.scene.lock,
                Usd.EditContext(self.scene.stage, Usd.EditTarget(target)),
                graph.transaction(),
            ):
                include_removed_spec_fields(target, canonical_events)
                with atomic_apply(self.scene.stage):
                    apply_events(self.scene.stage, canonical_events, prevalidated=True)
                    if prepared is not None:
                        graph.accept_sublayers(prepared)
                        routed_events.extend(graph.discover_sublayer_states(prepared.mappings))
                    records = self.persist_shared_events(
                        routed_events,
                        client_id=client_id,
                        origin=origin,
                        client_addr=client_addr,
                        transaction_identity=transaction_identity,
                    )
        except StaleLayerGraphError as exc:
            raise TransactionRejectedError("stale_layer_graph", str(exc)) from exc
        self.scene.invalidate_prim_count()
        return records

    def persist_shared_events(
        self,
        routed_events: list[tuple[str, dict]],
        *,
        client_id: str | None,
        origin: str | None,
        client_addr: str | None,
        transaction_identity: tuple[str, int] | None = None,
    ) -> list[tuple[dict, bytes]]:
        """Persist routed records in the caller's commit scope.

        Producer progress and graph identities require synchronous persistence;
        other records follow the configured journal durability policy.
        """
        encoded = self.journal.encode_events(
            routed_events,
            client_id=client_id,
            origin=origin,
            client_addr=client_addr,
        )
        producer_progress: tuple[ProducerProgress, ...] = ()
        if transaction_identity is not None:
            if not client_id:
                raise ValueError("idempotent transaction persistence requires client_id")
            session_id, txn_id = transaction_identity
            producer_progress = (
                ProducerProgress(
                    client_id,
                    session_id,
                    txn_id,
                ),
            )
        topology_keys = set()
        for event_layer_key, event in routed_events:
            if event.get("k") != K_SET_SUBLAYERS:
                continue
            topology_keys.add(event_layer_key)
            topology_keys.update(
                entry["layer_key"] for entry in event.get("sublayers", ()) if entry.get("layer_key")
            )
        layer_identities = (
            self._shared_layer_identity_updates(topology_keys) if topology_keys else ()
        )
        self.journal.append_batch(
            encoded.store_rows,
            producer_progress=producer_progress,
            layer_identities=layer_identities,
        )
        return encoded.records

    def _shared_layer_identity_updates(
        self,
        layer_keys: set[str] | tuple[str, ...],
    ) -> tuple[LayerIdentity, ...]:
        graph = self.scene.shared_layer_graph
        if graph is None:
            raise RuntimeError("shared layer identity requires shared-stage mode")
        identities = []
        for layer_key in sorted(set(layer_keys)):
            identifier = graph.identifier_for_key(layer_key)
            if identifier is not None:
                identities.append(LayerIdentity(identifier=identifier, layer_key=layer_key))
        return tuple(identities)

    def append_graph_baseline(self) -> None:
        """Persist the initial graph baseline and its stable keys atomically."""
        graph = self.scene.shared_layer_graph
        if graph is None:
            raise RuntimeError("shared graph baseline requires shared-stage mode")
        record = graph.state_message(seq=self.journal.assign_seq())
        record_bin = encode_message(record)
        self.journal.append_batch(
            [
                (
                    record["seq"],
                    record_bin,
                    None,
                    MSG_LAYER_GRAPH_STATE,
                    None,
                )
            ],
            layer_identities=tuple(
                LayerIdentity(identifier, layer_key)
                for identifier, layer_key in graph.identity_records()
            ),
        )
