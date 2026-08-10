------------------------- MODULE TransactionRecovery -------------------------
EXTENDS Integers, Sequences, TLC

(***************************************************************************
This is a bounded abstraction of one OpenUSDConnect producer session.

It models the protocol-critical behavior implemented by EventSender and the
server transaction coordinator:

* producer transaction IDs are assigned monotonically;
* the server commits only committedThrough + 1;
* an older ID is a duplicate and returns the cumulative high-watermark;
* disconnects can lose an ACK after the durable commit;
* reconnect replays the exact ordered outbox;
* a recoverable rejection removes only the rejected ID and quarantines the
  later suffix;
* repair inserts new bytes at the rejected ID before replaying the suffix.

Payload semantics and USD composition are deliberately outside this model.
RejectTxn uses BadPayload so every fair behavior exercises rejection and
same-ID repair. Other transactions use GoodPayload.
***************************************************************************)

CONSTANTS MaxTxn, RejectTxn, NonePayload, BadPayload, GoodPayload

TxnIds == 1..MaxTxn
Payloads == {NonePayload, BadPayload, GoodPayload}
RealPayloads == {BadPayload, GoodPayload}
ResultKinds == {"ack", "recoverable", "fatal"}
FailureKinds == {"none", "recoverable", "fatal"}

TxnMessage == [id : TxnIds, payload : RealPayloads]
ResultMessage == [kind : ResultKinds, id : TxnIds, high : 0..MaxTxn]

VARIABLES
    nextId,             \* next producer ID to assign
    outbox,             \* durable client bytes by transaction ID
    connected,
    networkStable,      \* once true, adversarial disconnects stop
    wire,               \* ordered frames on the current connection
    results,            \* ordered results on the current connection
    serverHigh,         \* durable committed-through watermark
    committedPayload,   \* payload applied for each committed ID
    applyCount,         \* number of semantic applications per ID
    clientAck,          \* cumulative result applied by the client
    failureKind,
    rejectedId

vars == <<
    nextId, outbox, connected, networkStable, wire, results,
    serverHigh, committedPayload, applyCount, clientAck,
    failureKind, rejectedId
>>

RECURSIVE Replay(_, _)
Replay(box, id) ==
    IF id > MaxTxn
    THEN <<>>
    ELSE IF box[id] = NonePayload
         THEN Replay(box, id + 1)
         ELSE <<[id |-> id, payload |-> box[id]]>> \o Replay(box, id + 1)

Init ==
    /\ nextId = 1
    /\ outbox = [id \in TxnIds |-> NonePayload]
    /\ connected = FALSE
    /\ networkStable = FALSE
    /\ wire = <<>>
    /\ results = <<>>
    /\ serverHigh = 0
    /\ committedPayload = [id \in TxnIds |-> NonePayload]
    /\ applyCount = [id \in TxnIds |-> 0]
    /\ clientAck = 0
    /\ failureKind = "none"
    /\ rejectedId = 0

StabilizeNetwork ==
    /\ ~networkStable
    /\ networkStable' = TRUE
    /\ UNCHANGED <<
        nextId, outbox, connected, wire, results, serverHigh,
        committedPayload, applyCount, clientAck, failureKind, rejectedId
       >>

Connect ==
    /\ ~connected
    /\ failureKind = "none"
    /\ connected' = TRUE
    /\ wire' = Replay(outbox, 1)
    /\ results' = <<>>
    /\ UNCHANGED <<
        nextId, outbox, networkStable, serverHigh, committedPayload,
        applyCount, clientAck, failureKind, rejectedId
       >>

Disconnect ==
    /\ connected
    /\ ~networkStable
    /\ connected' = FALSE
    /\ wire' = <<>>
    /\ results' = <<>>
    /\ UNCHANGED <<
        nextId, outbox, networkStable, serverHigh, committedPayload,
        applyCount, clientAck, failureKind, rejectedId
       >>

Submit ==
    /\ connected
    /\ failureKind = "none"
    /\ nextId <= MaxTxn
    /\ LET payload == IF nextId = RejectTxn THEN BadPayload ELSE GoodPayload
       IN /\ outbox' = [outbox EXCEPT ![nextId] = payload]
          /\ wire' = Append(wire, [id |-> nextId, payload |-> payload])
    /\ nextId' = nextId + 1
    /\ UNCHANGED <<
        connected, networkStable, results, serverHigh, committedPayload,
        applyCount, clientAck, failureKind, rejectedId
       >>

ServerDuplicate(message) ==
    /\ message.id <= serverHigh
    /\ wire' = Tail(wire)
    /\ results' = Append(
        results,
        [kind |-> "ack", id |-> message.id, high |-> serverHigh]
       )
    /\ UNCHANGED <<serverHigh, committedPayload, applyCount>>

ServerRecoverableReject(message) ==
    /\ message.id = serverHigh + 1
    /\ message.payload = BadPayload
    /\ wire' = Tail(wire)
    /\ results' = Append(
        results,
        [kind |-> "recoverable", id |-> message.id, high |-> serverHigh]
       )
    /\ UNCHANGED <<serverHigh, committedPayload, applyCount>>

ServerCommit(message) ==
    /\ message.id = serverHigh + 1
    /\ message.payload = GoodPayload
    /\ serverHigh' = message.id
    /\ committedPayload' = [committedPayload EXCEPT ![message.id] = message.payload]
    /\ applyCount' = [applyCount EXCEPT ![message.id] = @ + 1]
    /\ wire' = Tail(wire)
    /\ results' = Append(
        results,
        [kind |-> "ack", id |-> message.id, high |-> message.id]
       )

ServerUnexpected(message) ==
    /\ message.id > serverHigh + 1
    /\ wire' = Tail(wire)
    /\ results' = Append(
        results,
        [kind |-> "fatal", id |-> message.id, high |-> serverHigh]
       )
    /\ UNCHANGED <<serverHigh, committedPayload, applyCount>>

ServerStep ==
    /\ connected
    /\ Len(wire) > 0
    /\ LET message == Head(wire)
       IN \/ ServerDuplicate(message)
          \/ ServerRecoverableReject(message)
          \/ ServerCommit(message)
          \/ ServerUnexpected(message)
    /\ UNCHANGED <<
        nextId, outbox, connected, networkStable, clientAck,
        failureKind, rejectedId
       >>

ApplyAck(result) ==
    /\ outbox' = [id \in TxnIds |->
        IF id <= result.high THEN NonePayload ELSE outbox[id]]
    /\ clientAck' = result.high
    /\ results' = Tail(results)
    /\ UNCHANGED <<connected, wire, failureKind, rejectedId>>

ApplyRecoverable(result) ==
    /\ outbox' = [outbox EXCEPT ![result.id] = NonePayload]
    /\ failureKind' = "recoverable"
    /\ rejectedId' = result.id
    /\ connected' = FALSE
    /\ wire' = <<>>
    /\ results' = <<>>
    /\ UNCHANGED clientAck

ApplyFatal(result) ==
    /\ failureKind' = "fatal"
    /\ rejectedId' = result.id
    /\ connected' = FALSE
    /\ wire' = <<>>
    /\ results' = <<>>
    /\ UNCHANGED <<outbox, clientAck>>

ReceiveResult ==
    /\ connected
    /\ Len(results) > 0
    /\ LET result == Head(results)
       IN \/ (result.kind = "ack" /\ ApplyAck(result))
          \/ (result.kind = "recoverable" /\ ApplyRecoverable(result))
          \/ (result.kind = "fatal" /\ ApplyFatal(result))
    /\ UNCHANGED <<
        nextId, networkStable, serverHigh, committedPayload, applyCount
       >>

Repair ==
    /\ failureKind = "recoverable"
    /\ outbox' = [outbox EXCEPT ![rejectedId] = GoodPayload]
    /\ failureKind' = "none"
    /\ rejectedId' = 0
    /\ UNCHANGED <<
        nextId, connected, networkStable, wire, results, serverHigh,
        committedPayload, applyCount, clientAck
       >>

Next ==
    \/ StabilizeNetwork
    \/ Connect
    \/ Disconnect
    \/ Submit
    \/ ServerStep
    \/ ReceiveResult
    \/ Repair

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(StabilizeNetwork)
    /\ WF_vars(Connect)
    /\ WF_vars(Submit)
    /\ WF_vars(ServerStep)
    /\ WF_vars(ReceiveResult)
    /\ WF_vars(Repair)

TypeOK ==
    /\ MaxTxn >= 1
    /\ RejectTxn \in TxnIds
    /\ nextId \in 1..(MaxTxn + 1)
    /\ outbox \in [TxnIds -> Payloads]
    /\ connected \in BOOLEAN
    /\ networkStable \in BOOLEAN
    /\ wire \in Seq(TxnMessage)
    /\ results \in Seq(ResultMessage)
    /\ serverHigh \in 0..MaxTxn
    /\ committedPayload \in [TxnIds -> Payloads]
    /\ applyCount \in [TxnIds -> 0..1]
    /\ clientAck \in 0..MaxTxn
    /\ failureKind \in FailureKinds
    /\ rejectedId \in 0..MaxTxn

CommittedPrefix ==
    /\ \A id \in 1..serverHigh:
        /\ applyCount[id] = 1
        /\ committedPayload[id] # NonePayload
    /\ \A id \in (serverHigh + 1)..MaxTxn:
        /\ applyCount[id] = 0
        /\ committedPayload[id] = NonePayload

ClientAckIsDurable == clientAck <= serverHigh

OnlyAssignedTransactionsExist ==
    \A id \in nextId..MaxTxn: outbox[id] = NonePayload

FailureQuarantinesTransport ==
    failureKind # "none" => (~connected /\ Len(wire) = 0)

RecoverableFailureIsTheGap ==
    failureKind = "recoverable" =>
        /\ rejectedId = serverHigh + 1
        /\ outbox[rejectedId] = NonePayload

OrderedWire ==
    \A first, second \in 1..Len(wire):
        first < second => wire[first].id < wire[second].id

NoUnfillableOutboxGap ==
    serverHigh < MaxTxn =>
        \A later \in (serverHigh + 2)..MaxTxn:
            outbox[later] # NonePayload =>
                \/ outbox[serverHigh + 1] # NonePayload
                \/ failureKind = "recoverable"

EventuallyComplete ==
    <> (
        /\ nextId = MaxTxn + 1
        /\ serverHigh = MaxTxn
        /\ clientAck = MaxTxn
        /\ failureKind = "none"
        /\ \A id \in TxnIds: outbox[id] = NonePayload
    )

=============================================================================
