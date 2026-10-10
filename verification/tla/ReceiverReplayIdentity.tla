----------------------- MODULE ReceiverReplayIdentity -----------------------
EXTENDS Integers, Sequences, TLC

(***************************************************************************
Bounded model of the receiver's Hello and replay-identity flow.

The server's history lives in one sequence domain (server instance and replay
epoch) until compaction, purge, snapshot replacement, or a restart replaces
it. Every Hello carries the receiver's cursor and, from the second Hello on,
a claim naming the domain of the prefix the receiver holds. The server
resumes a matching claim and otherwise sends Resync and replays from one; it
never resets a replay that starts at one. A live reset sends Resync, the new
domain's history, and ReplayComplete on the open connection.

The receiver keeps its queue across reconnects, so it claims the received
identity: the domain of the newest prefix it holds. A replay request discards
the queue and keeps that claim unless a reset is pending, queued or drained
but not yet reported applied; the claim then falls back to the applied
identity, the domain of the last replay the consumer marked applied. A
request for sequence one makes the receiver queue its own reset. The
consumer drains and applies one frame at a time, may fail once, and a
reconnect may separate draining from reporting the applied cursor. Until
the network stabilizes it may drop connections and frames.

The stage must always hold a contiguous prefix of exactly one domain's
history. In particular, a replay positioned by an applied cursor that still
counts the old domain after a live reset must never resume the new domain.
The invariants also tie both identities to the prefix they describe, and a
receiver that knew the domain of everything it kept is resumed rather than
reset. Without that knowledge a reconnect to an unchanged domain still
resets: the claim carries no proof for a host-loaded snapshot or for a live
reset whose ReplayComplete never arrived.
***************************************************************************)

CONSTANTS MaxSeq, MaxDomain, QueueBound, InitialHead, InitialCursor, MaxFailures

None == -1
NoClaim == -2
Domains == 0..MaxDomain
Identities == Domains \cup {None}

Msg(type, dom, seq) == [type |-> type, dom |-> dom, seq |-> seq]
Frame(kind, dom, seq) == [kind |-> kind, dom |-> dom, seq |-> seq]
NoFrame == Frame("none", None, 0)
NoMarker == [head |-> -1, dom |-> None, backlog |-> 0]
NoReady == [head |-> -1, dom |-> None]

EventsFrom(dom, first, last) ==
    [index \in 1..(last - first + 1) |-> Msg("event", dom, first + index - 1)]

Max(a, b) == IF a > b THEN a ELSE b

VARIABLES
    \* Server.
    dom,
    head,
    \* Connection: frames the server sent on the open socket, in order.
    conn,
    chan,
    connSync,
    \* Receiver inbox.
    queue,
    lastRecv,
    lastApplied,
    requested,
    marker,
    ready,
    resetPending,
    \* Receiver replay identity.
    helloSent,
    claimIncluded,
    claimed,
    handshake,
    received,
    pendingId,
    appliedId,
    proven,
    resetRequired,
    \* Consumer and its stage; stageDom is None while the stage is empty.
    stageDom,
    stageLen,
    stageOk,
    inflight,
    inflightStale,
    failures,
    networkStable,
    \* History only, for stating properties: they never constrain a transition.
    domainChangedSinceConnect,
    stageEmptiedSinceMark,
    keptPrefixKnown

serverVars == <<dom, head>>
connVars == <<conn, chan, connSync>>
inboxVars == <<queue, lastRecv, lastApplied, requested, marker, ready, resetPending>>
identityVars == <<helloSent, claimIncluded, claimed, handshake, received, pendingId,
                  appliedId, proven, resetRequired>>
consumerVars == <<stageDom, stageLen, stageOk, inflight, inflightStale, failures>>
historyVars == <<domainChangedSinceConnect, stageEmptiedSinceMark, keptPrefixKnown>>
vars == <<serverVars, connVars, inboxVars, identityVars, consumerVars, networkStable,
          historyVars>>

Init ==
    /\ InitialHead \in 0..MaxSeq
    /\ InitialCursor \in 1..(InitialHead + 1)
    /\ QueueBound >= 1
    /\ dom = 0
    /\ head = InitialHead
    /\ conn = "down"
    /\ chan = <<>>
    /\ connSync = 0
    /\ queue = <<>>
    /\ lastRecv = InitialCursor - 1
    /\ lastApplied = InitialCursor - 1
    /\ requested = 0
    /\ marker = NoMarker
    /\ ready = NoReady
    /\ resetPending = FALSE
    /\ helloSent = FALSE
    /\ claimIncluded = FALSE
    /\ claimed = None
    /\ handshake = None
    /\ received = None
    /\ pendingId = None
    /\ appliedId = None
    /\ proven = FALSE
    /\ resetRequired = FALSE
    \* A cursor above one means the host loaded this server's snapshot.
    /\ stageDom = IF InitialCursor > 1 THEN 0 ELSE None
    /\ stageLen = InitialCursor - 1
    /\ stageOk = TRUE
    /\ inflight = NoFrame
    /\ inflightStale = FALSE
    /\ failures = 0
    /\ networkStable = FALSE
    /\ domainChangedSinceConnect = FALSE
    /\ stageEmptiedSinceMark = FALSE
    /\ keptPrefixKnown = FALSE

(* Frames the consumer drained or will drain, in order. *)
PendingFrames == (IF inflight = NoFrame THEN <<>> ELSE <<inflight>>) \o queue

IsReset(frames, index) == frames[index].kind = "reset"

QueuedReset == \E index \in 1..Len(queue) : IsReset(queue, index)

ResetAmongPending == \E index \in 1..Len(PendingFrames) : IsReset(PendingFrames, index)

(* The next Hello's cursor and claim. *)
HelloSyncFrom == IF requested > 0 THEN requested ELSE lastRecv + 1
HelloClaim == IF helloSent THEN received ELSE NoClaim

ServerResets(sync, claim) ==
    \/ sync > head + 1
    \/ sync > 1 /\ claim # NoClaim /\ claim # dom

(* The server captures its replay atomically when it accepts the Hello. *)
ServerReplay(sync, claim) ==
    LET reset == ServerResets(sync, claim)
        first == IF reset THEN 1 ELSE sync
    IN  <<Msg("hello", dom, 0)>>
        \o (IF reset THEN <<Msg("resync", None, 0)>> ELSE <<>>)
        \o EventsFrom(dom, first, head)
        \o <<Msg("complete", dom, head)>>

Connect ==
    /\ conn = "down"
    /\ chan' = ServerReplay(HelloSyncFrom, HelloClaim)
    /\ connSync' = HelloSyncFrom
    /\ conn' = "handshake"
    /\ requested' = 0
    /\ marker' = NoMarker
    /\ ready' = NoReady
    /\ helloSent' = TRUE
    /\ claimIncluded' = helloSent
    /\ claimed' = received
    /\ pendingId' = None
    /\ inflightStale' = TRUE
    /\ domainChangedSinceConnect' = FALSE
    /\ keptPrefixKnown' = FALSE
    /\ UNCHANGED <<serverVars, queue, lastRecv, lastApplied, resetPending, handshake, received,
                   appliedId, proven, resetRequired, stageDom, stageLen, stageOk,
                   inflight, failures, networkStable, stageEmptiedSinceMark>>

AcceptReset ==
    /\ queue' = Append(queue, Frame("reset", None, 0))
    /\ lastRecv' = 0
    /\ marker' = NoMarker
    /\ ready' = NoReady
    /\ resetPending' = TRUE
    /\ handshake' = None
    /\ proven' = TRUE
    /\ pendingId' = None
    /\ resetRequired' = FALSE

ReceiveHello ==
    /\ conn = "handshake"
    /\ chan # <<>>
    /\ Head(chan).type = "hello"
    /\ LET d == Head(chan).dom
           isProven == connSync = 1 \/ (claimIncluded /\ claimed = d)
       IN  IF resetRequired
           THEN /\ AcceptReset
                /\ received' = d
           ELSE /\ received' = IF isProven THEN d ELSE received
                /\ handshake' = d
                /\ proven' = isProven
                /\ UNCHANGED <<queue, lastRecv, marker, ready, resetPending, pendingId,
                               resetRequired>>
    /\ conn' = "up"
    /\ chan' = Tail(chan)
    /\ UNCHANGED <<serverVars, connSync, lastApplied, requested, helloSent, claimIncluded,
                   claimed, appliedId, consumerVars, networkStable, historyVars>>

(* Inbox overflow closes the connection; the queue survives the reconnect. *)
Overflow ==
    /\ conn' = "down"
    /\ chan' = <<>>
    /\ marker' = NoMarker
    /\ ready' = NoReady
    /\ keptPrefixKnown' = (received = dom)
    /\ UNCHANGED <<queue, lastRecv, resetPending, handshake, received, proven, pendingId,
                   resetRequired>>

(* A pending reset separates the consumer's prefix from the received frames,
   so only the applied replay can name it. *)
RequestReplay(seq) ==
    /\ requested' = seq
    /\ lastRecv' = seq - 1
    /\ lastApplied' = seq - 1
    /\ queue' = <<>>
    /\ marker' = NoMarker
    /\ ready' = NoReady
    /\ resetPending' = FALSE
    /\ received' = IF resetPending THEN appliedId ELSE received
    /\ handshake' = None
    /\ pendingId' = None
    /\ resetRequired' = (seq = 1)
    /\ inflightStale' = TRUE
    /\ conn' = "down"
    /\ chan' = <<>>
    /\ keptPrefixKnown' = (received = dom /\ ~ResetAmongPending)

ReceiveResync ==
    /\ conn = "up"
    /\ chan # <<>>
    /\ Head(chan).type = "resync"
    /\ IF Len(queue) = QueueBound
       THEN Overflow
       ELSE /\ AcceptReset
            /\ received' = handshake
            /\ chan' = Tail(chan)
            /\ UNCHANGED <<conn, keptPrefixKnown>>
    /\ UNCHANGED <<serverVars, connSync, lastApplied, requested, helloSent, claimIncluded,
                   claimed, appliedId, consumerVars, networkStable, domainChangedSinceConnect,
                   stageEmptiedSinceMark>>

ReceiveEvent ==
    /\ conn = "up"
    /\ chan # <<>>
    /\ Head(chan).type = "event"
    /\ LET m == Head(chan) IN
       \/ /\ m.seq <= lastRecv
          /\ chan' = Tail(chan)
          /\ UNCHANGED <<conn, queue, lastRecv, lastApplied, requested, marker, ready,
                         resetPending, handshake, received, pendingId, resetRequired,
                         inflightStale, keptPrefixKnown>>
       \/ /\ m.seq > lastRecv + 1
          /\ RequestReplay(lastApplied + 1)
       \/ /\ m.seq = lastRecv + 1
          /\ Len(queue) = QueueBound
          /\ Overflow
          /\ UNCHANGED <<lastApplied, requested, inflightStale>>
       \/ /\ m.seq = lastRecv + 1
          /\ Len(queue) < QueueBound
          /\ queue' = Append(queue, Frame("event", m.dom, m.seq))
          /\ lastRecv' = m.seq
          /\ chan' = Tail(chan)
          /\ UNCHANGED <<conn, lastApplied, requested, marker, ready, resetPending, handshake,
                         received, pendingId, resetRequired, inflightStale, keptPrefixKnown>>
    /\ UNCHANGED <<serverVars, connSync, helloSent, claimIncluded, claimed, appliedId, proven,
                   stageDom, stageLen, stageOk, inflight, failures, networkStable,
                   domainChangedSinceConnect, stageEmptiedSinceMark>>

(* A marker beyond the received records reveals a gap like an early frame. *)
ReceiveComplete ==
    /\ conn = "up"
    /\ chan # <<>>
    /\ Head(chan).type = "complete"
    /\ LET m == Head(chan)
           identity == IF proven THEN m.dom ELSE None
       IN  IF m.seq > lastRecv
           THEN RequestReplay(lastApplied + 1)
           ELSE /\ marker' = [head |-> m.seq, dom |-> m.dom, backlog |-> Len(queue)]
                /\ received' = identity
                /\ pendingId' = identity
                /\ handshake' = None
                /\ chan' = Tail(chan)
                /\ UNCHANGED <<conn, queue, lastRecv, lastApplied, requested, ready,
                               resetPending, resetRequired, inflightStale, keptPrefixKnown>>
    /\ UNCHANGED <<serverVars, connSync, helloSent, claimIncluded, claimed, appliedId, proven,
                   stageDom, stageLen, stageOk, inflight, failures, networkStable,
                   domainChangedSinceConnect, stageEmptiedSinceMark>>

Disconnect ==
    /\ ~networkStable
    /\ conn # "down"
    /\ conn' = "down"
    /\ chan' = <<>>
    /\ marker' = NoMarker
    /\ ready' = NoReady
    /\ keptPrefixKnown' = (received = dom)
    /\ UNCHANGED <<serverVars, connSync, queue, lastRecv, lastApplied, requested, resetPending,
                   identityVars, consumerVars, networkStable, domainChangedSinceConnect,
                   stageEmptiedSinceMark>>

(* A server-side sequence gap, which only a later frame can reveal. *)
DropEvent ==
    /\ ~networkStable
    /\ conn = "up"
    /\ Len(chan) > 1
    /\ Head(chan).type = "event"
    /\ chan' = Tail(chan)
    /\ UNCHANGED <<serverVars, conn, connSync, inboxVars, identityVars, consumerVars,
                   networkStable, historyVars>>

AppendLive ==
    /\ head < MaxSeq
    /\ head' = head + 1
    /\ chan' = IF conn = "down" THEN chan ELSE Append(chan, Msg("event", dom, head + 1))
    /\ UNCHANGED <<dom, conn, connSync, inboxVars, identityVars, consumerVars, networkStable,
                   historyVars>>

(* A snapshot cursor is only meaningful against the server it came from. *)
DomainMayChange == dom < MaxDomain /\ (helloSent \/ InitialCursor = 1)

LiveReset ==
    /\ DomainMayChange
    /\ dom' = dom + 1
    /\ \E newHead \in 0..MaxSeq:
           /\ head' = newHead
           /\ chan' = IF conn = "down" THEN chan
                      ELSE chan \o <<Msg("resync", None, 0)>> \o EventsFrom(dom + 1, 1, newHead)
                                \o <<Msg("complete", dom + 1, newHead)>>
    /\ domainChangedSinceConnect' = TRUE
    /\ UNCHANGED <<conn, connSync, inboxVars, identityVars, consumerVars, networkStable,
                   stageEmptiedSinceMark, keptPrefixKnown>>

Restart ==
    /\ DomainMayChange
    /\ dom' = dom + 1
    /\ head' \in 0..MaxSeq
    /\ conn' = "down"
    /\ chan' = <<>>
    /\ marker' = NoMarker
    /\ ready' = NoReady
    /\ domainChangedSinceConnect' = TRUE
    /\ keptPrefixKnown' = FALSE
    /\ UNCHANGED <<connSync, queue, lastRecv, lastApplied, requested, resetPending,
                   identityVars, consumerVars, networkStable, stageEmptiedSinceMark>>

(* Reading the generation and draining are one step; reporting is later. *)
ConsumerDrain ==
    /\ inflight = NoFrame
    /\ queue # <<>>
    /\ inflight' = Head(queue)
    /\ queue' = Tail(queue)
    /\ inflightStale' = FALSE
    /\ marker' = IF marker.backlog > 0 THEN [marker EXCEPT !.backlog = @ - 1] ELSE marker
    /\ UNCHANGED <<serverVars, connVars, lastRecv, lastApplied, requested, ready, resetPending,
                   identityVars, stageDom, stageLen, stageOk, failures, networkStable,
                   historyVars>>

(* Applies the frame, then reports the cursor with the drained generation; an
   applied reset reports every reset drained so far. *)
ConsumerApply ==
    /\ inflight # NoFrame
    /\ LET f == inflight
           reset == f.kind = "reset"
           extends == IF stageDom = None THEN f.seq = 1
                      ELSE f.dom = stageDom /\ f.seq <= stageLen + 1
           newLen == IF reset THEN 0 ELSE Max(stageLen, f.seq)
           cursor == IF reset THEN 0 ELSE lastApplied
           reported == ~inflightStale /\ newLen >= cursor /\ newLen <= lastRecv
       IN  /\ stageDom' = IF reset THEN None ELSE f.dom
           /\ stageLen' = newLen
           /\ stageOk' = (stageOk /\ (reset \/ extends))
           /\ lastApplied' = IF reported THEN newLen ELSE cursor
           /\ ready' = IF reset THEN NoReady ELSE ready
           /\ resetPending' = IF reset THEN QueuedReset ELSE resetPending
           /\ stageEmptiedSinceMark' = (stageEmptiedSinceMark \/ reset)
    /\ inflight' = NoFrame
    /\ UNCHANGED <<serverVars, connVars, queue, lastRecv, requested, marker, identityVars,
                   inflightStale, failures, networkStable, domainChangedSinceConnect,
                   keptPrefixKnown>>

(* The batch failed, so the consumer replays from its own applied cursor. *)
ConsumerFail ==
    /\ inflight # NoFrame
    /\ failures < MaxFailures
    /\ failures' = failures + 1
    /\ inflight' = NoFrame
    /\ RequestReplay(IF inflight.kind = "reset" THEN 1 ELSE stageLen + 1)
    /\ UNCHANGED <<serverVars, connSync, helloSent, claimIncluded, claimed, appliedId, proven,
                   stageDom, stageLen, stageOk, networkStable, domainChangedSinceConnect,
                   stageEmptiedSinceMark>>

(* Every drained frame applied, so the replay head counts as applied. *)
ConsumerMark ==
    /\ inflight = NoFrame
    /\ marker # NoMarker
    /\ marker.backlog = 0
    /\ lastApplied' = Max(lastApplied, marker.head)
    /\ ready' = [head |-> marker.head, dom |-> marker.dom]
    /\ marker' = NoMarker
    /\ appliedId' = pendingId
    /\ pendingId' = None
    /\ stageEmptiedSinceMark' = (stageDom = None)
    /\ UNCHANGED <<serverVars, connVars, queue, lastRecv, requested, resetPending, helloSent,
                   claimIncluded, claimed, handshake, received, proven, resetRequired,
                   consumerVars, networkStable, domainChangedSinceConnect, keptPrefixKnown>>

StabilizeNetwork ==
    /\ ~networkStable
    /\ networkStable' = TRUE
    /\ UNCHANGED <<serverVars, connVars, inboxVars, identityVars, consumerVars, historyVars>>

Next ==
    \/ Connect
    \/ ReceiveHello
    \/ ReceiveResync
    \/ ReceiveEvent
    \/ ReceiveComplete
    \/ Disconnect
    \/ DropEvent
    \/ AppendLive
    \/ LiveReset
    \/ Restart
    \/ ConsumerDrain
    \/ ConsumerApply
    \/ ConsumerFail
    \/ ConsumerMark
    \/ StabilizeNetwork

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(StabilizeNetwork)
    /\ WF_vars(Connect)
    /\ WF_vars(ReceiveHello)
    /\ WF_vars(ReceiveResync)
    /\ WF_vars(ReceiveEvent)
    /\ WF_vars(ReceiveComplete)
    /\ WF_vars(ConsumerDrain)
    /\ WF_vars(ConsumerApply)
    /\ WF_vars(ConsumerMark)

TypeOK ==
    /\ dom \in Domains
    /\ head \in 0..MaxSeq
    /\ conn \in {"down", "handshake", "up"}
    /\ connSync \in 0..(MaxSeq + 1)
    /\ Len(queue) <= QueueBound
    /\ lastRecv \in 0..MaxSeq
    /\ lastApplied \in 0..MaxSeq
    /\ requested \in 0..(MaxSeq + 1)
    /\ {resetPending, helloSent, claimIncluded, proven, resetRequired} \subseteq BOOLEAN
    /\ {claimed, handshake, received, pendingId, appliedId} \subseteq Identities
    /\ stageDom \in Identities
    /\ stageLen \in 0..MaxSeq
    /\ stageOk \in BOOLEAN
    /\ failures \in 0..MaxFailures
    /\ {domainChangedSinceConnect, stageEmptiedSinceMark, keptPrefixKnown} \subseteq BOOLEAN

LastPendingReset ==
    LET resets == {index \in 1..Len(PendingFrames) : IsReset(PendingFrames, index)}
    IN  IF resets = {} THEN 0 ELSE CHOOSE index \in resets : \A other \in resets : other <= index

NewestPending == SubSeq(PendingFrames, LastPendingReset + 1, Len(PendingFrames))

(* Domains of the newest prefix the receiver holds once everything pending
   applies: the frames after the last pending reset, or else the stage and the
   pending frames. A well-formed prefix has at most one. *)
HeldDomains ==
    {NewestPending[index].dom : index \in 1..Len(NewestPending)}
    \cup (IF LastPendingReset = 0 /\ stageDom # None THEN {stageDom} ELSE {})

(* The inbox flag covers every queued reset and every reset drained in this
   generation, and is only set while some reset is still queued or drained. *)
ResetPendingTracksResets ==
    /\ QueuedReset => resetPending
    /\ (inflight.kind = "reset" /\ ~inflightStale) => resetPending
    /\ resetPending => ResetAmongPending

(* The received identity names the held prefix, except after a replay request
   with a reset pending whose consumer started a newer domain from an empty
   stage since its last mark: the claim then names that older applied replay,
   so the server resets. Domain numbers grow with every replacement. *)
ReceivedNamesHeldPrefix ==
    received # None =>
        \/ HeldDomains \subseteq {received}
        \/ /\ received = appliedId
           /\ stageEmptiedSinceMark
           /\ \A held \in HeldDomains : held > received

(* The applied identity names the stage until the stage is next empty. *)
AppliedNamesStage == appliedId # None => stageEmptiedSinceMark \/ stageDom = appliedId

(* Once stable, a receiver that knew the domain of everything it kept when its
   connection last ended is resumed by an unchanged server. *)
NoSpuriousResetWhenKnown ==
    conn = "down" /\ networkStable /\ ~domainChangedSinceConnect /\ keptPrefixKnown
        => ~ServerResets(HelloSyncFrom, HelloClaim)

(* Not checked: a host-loaded snapshot prefix and a live reset whose
   ReplayComplete never arrived both reset an unchanged server. *)
NoSpuriousReset ==
    conn = "down" /\ networkStable /\ ~domainChangedSinceConnect /\ HeldDomains = {dom}
        => ~ServerResets(HelloSyncFrom, HelloClaim)

StageIsOneDomainPrefix == stageOk

ReadyMeansReplayApplied ==
    ready # NoReady =>
        /\ conn = "up"
        /\ stageLen >= ready.head
        /\ (ready.head = 0 \/ stageDom = ready.dom)

(* Justifies queueing the receiver's own reset without an overflow check. *)
OwnResetPrecedesReplayFromOne ==
    resetRequired =>
        /\ queue = <<>>
        /\ (conn = "handshake" => connSync = 1)

Converged ==
    /\ conn = "up"
    /\ chan = <<>>
    /\ queue = <<>>
    /\ inflight = NoFrame
    /\ ready # NoReady
    /\ stageOk
    /\ stageLen = head
    /\ (head = 0 \/ stageDom = dom)

EventuallyConverged == <>[]Converged

=============================================================================
