---------------------- MODULE ReceiverSynchronization ----------------------
EXTENDS FiniteSets, Integers, Sequences, TLC

(***************************************************************************
Bounded model of a receiver that queues network frames for stage-owning code.

The server exposes an ordered durable log. A connection captures replayHead,
delivers its replay prefix, then a ReplayComplete(replayHead) marker, followed
by live events. The consumer publishes applied only after successful stage
application. Disconnect, gap, overflow, or apply failure starts a fresh replay
from applied + 1 and invalidates synchronization.

The model permits queue overflow and one deterministic application failure.
It also permits disconnects and detected gaps until networkStable becomes true.
***************************************************************************)

CONSTANTS InitialHead, MaxSeq, QueueBound, FailSeq

SeqIds == 1..MaxSeq
FrameKinds == {"event", "complete"}
Frame == [kind : FrameKinds, seq : 0..MaxSeq, generation : BOOLEAN]

VARIABLES
    durableHead,
    connected,
    generation,
    networkStable,
    stageReady,
    replayHead,
    nextToSend,
    markerSent,
    queue,
    received,
    applied,
    synchronized,
    synchronizedGeneration,
    failedOnce

vars == <<
    durableHead, connected, generation, networkStable, stageReady, replayHead,
    nextToSend, markerSent, queue, received, applied, synchronized,
    synchronizedGeneration, failedOnce
>>

EventLimit == IF markerSent THEN durableHead ELSE replayHead

EventAvailable ==
    connected /\ nextToSend <= EventLimit

CompleteAvailable ==
    connected /\ ~markerSent /\ nextToSend > replayHead

Init ==
    /\ InitialHead \in 0..MaxSeq
    /\ FailSeq \in SeqIds
    /\ QueueBound >= 1
    /\ durableHead = InitialHead
    /\ connected = FALSE
    /\ generation = FALSE
    /\ networkStable = FALSE
    /\ stageReady = FALSE
    /\ replayHead = 0
    /\ nextToSend = 1
    /\ markerSent = FALSE
    /\ queue = <<>>
    /\ received = 0
    /\ applied = 0
    /\ synchronized = FALSE
    /\ synchronizedGeneration = FALSE
    /\ failedOnce = FALSE

StabilizeNetwork ==
    /\ ~networkStable
    /\ networkStable' = TRUE
    /\ UNCHANGED <<
        durableHead, connected, generation, stageReady, replayHead, nextToSend,
        markerSent, queue, received, applied, synchronized,
        synchronizedGeneration, failedOnce
       >>

MakeStageReady ==
    /\ ~stageReady
    /\ stageReady' = TRUE
    /\ UNCHANGED <<
        durableHead, connected, generation, networkStable, replayHead,
        nextToSend, markerSent, queue, received, applied, synchronized,
        synchronizedGeneration, failedOnce
       >>

AppendLive ==
    /\ durableHead < MaxSeq
    /\ durableHead' = durableHead + 1
    /\ UNCHANGED <<
        connected, generation, networkStable, stageReady, replayHead,
        nextToSend, markerSent, queue, received, applied, synchronized,
        synchronizedGeneration, failedOnce
       >>

Connect ==
    /\ ~connected
    /\ connected' = TRUE
    /\ generation' = ~generation
    /\ replayHead' = durableHead
    /\ nextToSend' = applied + 1
    /\ markerSent' = FALSE
    /\ queue' = <<>>
    /\ received' = applied
    /\ synchronized' = FALSE
    /\ synchronizedGeneration' = ~generation
    /\ UNCHANGED <<
        durableHead, networkStable, stageReady, applied, failedOnce
       >>

ResetForReplay ==
    /\ connected' = FALSE
    /\ replayHead' = 0
    /\ nextToSend' = applied + 1
    /\ markerSent' = FALSE
    /\ queue' = <<>>
    /\ received' = applied
    /\ synchronized' = FALSE

Disconnect ==
    /\ connected
    /\ ~networkStable
    /\ ResetForReplay
    /\ UNCHANGED <<
        durableHead, generation, networkStable, stageReady, applied,
        synchronizedGeneration, failedOnce
       >>

DetectGap ==
    /\ connected
    /\ ~networkStable
    /\ nextToSend <= durableHead
    /\ ResetForReplay
    /\ UNCHANGED <<
        durableHead, generation, networkStable, stageReady, applied,
        synchronizedGeneration, failedOnce
       >>

SendEvent ==
    /\ EventAvailable
    /\ Len(queue) < QueueBound
    /\ queue' = Append(queue, [
        kind |-> "event", seq |-> nextToSend, generation |-> generation
       ])
    /\ received' = nextToSend
    /\ nextToSend' = nextToSend + 1
    /\ UNCHANGED <<
        durableHead, connected, generation, networkStable, stageReady,
        replayHead, markerSent, applied, synchronized,
        synchronizedGeneration, failedOnce
       >>

SendComplete ==
    /\ CompleteAvailable
    /\ Len(queue) < QueueBound
    /\ queue' = Append(queue, [
        kind |-> "complete", seq |-> replayHead, generation |-> generation
       ])
    /\ markerSent' = TRUE
    /\ UNCHANGED <<
        durableHead, connected, generation, networkStable, stageReady,
        replayHead, nextToSend, received, applied, synchronized,
        synchronizedGeneration, failedOnce
       >>

InjectStaleComplete ==
    /\ connected
    /\ ~networkStable
    /\ Len(queue) < QueueBound
    /\ queue' = Append(queue, [
        kind |-> "complete", seq |-> replayHead, generation |-> ~generation
       ])
    /\ UNCHANGED <<
        durableHead, connected, generation, networkStable, stageReady,
        replayHead, nextToSend, markerSent, received, applied, synchronized,
        synchronizedGeneration, failedOnce
       >>

Overflow ==
    /\ connected
    /\ Len(queue) = QueueBound
    /\ (EventAvailable \/ CompleteAvailable)
    /\ ResetForReplay
    /\ UNCHANGED <<
        durableHead, generation, networkStable, stageReady, applied,
        synchronizedGeneration, failedOnce
       >>

ApplyEventSuccess ==
    /\ stageReady
    /\ Len(queue) > 0
    /\ Head(queue).kind = "event"
    /\ Head(queue).generation = generation
    /\ Head(queue).seq = applied + 1
    /\ (Head(queue).seq # FailSeq \/ failedOnce)
    /\ applied' = Head(queue).seq
    /\ queue' = Tail(queue)
    /\ UNCHANGED <<
        durableHead, connected, generation, networkStable, stageReady,
        replayHead, nextToSend, markerSent, received, synchronized,
        synchronizedGeneration, failedOnce
       >>

ApplyEventFailure ==
    /\ stageReady
    /\ Len(queue) > 0
    /\ Head(queue).kind = "event"
    /\ Head(queue).generation = generation
    /\ Head(queue).seq = applied + 1
    /\ Head(queue).seq = FailSeq
    /\ ~failedOnce
    /\ failedOnce' = TRUE
    /\ ResetForReplay
    /\ UNCHANGED <<
        durableHead, generation, networkStable, stageReady, applied,
        synchronizedGeneration
       >>

ApplyCompleteSuccess ==
    /\ stageReady
    /\ Len(queue) > 0
    /\ Head(queue).kind = "complete"
    /\ Head(queue).generation = generation
    /\ Head(queue).seq = applied
    /\ queue' = Tail(queue)
    /\ synchronized' = TRUE
    /\ synchronizedGeneration' = generation
    /\ UNCHANGED <<
        durableHead, connected, generation, networkStable, stageReady,
        replayHead, nextToSend, markerSent, received, applied, failedOnce
       >>

DiscardStaleFrame ==
    /\ stageReady
    /\ Len(queue) > 0
    /\ Head(queue).generation # generation
    /\ queue' = Tail(queue)
    /\ UNCHANGED <<
        durableHead, connected, generation, networkStable, stageReady,
        replayHead, nextToSend, markerSent, received, applied, synchronized,
        synchronizedGeneration, failedOnce
       >>

ApplyHead ==
    \/ ApplyEventSuccess
    \/ ApplyEventFailure
    \/ ApplyCompleteSuccess
    \/ DiscardStaleFrame

Next ==
    \/ StabilizeNetwork
    \/ MakeStageReady
    \/ AppendLive
    \/ Connect
    \/ Disconnect
    \/ DetectGap
    \/ SendEvent
    \/ SendComplete
    \/ InjectStaleComplete
    \/ Overflow
    \/ ApplyHead

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(StabilizeNetwork)
    /\ WF_vars(MakeStageReady)
    /\ WF_vars(AppendLive)
    /\ WF_vars(Connect)
    /\ WF_vars(SendEvent)
    /\ WF_vars(SendComplete)
    /\ SF_vars(ApplyHead)

TypeOK ==
    /\ durableHead \in 0..MaxSeq
    /\ connected \in BOOLEAN
    /\ generation \in BOOLEAN
    /\ networkStable \in BOOLEAN
    /\ stageReady \in BOOLEAN
    /\ replayHead \in 0..MaxSeq
    /\ nextToSend \in 1..(MaxSeq + 1)
    /\ markerSent \in BOOLEAN
    /\ queue \in Seq(Frame)
    /\ received \in 0..MaxSeq
    /\ applied \in 0..MaxSeq
    /\ synchronized \in BOOLEAN
    /\ synchronizedGeneration \in BOOLEAN
    /\ failedOnce \in BOOLEAN

AppliedCursorIsSound ==
    applied <= received /\ received <= durableHead

QueueIsBounded == Len(queue) <= QueueBound

QueuedEventsAreUnapplied ==
    \A index \in 1..Len(queue):
        queue[index].kind = "event" =>
            /\ queue[index].seq > applied
            /\ queue[index].seq <= received

QueuedEventsAreOrdered ==
    \A first, second \in 1..Len(queue):
        first < second
        /\ queue[first].kind = "event"
        /\ queue[second].kind = "event"
        => queue[first].seq < queue[second].seq

ReplayMarkerOrdersLiveTraffic ==
    \A marker, event \in 1..Len(queue):
        queue[marker].kind = "complete"
        /\ queue[event].kind = "event"
        /\ queue[marker].generation = queue[event].generation =>
            /\ (event < marker => queue[event].seq <= queue[marker].seq)
            /\ (marker < event => queue[event].seq > queue[marker].seq)

AtMostOneCurrentReplayMarker ==
    Cardinality({index \in 1..Len(queue):
        queue[index].kind = "complete"
        /\ queue[index].generation = generation}) <= 1

SynchronizedMeansReplayApplied ==
    synchronized =>
        /\ connected
        /\ markerSent
        /\ synchronizedGeneration = generation
        /\ applied >= replayHead
        /\ \A index \in 1..Len(queue):
            queue[index].kind = "event"
            /\ queue[index].generation = generation
            => queue[index].seq > replayHead

DisconnectedIsNotSynchronized == ~connected => ~synchronized

EventuallyCaughtUp ==
    <> (
        /\ durableHead = MaxSeq
        /\ connected
        /\ synchronized
        /\ applied = MaxSeq
    )

=============================================================================
