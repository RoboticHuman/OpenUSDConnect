----------------------- MODULE TransactionCoordinator -----------------------
EXTENDS Integers, Sequences, TLC

(***************************************************************************
Bounded model of the managed transaction group/fallback coordinator.

One batch interleaves two producer sessions. It contains a duplicate request
and, depending on InvalidA2, either a valid middle transaction or one malformed
transaction that forces the group boundary to roll back and retry every request
individually. Group infrastructure failure is also always possible.

Each transaction contains one abstract event so transaction order and global
event sequence order are the same in this finite model.
***************************************************************************)

CONSTANTS P1, P2, InvalidA2

Producers == {P1, P2}
MaxTxn == 3
MaxEvents == 5
ResultValues == {
    "pending", "committed", "duplicate", "invalid", "unexpected"
}

Request(producer, id, valid) ==
    [producer |-> producer, id |-> id, valid |-> valid]

Batch == <<
    Request(P1, 1, TRUE),
    Request(P2, 1, TRUE),
    Request(P1, 2, ~InvalidA2),
    Request(P2, 2, TRUE),
    Request(P2, 1, TRUE), \* duplicate after P2/2 in group order
    Request(P1, 3, TRUE)
>>

RequestIndex == 1..Len(Batch)
Key(producer, id) == [producer |-> producer, id |-> id]
KeyType == [producer : Producers, id : 1..MaxTxn]
LogRecord == [seq : 1..MaxEvents, producer : Producers, id : 1..MaxTxn]

VARIABLES
    phase,
    cursor,
    progress,
    receipts,
    nextSeq,
    log,
    stage,
    results,
    publishQueue,
    broadcast

vars == <<
    phase, cursor, progress, receipts, nextSeq, log, stage,
    results, publishQueue, broadcast
>>

Init ==
    /\ InvalidA2 \in BOOLEAN
    /\ P1 # P2
    /\ phase = "group"
    /\ cursor = 1
    /\ progress = [producer \in Producers |-> 0]
    /\ receipts = [producer \in Producers |-> 0]
    /\ nextSeq = 1
    /\ log = <<>>
    /\ stage = {}
    /\ results = [index \in RequestIndex |-> "pending"]
    /\ publishQueue = <<>>
    /\ broadcast = <<>>

ExpectedGroupLog == <<
    [seq |-> 1, producer |-> P1, id |-> 1],
    [seq |-> 2, producer |-> P2, id |-> 1],
    [seq |-> 3, producer |-> P1, id |-> 2],
    [seq |-> 4, producer |-> P2, id |-> 2],
    [seq |-> 5, producer |-> P1, id |-> 3]
>>

ExpectedGroupStage == {
    Key(P1, 1), Key(P2, 1), Key(P1, 2), Key(P2, 2), Key(P1, 3)
}

GroupApply ==
    /\ phase = "group"
    /\ ~InvalidA2
    /\ phase' = "group_applied"
    /\ stage' = ExpectedGroupStage
    /\ UNCHANGED <<
        cursor, progress, receipts, nextSeq, log, results,
        publishQueue, broadcast
       >>

GroupPersist ==
    /\ phase = "group_applied"
    /\ phase' = "done"
    /\ cursor' = Len(Batch) + 1
    /\ progress' = [producer \in Producers |->
        IF producer = P1 THEN 3 ELSE 2]
    /\ receipts' = progress'
    /\ nextSeq' = 6
    /\ log' = ExpectedGroupLog
    /\ results' = [index \in RequestIndex |->
        IF index = 5 THEN "duplicate" ELSE "committed"]
    /\ publishQueue' = <<1, 2, 3, 4, 5>>
    /\ UNCHANGED <<stage, broadcast>>

GroupFailure ==
    /\ phase = "group"
    /\ phase' = "fallback"
    /\ cursor' = 1
    /\ UNCHANGED <<
        progress, receipts, nextSeq, log, stage, results,
        publishQueue, broadcast
       >>

GroupRollback ==
    /\ phase = "group_applied"
    /\ phase' = "fallback"
    /\ cursor' = 1
    /\ stage' = {}
    /\ UNCHANGED <<
        progress, receipts, nextSeq, log, results, publishQueue, broadcast
       >>

GroupStep == GroupApply \/ GroupPersist \/ GroupFailure \/ GroupRollback

AdvanceFallback ==
    /\ cursor' = cursor + 1
    /\ phase' = IF cursor = Len(Batch) THEN "done" ELSE "fallback"

FallbackDuplicate ==
    /\ phase = "fallback"
    /\ LET request == Batch[cursor]
           producer == request.producer
       IN /\ request.id <= progress[producer]
          /\ results' = [results EXCEPT ![cursor] = "duplicate"]
    /\ AdvanceFallback
    /\ UNCHANGED <<
        progress, receipts, nextSeq, log, stage, publishQueue, broadcast
       >>

FallbackUnexpected ==
    /\ phase = "fallback"
    /\ LET request == Batch[cursor]
           producer == request.producer
       IN /\ request.id > progress[producer] + 1
          /\ results' = [results EXCEPT ![cursor] = "unexpected"]
    /\ AdvanceFallback
    /\ UNCHANGED <<
        progress, receipts, nextSeq, log, stage, publishQueue, broadcast
       >>

FallbackInvalid ==
    /\ phase = "fallback"
    /\ LET request == Batch[cursor]
           producer == request.producer
       IN /\ request.id = progress[producer] + 1
          /\ ~request.valid
          /\ results' = [results EXCEPT ![cursor] = "invalid"]
    /\ AdvanceFallback
    /\ UNCHANGED <<
        progress, receipts, nextSeq, log, stage, publishQueue, broadcast
       >>

FallbackCommit ==
    /\ phase = "fallback"
    /\ LET request == Batch[cursor]
           producer == request.producer
       IN /\ request.id = progress[producer] + 1
          /\ request.valid
          /\ progress' = [progress EXCEPT ![producer] = request.id]
          /\ receipts' = [receipts EXCEPT ![producer] = request.id]
          /\ log' = Append(
              log,
              [seq |-> nextSeq, producer |-> producer, id |-> request.id]
             )
          /\ stage' = stage \union {Key(producer, request.id)}
          /\ results' = [results EXCEPT ![cursor] = "committed"]
          /\ publishQueue' = Append(publishQueue, nextSeq)
    /\ nextSeq' = nextSeq + 1
    /\ AdvanceFallback
    /\ UNCHANGED broadcast

FallbackStep ==
    \/ FallbackDuplicate
    \/ FallbackUnexpected
    \/ FallbackInvalid
    \/ FallbackCommit

DeliverBroadcast ==
    /\ Len(publishQueue) > 0
    /\ broadcast' = Append(broadcast, Head(publishQueue))
    /\ publishQueue' = Tail(publishQueue)
    /\ UNCHANGED <<
        phase, cursor, progress, receipts, nextSeq, log, stage, results
       >>

DropBroadcast ==
    /\ Len(publishQueue) > 0
    /\ publishQueue' = Tail(publishQueue)
    /\ UNCHANGED <<
        phase, cursor, progress, receipts, nextSeq, log, stage,
        results, broadcast
       >>

PublishStep == DeliverBroadcast \/ DropBroadcast

Next == GroupStep \/ FallbackStep \/ PublishStep

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(GroupStep)
    /\ WF_vars(FallbackStep)
    /\ WF_vars(PublishStep)

TypeOK ==
    /\ phase \in {"group", "group_applied", "fallback", "done"}
    /\ cursor \in 1..(Len(Batch) + 1)
    /\ progress \in [Producers -> 0..MaxTxn]
    /\ receipts \in [Producers -> 0..MaxTxn]
    /\ nextSeq \in 1..(MaxEvents + 1)
    /\ log \in Seq(LogRecord)
    /\ stage \subseteq KeyType
    /\ results \in [RequestIndex -> ResultValues]
    /\ publishQueue \in Seq(1..MaxEvents)
    /\ broadcast \in Seq(1..MaxEvents)

ReceiptAndProgressAreAtomic == receipts = progress

LogSequenceIsContiguous ==
    /\ Len(log) = nextSeq - 1
    /\ \A index \in 1..Len(log): log[index].seq = index

StageMatchesDurableLog ==
    IF phase = "group_applied"
    THEN /\ stage = ExpectedGroupStage
         /\ log = <<>>
    ELSE stage = {
             Key(log[index].producer, log[index].id) : index \in 1..Len(log)
         }

PerProducerCommitIsPrefix ==
    \A producer \in Producers, id \in 1..MaxTxn:
        (id <= progress[producer]) =
            (\E index \in 1..Len(log):
                Key(log[index].producer, log[index].id) = Key(producer, id))

TransactionAppliedAtMostOnce ==
    \A first, second \in 1..Len(log):
        first # second =>
            Key(log[first].producer, log[first].id)
            # Key(log[second].producer, log[second].id)

BroadcastFollowsCommitOrder ==
    /\ \A index \in 1..Len(broadcast): broadcast[index] <= Len(log)
    /\ \A first, second \in 1..Len(broadcast):
        first < second => broadcast[first] < broadcast[second]

TerminalResultsAreSound ==
    \A index \in RequestIndex:
        LET request == Batch[index]
            key == Key(request.producer, request.id)
        IN /\ (results[index] = "committed" => key \in stage)
           /\ (results[index] = "duplicate" => key \in stage)
           /\ (results[index] = "invalid" => (~request.valid /\ key \notin stage))
           /\ (results[index] = "unexpected" => key \notin stage)

InvalidTransactionNeverCommits ==
    \A index \in RequestIndex:
        ~Batch[index].valid =>
            Key(Batch[index].producer, Batch[index].id) \notin stage

FallbackStartsAfterCleanRollback ==
    phase = "fallback" /\ cursor = 1 =>
        /\ nextSeq = 1
        /\ log = <<>>
        /\ stage = {}
        /\ progress = [producer \in Producers |-> 0]
        /\ receipts = [producer \in Producers |-> 0]

TentativeGroupIsNotPublished ==
    phase = "group_applied" =>
        /\ progress = [producer \in Producers |-> 0]
        /\ receipts = [producer \in Producers |-> 0]
        /\ nextSeq = 1
        /\ results = [index \in RequestIndex |-> "pending"]
        /\ publishQueue = <<>>
        /\ broadcast = <<>>

DoneHasExpectedOutcome ==
    phase = "done" =>
        IF InvalidA2
        THEN /\ progress[P1] = 1
             /\ progress[P2] = 2
             /\ Len(log) = 3
             /\ results = <<
                 "committed", "committed", "invalid",
                 "committed", "duplicate", "unexpected"
                >>
        ELSE /\ progress[P1] = 3
             /\ progress[P2] = 2
             /\ Len(log) = 5
             /\ results = <<
                 "committed", "committed", "committed",
                 "committed", "duplicate", "committed"
                >>

EventuallyCompletes ==
    <> (phase = "done" /\ Len(publishQueue) = 0)

=============================================================================
