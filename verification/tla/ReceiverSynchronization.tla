---------------------- MODULE ReceiverSynchronization ----------------------
EXTENDS FiniteSets, Integers, Sequences, TLC

(***************************************************************************
Bounded receiver model shared by the Python and Unreal implementations.

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
Frame == [kind : FrameKinds, seq : 0..MaxSeq]

VARIABLES
    durableHead,
    connected,
    networkStable,
    stageReady,
    replayHead,
    nextToSend,
    markerSent,
    queue,
    received,
    applied,
    synchronized,
    failedOnce

vars == <<
    durableHead, connected, networkStable, stageReady, replayHead,
    nextToSend, markerSent, queue, received, applied, synchronized,
    failedOnce
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
    /\ networkStable = FALSE
    /\ stageReady = FALSE
    /\ replayHead = 0
    /\ nextToSend = 1
    /\ markerSent = FALSE
    /\ queue = <<>>
    /\ received = 0
    /\ applied = 0
    /\ synchronized = FALSE
    /\ failedOnce = FALSE

StabilizeNetwork ==
    /\ ~networkStable
    /\ networkStable' = TRUE
    /\ UNCHANGED <<
        durableHead, connected, stageReady, replayHead, nextToSend,
        markerSent, queue, received, applied, synchronized, failedOnce
       >>

MakeStageReady ==
    /\ ~stageReady
    /\ stageReady' = TRUE
    /\ UNCHANGED <<
        durableHead, connected, networkStable, replayHead, nextToSend,
        markerSent, queue, received, applied, synchronized, failedOnce
       >>

AppendLive ==
    /\ durableHead < MaxSeq
    /\ durableHead' = durableHead + 1
    /\ UNCHANGED <<
        connected, networkStable, stageReady, replayHead, nextToSend,
        markerSent, queue, received, applied, synchronized, failedOnce
       >>

Connect ==
    /\ ~connected
    /\ connected' = TRUE
    /\ replayHead' = durableHead
    /\ nextToSend' = applied + 1
    /\ markerSent' = FALSE
    /\ queue' = <<>>
    /\ received' = applied
    /\ synchronized' = FALSE
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
        durableHead, networkStable, stageReady, applied, failedOnce
       >>

DetectGap ==
    /\ connected
    /\ ~networkStable
    /\ nextToSend <= durableHead
    /\ ResetForReplay
    /\ UNCHANGED <<
        durableHead, networkStable, stageReady, applied, failedOnce
       >>

SendEvent ==
    /\ EventAvailable
    /\ Len(queue) < QueueBound
    /\ queue' = Append(queue, [kind |-> "event", seq |-> nextToSend])
    /\ received' = nextToSend
    /\ nextToSend' = nextToSend + 1
    /\ UNCHANGED <<
        durableHead, connected, networkStable, stageReady, replayHead,
        markerSent, applied, synchronized, failedOnce
       >>

SendComplete ==
    /\ CompleteAvailable
    /\ Len(queue) < QueueBound
    /\ queue' = Append(queue, [kind |-> "complete", seq |-> replayHead])
    /\ markerSent' = TRUE
    /\ UNCHANGED <<
        durableHead, connected, networkStable, stageReady, replayHead,
        nextToSend, received, applied, synchronized, failedOnce
       >>

Overflow ==
    /\ connected
    /\ Len(queue) = QueueBound
    /\ (EventAvailable \/ CompleteAvailable)
    /\ ResetForReplay
    /\ UNCHANGED <<
        durableHead, networkStable, stageReady, applied, failedOnce
       >>

ApplyEventSuccess ==
    /\ stageReady
    /\ Len(queue) > 0
    /\ Head(queue).kind = "event"
    /\ Head(queue).seq = applied + 1
    /\ (Head(queue).seq # FailSeq \/ failedOnce)
    /\ applied' = Head(queue).seq
    /\ queue' = Tail(queue)
    /\ UNCHANGED <<
        durableHead, connected, networkStable, stageReady, replayHead,
        nextToSend, markerSent, received, synchronized, failedOnce
       >>

ApplyEventFailure ==
    /\ stageReady
    /\ Len(queue) > 0
    /\ Head(queue).kind = "event"
    /\ Head(queue).seq = applied + 1
    /\ Head(queue).seq = FailSeq
    /\ ~failedOnce
    /\ failedOnce' = TRUE
    /\ ResetForReplay
    /\ UNCHANGED <<durableHead, networkStable, stageReady, applied>>

ApplyGap ==
    /\ stageReady
    /\ Len(queue) > 0
    /\ Head(queue).kind = "event"
    /\ Head(queue).seq # applied + 1
    /\ ResetForReplay
    /\ UNCHANGED <<
        durableHead, networkStable, stageReady, applied, failedOnce
       >>

ApplyCompleteSuccess ==
    /\ stageReady
    /\ Len(queue) > 0
    /\ Head(queue).kind = "complete"
    /\ Head(queue).seq = applied
    /\ queue' = Tail(queue)
    /\ synchronized' = TRUE
    /\ UNCHANGED <<
        durableHead, connected, networkStable, stageReady, replayHead,
        nextToSend, markerSent, received, applied, failedOnce
       >>

ApplyCompleteFailure ==
    /\ stageReady
    /\ Len(queue) > 0
    /\ Head(queue).kind = "complete"
    /\ Head(queue).seq # applied
    /\ ResetForReplay
    /\ UNCHANGED <<
        durableHead, networkStable, stageReady, applied, failedOnce
       >>

ApplyHead ==
    \/ ApplyEventSuccess
    \/ ApplyEventFailure
    \/ ApplyGap
    \/ ApplyCompleteSuccess
    \/ ApplyCompleteFailure

Next ==
    \/ StabilizeNetwork
    \/ MakeStageReady
    \/ AppendLive
    \/ Connect
    \/ Disconnect
    \/ DetectGap
    \/ SendEvent
    \/ SendComplete
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
    /\ networkStable \in BOOLEAN
    /\ stageReady \in BOOLEAN
    /\ replayHead \in 0..MaxSeq
    /\ nextToSend \in 1..(MaxSeq + 1)
    /\ markerSent \in BOOLEAN
    /\ queue \in Seq(Frame)
    /\ received \in 0..MaxSeq
    /\ applied \in 0..MaxSeq
    /\ synchronized \in BOOLEAN
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
        queue[marker].kind = "complete" /\ queue[event].kind = "event" =>
            /\ (event < marker => queue[event].seq <= queue[marker].seq)
            /\ (marker < event => queue[event].seq > queue[marker].seq)

AtMostOneReplayMarker ==
    Cardinality({index \in 1..Len(queue): queue[index].kind = "complete"}) <= 1

SynchronizedMeansReplayApplied ==
    synchronized =>
        /\ connected
        /\ markerSent
        /\ applied >= replayHead
        /\ \A index \in 1..Len(queue):
            queue[index].kind = "event" => queue[index].seq > replayHead

DisconnectedIsNotSynchronized == ~connected => ~synchronized

EventuallyCaughtUp ==
    <> (
        /\ durableHead = MaxSeq
        /\ connected
        /\ synchronized
        /\ applied = MaxSeq
    )

=============================================================================
