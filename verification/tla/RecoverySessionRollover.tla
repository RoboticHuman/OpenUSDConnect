---------------------- MODULE RecoverySessionRollover ----------------------
EXTENDS Integers, TLC

(***************************************************************************
Bounded model of high-level recovery after an explicit transaction rejection.

The rejected producer session is abandoned as a whole. Its rejected ID and
later suffix remain preserved as an artifact, but can never commit. The client
must replay through a fresh authoritative checkpoint and select server state
before a new producer session can submit transaction 1.

Payload semantics and conflict-aware local rebase are outside this model.
***************************************************************************)

CONSTANTS MaxOld, RejectTxn, MaxNew

OldIds == 1..MaxOld
NewIds == 1..MaxNew
RejectedSuffix == RejectTxn..MaxOld

VARIABLES
    phase,
    oldHigh,
    oldApplyCount,
    artifact,
    abandoned,
    abandonedOldHigh,
    serverSeq,
    receiverSeq,
    checkpointSeq,
    activationSeq,
    concurrentCommitPending,
    localState,
    newSession,
    newNext,
    newHigh,
    newApplyCount

vars == <<
    phase,
    oldHigh,
    oldApplyCount,
    artifact,
    abandoned,
    abandonedOldHigh,
    serverSeq,
    receiverSeq,
    checkpointSeq,
    activationSeq,
    concurrentCommitPending,
    localState,
    newSession,
    newNext,
    newHigh,
    newApplyCount
>>

Init ==
    /\ MaxOld >= 1
    /\ RejectTxn \in OldIds
    /\ MaxNew >= 1
    /\ phase = "sending"
    /\ oldHigh = 0
    /\ oldApplyCount = [id \in OldIds |-> 0]
    /\ artifact = {}
    /\ abandoned = FALSE
    /\ abandonedOldHigh = 0
    /\ serverSeq = 0
    /\ receiverSeq = 0
    /\ checkpointSeq = 0
    /\ activationSeq = 0
    /\ concurrentCommitPending = TRUE
    /\ localState = "optimistic"
    /\ newSession = FALSE
    /\ newNext = 1
    /\ newHigh = 0
    /\ newApplyCount = [id \in NewIds |-> 0]

CommitOldPrefix ==
    /\ phase = "sending"
    /\ oldHigh + 1 < RejectTxn
    /\ oldHigh' = oldHigh + 1
    /\ oldApplyCount' = [oldApplyCount EXCEPT ![oldHigh + 1] = @ + 1]
    /\ serverSeq' = serverSeq + 1
    /\ UNCHANGED <<
        phase, artifact, abandoned, abandonedOldHigh, receiverSeq,
        checkpointSeq, activationSeq, concurrentCommitPending, localState,
        newSession, newNext,
        newHigh, newApplyCount
       >>

Reject ==
    /\ phase = "sending"
    /\ oldHigh = RejectTxn - 1
    /\ phase' = "rejected"
    /\ artifact' = RejectedSuffix
    /\ UNCHANGED <<
        oldHigh, oldApplyCount, abandoned, abandonedOldHigh, serverSeq,
        receiverSeq, checkpointSeq, activationSeq, concurrentCommitPending,
        localState, newSession,
        newNext, newHigh, newApplyCount
       >>

AbandonAndRequestReplay ==
    /\ phase = "rejected"
    /\ phase' = "syncing"
    /\ abandoned' = TRUE
    /\ abandonedOldHigh' = oldHigh
    /\ checkpointSeq' = serverSeq
    /\ UNCHANGED <<
        oldHigh, oldApplyCount, artifact, serverSeq, receiverSeq,
        activationSeq, concurrentCommitPending, localState, newSession,
        newNext, newHigh,
        newApplyCount
       >>

ApplyReplay ==
    /\ phase = "syncing"
    /\ receiverSeq < checkpointSeq
    /\ receiverSeq' = receiverSeq + 1
    /\ UNCHANGED <<
        phase, oldHigh, oldApplyCount, artifact, abandoned,
        abandonedOldHigh, serverSeq, checkpointSeq, activationSeq,
        concurrentCommitPending, localState, newSession, newNext, newHigh,
        newApplyCount
       >>

ConcurrentAuthoritativeCommit ==
    /\ phase = "syncing"
    /\ concurrentCommitPending
    /\ serverSeq' = serverSeq + 1
    /\ concurrentCommitPending' = FALSE
    /\ UNCHANGED <<
        phase, oldHigh, oldApplyCount, artifact, abandoned,
        abandonedOldHigh, receiverSeq, checkpointSeq, activationSeq,
        localState, newSession, newNext, newHigh, newApplyCount
       >>

RefreshCheckpoint ==
    /\ phase = "syncing"
    /\ checkpointSeq < serverSeq
    /\ checkpointSeq' = serverSeq
    /\ UNCHANGED <<
        phase, oldHigh, oldApplyCount, artifact, abandoned,
        abandonedOldHigh, serverSeq, receiverSeq, activationSeq,
        concurrentCommitPending, localState, newSession, newNext, newHigh,
        newApplyCount
       >>

UseServerAndStartNewSession ==
    /\ phase = "syncing"
    /\ receiverSeq = checkpointSeq
    /\ checkpointSeq = serverSeq
    /\ phase' = "ready"
    /\ localState' = "authoritative"
    /\ newSession' = TRUE
    /\ activationSeq' = receiverSeq
    /\ UNCHANGED <<
        oldHigh, oldApplyCount, artifact, abandoned, abandonedOldHigh,
        serverSeq, receiverSeq, checkpointSeq, concurrentCommitPending,
        newNext, newHigh,
        newApplyCount
       >>

SubmitNew ==
    /\ phase = "ready"
    /\ newSession
    /\ newNext <= MaxNew
    /\ newNext' = newNext + 1
    /\ UNCHANGED <<
        phase, oldHigh, oldApplyCount, artifact, abandoned,
        abandonedOldHigh, serverSeq, receiverSeq, checkpointSeq,
        activationSeq, concurrentCommitPending, localState, newSession,
        newHigh, newApplyCount
       >>

CommitNew ==
    /\ phase = "ready"
    /\ newHigh + 1 < newNext
    /\ newHigh' = newHigh + 1
    /\ newApplyCount' = [newApplyCount EXCEPT ![newHigh + 1] = @ + 1]
    /\ serverSeq' = serverSeq + 1
    /\ UNCHANGED <<
        phase, oldHigh, oldApplyCount, artifact, abandoned,
        abandonedOldHigh, receiverSeq, checkpointSeq, activationSeq,
        concurrentCommitPending, localState, newSession, newNext
       >>

Complete ==
    /\ phase = "ready"
    /\ newHigh = MaxNew
    /\ phase' = "done"
    /\ UNCHANGED <<
        oldHigh, oldApplyCount, artifact, abandoned, abandonedOldHigh,
        serverSeq, receiverSeq, checkpointSeq, activationSeq, localState,
        concurrentCommitPending, newSession, newNext, newHigh, newApplyCount
       >>

Next ==
    \/ CommitOldPrefix
    \/ Reject
    \/ AbandonAndRequestReplay
    \/ ApplyReplay
    \/ ConcurrentAuthoritativeCommit
    \/ RefreshCheckpoint
    \/ UseServerAndStartNewSession
    \/ SubmitNew
    \/ CommitNew
    \/ Complete

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(CommitOldPrefix)
    /\ WF_vars(Reject)
    /\ WF_vars(AbandonAndRequestReplay)
    /\ WF_vars(ApplyReplay)
    /\ WF_vars(RefreshCheckpoint)
    /\ WF_vars(UseServerAndStartNewSession)
    /\ WF_vars(SubmitNew)
    /\ WF_vars(CommitNew)
    /\ WF_vars(Complete)

TypeOK ==
    /\ phase \in {"sending", "rejected", "syncing", "ready", "done"}
    /\ oldHigh \in 0..MaxOld
    /\ oldApplyCount \in [OldIds -> 0..1]
    /\ artifact \subseteq OldIds
    /\ abandoned \in BOOLEAN
    /\ abandonedOldHigh \in 0..MaxOld
    /\ serverSeq \in 0..(MaxOld + MaxNew + 1)
    /\ receiverSeq \in 0..(MaxOld + 1)
    /\ checkpointSeq \in 0..(MaxOld + 1)
    /\ activationSeq \in 0..(MaxOld + 1)
    /\ concurrentCommitPending \in BOOLEAN
    /\ localState \in {"optimistic", "authoritative"}
    /\ newSession \in BOOLEAN
    /\ newNext \in 1..(MaxNew + 1)
    /\ newHigh \in 0..MaxNew
    /\ newApplyCount \in [NewIds -> 0..1]

OldCommittedPrefix ==
    \A id \in OldIds : oldApplyCount[id] = IF id <= oldHigh THEN 1 ELSE 0

AbandonmentFreezesOldSession ==
    abandoned => oldHigh = abandonedOldHigh

ArtifactPreservesWholeSuffix ==
    phase # "sending" => artifact = RejectedSuffix

RejectedSuffixNeverCommits ==
    phase # "sending" =>
        \A id \in RejectedSuffix : oldApplyCount[id] = 0

NewSessionStartsFromAuthoritativeCheckpoint ==
    newSession =>
        /\ localState = "authoritative"
        /\ activationSeq = checkpointSeq
        /\ activationSeq <= serverSeq

NoNewCommitBeforeNewSession ==
    newHigh > 0 => newSession

NewCommittedPrefix ==
    \A id \in NewIds : newApplyCount[id] = IF id <= newHigh THEN 1 ELSE 0

EventuallyReady == <> (phase \in {"ready", "done"})
EventuallyComplete == <> (phase = "done")

=============================================================================
