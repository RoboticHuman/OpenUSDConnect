------------------------- MODULE SharedLayerGraphRace -------------------------
EXTENDS Integers, Sequences, TLC

(***************************************************************************
Bounded abstraction of a shared-stage topology edit prepared before the server
commit lock. A competing revision or generation change may make that prepared
edit stale. Stale rejection must not alter the graph, abstract stage, durable
state, global sequence, or log. Repair binds a replacement edit to the current
generation/revision before it may commit. Alternatively, a clean-stage rebind
or integration-owned reconciliation removes every local conflict that still
intersects the authoritative graph before the old session is abandoned.
***************************************************************************)

CONSTANTS Root, LayerA, LayerB

Layers == {Root, LayerA, LayerB}
MaxLog == 4
GraphState == SUBSET Layers
LogRecord == [seq : 1..MaxLog, generation : 1..2, revision : 1..3,
              state : GraphState]

VARIABLES
    phase,
    networkStable,
    generation,
    revision,
    reachable,
    stageState,
    durableState,
    nextSeq,
    log,
    preparedGeneration,
    preparedRevision,
    preparedTarget,
    preparedProposal,
    rejectedGeneration,
    rejectedRevision,
    rejectedState,
    rejectedNextSeq,
    artifactTargets,
    localConflicts,
    abandoned

vars == <<
    phase, networkStable, generation, revision, reachable, stageState,
    durableState, nextSeq, log, preparedGeneration, preparedRevision,
    preparedTarget, preparedProposal, rejectedGeneration, rejectedRevision,
    rejectedState, rejectedNextSeq, artifactTargets, localConflicts, abandoned
>>

InitialState == {Root, LayerA}
CompetingState == {Root, LayerB}
RepairedState == {Root, LayerA, LayerB}

Init ==
    /\ Root # LayerA /\ Root # LayerB /\ LayerA # LayerB
    /\ phase = "idle"
    /\ networkStable = FALSE
    /\ generation = 1
    /\ revision = 1
    /\ reachable = InitialState
    /\ stageState = InitialState
    /\ durableState = InitialState
    /\ nextSeq = 1
    /\ log = <<>>
    /\ preparedGeneration = 1
    /\ preparedRevision = 1
    /\ preparedTarget = Root
    /\ preparedProposal = InitialState
    /\ rejectedGeneration = 1
    /\ rejectedRevision = 1
    /\ rejectedState = InitialState
    /\ rejectedNextSeq = 1
    /\ artifactTargets \in {{LayerA}, {LayerA, Root}}
    /\ localConflicts = artifactTargets
    /\ abandoned = FALSE

StabilizeNetwork ==
    /\ ~networkStable
    /\ networkStable' = TRUE
    /\ UNCHANGED <<
        phase, generation, revision, reachable, stageState, durableState,
        nextSeq, log, preparedGeneration, preparedRevision, preparedTarget,
        preparedProposal, rejectedGeneration, rejectedRevision,
        rejectedState, rejectedNextSeq, artifactTargets, localConflicts,
        abandoned
       >>

Prepare ==
    /\ phase = "idle"
    /\ phase' = "prepared"
    /\ preparedGeneration' = generation
    /\ preparedRevision' = revision
    /\ preparedTarget' = LayerA
    /\ preparedProposal' = CompetingState
    /\ UNCHANGED <<
        networkStable, generation, revision, reachable, stageState,
        durableState, nextSeq, log, rejectedGeneration, rejectedRevision,
        rejectedState, rejectedNextSeq, artifactTargets, localConflicts,
        abandoned
       >>

AppendGraphCommit(newGeneration, newRevision, newState) ==
    /\ reachable' = newState
    /\ stageState' = newState
    /\ durableState' = newState
    /\ generation' = newGeneration
    /\ revision' = newRevision
    /\ log' = Append(
        log,
        [seq |-> nextSeq, generation |-> newGeneration,
         revision |-> newRevision, state |-> newState]
       )
    /\ nextSeq' = nextSeq + 1

ConcurrentRevision ==
    /\ phase = "prepared"
    /\ ~networkStable
    /\ revision < 2
    /\ nextSeq < MaxLog
    /\ AppendGraphCommit(generation, revision + 1, CompetingState)
    /\ UNCHANGED <<
        phase, networkStable, preparedGeneration, preparedRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedRevision, rejectedState, rejectedNextSeq, artifactTargets,
        localConflicts, abandoned
       >>

ConcurrentGenerationReset ==
    /\ phase = "prepared"
    /\ ~networkStable
    /\ generation = 1
    /\ nextSeq < MaxLog
    /\ AppendGraphCommit(2, 1, CompetingState)
    /\ UNCHANGED <<
        phase, networkStable, preparedGeneration, preparedRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedRevision, rejectedState, rejectedNextSeq, artifactTargets,
        localConflicts, abandoned
       >>

PreparedIsCurrent ==
    /\ preparedGeneration = generation
    /\ preparedRevision = revision
    /\ preparedTarget \in reachable

CommitPrepared ==
    /\ phase = "prepared"
    /\ PreparedIsCurrent
    /\ revision < 3
    /\ nextSeq <= MaxLog
    /\ phase' = "done"
    /\ AppendGraphCommit(generation, revision + 1, preparedProposal)
    /\ UNCHANGED <<
        networkStable, preparedGeneration, preparedRevision, preparedTarget,
        preparedProposal, rejectedGeneration, rejectedRevision,
        rejectedState, rejectedNextSeq, artifactTargets, localConflicts,
        abandoned
       >>

RejectStale ==
    /\ phase = "prepared"
    /\ ~PreparedIsCurrent
    /\ phase' = "rejected"
    /\ rejectedGeneration' = generation
    /\ rejectedRevision' = revision
    /\ rejectedState' = reachable
    /\ rejectedNextSeq' = nextSeq
    /\ UNCHANGED <<
        networkStable, generation, revision, reachable, stageState,
        durableState, nextSeq, log, preparedGeneration, preparedRevision,
        preparedTarget, preparedProposal, artifactTargets, localConflicts,
        abandoned
       >>

Repair ==
    /\ phase = "rejected"
    /\ phase' = "repaired"
    /\ preparedGeneration' = generation
    /\ preparedRevision' = revision
    /\ preparedTarget' = Root
    /\ preparedProposal' = RepairedState
    /\ UNCHANGED <<
        networkStable, generation, revision, reachable, stageState,
        durableState, nextSeq, log, rejectedGeneration, rejectedRevision,
        rejectedState, rejectedNextSeq, artifactTargets, localConflicts,
        abandoned
       >>

ReconcileExternal ==
    /\ phase = "rejected"
    /\ phase' = "reconciled"
    /\ localConflicts' = {}
    /\ UNCHANGED <<
        networkStable, generation, revision, reachable, stageState,
        durableState, nextSeq, log, preparedGeneration, preparedRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedRevision, rejectedState, rejectedNextSeq, artifactTargets,
        abandoned
       >>

AbandonResolved ==
    /\ phase \in {"rejected", "reconciled"}
    /\ localConflicts \cap reachable = {}
    /\ phase' = "done"
    /\ abandoned' = TRUE
    /\ UNCHANGED <<
        networkStable, generation, revision, reachable, stageState,
        durableState, nextSeq, log, preparedGeneration, preparedRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedRevision, rejectedState, rejectedNextSeq, artifactTargets,
        localConflicts
       >>

CommitRepaired ==
    /\ phase = "repaired"
    /\ PreparedIsCurrent
    /\ revision < 3
    /\ nextSeq <= MaxLog
    /\ phase' = "done"
    /\ AppendGraphCommit(generation, revision + 1, preparedProposal)
    /\ UNCHANGED <<
        networkStable, preparedGeneration, preparedRevision, preparedTarget,
        preparedProposal, rejectedGeneration, rejectedRevision,
        rejectedState, rejectedNextSeq, artifactTargets, localConflicts,
        abandoned
       >>

Next ==
    \/ StabilizeNetwork
    \/ Prepare
    \/ ConcurrentRevision
    \/ ConcurrentGenerationReset
    \/ CommitPrepared
    \/ RejectStale
    \/ Repair
    \/ ReconcileExternal
    \/ AbandonResolved
    \/ CommitRepaired

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(StabilizeNetwork)
    /\ WF_vars(Prepare)
    /\ WF_vars(CommitPrepared \/ RejectStale)
    /\ WF_vars(Repair)
    /\ WF_vars(ReconcileExternal)
    /\ WF_vars(AbandonResolved)
    /\ WF_vars(CommitRepaired)

TypeOK ==
    /\ phase \in {"idle", "prepared", "rejected", "reconciled", "repaired", "done"}
    /\ networkStable \in BOOLEAN
    /\ generation \in 1..2
    /\ revision \in 1..3
    /\ reachable \in GraphState
    /\ stageState \in GraphState
    /\ durableState \in GraphState
    /\ nextSeq \in 1..(MaxLog + 1)
    /\ log \in Seq(LogRecord)
    /\ preparedGeneration \in 1..2
    /\ preparedRevision \in 1..3
    /\ preparedTarget \in Layers
    /\ preparedProposal \in GraphState
    /\ rejectedGeneration \in 1..2
    /\ rejectedRevision \in 1..3
    /\ rejectedState \in GraphState
    /\ rejectedNextSeq \in 1..(MaxLog + 1)
    /\ artifactTargets \in SUBSET Layers
    /\ LayerA \in artifactTargets
    /\ localConflicts \in SUBSET artifactTargets
    /\ abandoned \in BOOLEAN

RootIsAlwaysReachable == Root \in reachable

StageGraphAndDurabilityAreAtomic ==
    reachable = stageState /\ stageState = durableState

LogSequenceIsContiguous ==
    /\ Len(log) = nextSeq - 1
    /\ \A index \in 1..Len(log): log[index].seq = index

LatestLogMatchesGraph ==
    Len(log) > 0 =>
        /\ log[Len(log)].generation = generation
        /\ log[Len(log)].revision = revision
        /\ log[Len(log)].state = reachable

StaleRejectionDoesNotMutateState ==
    phase = "rejected" =>
        /\ generation = rejectedGeneration
        /\ revision = rejectedRevision
        /\ reachable = rejectedState
        /\ stageState = rejectedState
        /\ durableState = rejectedState
        /\ nextSeq = rejectedNextSeq
        /\ Len(log) = rejectedNextSeq - 1

RepairedEditUsesCurrentIdentity ==
    phase = "repaired" =>
        /\ preparedGeneration = generation
        /\ preparedRevision = revision
        /\ preparedTarget \in reachable

AbandonmentRequiresNoLiveLocalConflict ==
    abandoned => localConflicts \cap reachable = {}

EventuallyResolves == <> (phase = "done")

=============================================================================
