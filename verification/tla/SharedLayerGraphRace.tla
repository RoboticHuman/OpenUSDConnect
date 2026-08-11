------------------------- MODULE SharedLayerGraphRace -------------------------
EXTENDS Integers, Sequences, TLC

(***************************************************************************
Bounded abstraction of shared-stage topology commits. Each parent layer has
its own optimistic revision while a graph generation scopes all revisions.
An unrelated-parent commit must not invalidate prepared work; a same-parent
commit or compaction generation change must. Logical layer keys are durable
even while a layer is detached, and event/log/identity persistence is atomic.
***************************************************************************)

CONSTANTS Root, LayerA, LayerB

Layers == {Root, LayerA, LayerB}
Parents == {Root, LayerA}
MaxLog == 5
TopologyState == [Parents -> SUBSET Layers]
ParentRevisionState == [Parents -> 1..4]
LogRecord == [
    seq : 1..MaxLog,
    kind : {"baseline", "topology"},
    generation : 1..2,
    graphRevision : 1..4,
    parent : Parents,
    parentRevision : 1..4,
    topology : TopologyState,
    knownKeys : SUBSET Layers
]

VARIABLES
    phase,
    networkStable,
    generation,
    graphRevision,
    parentRevision,
    topology,
    stageTopology,
    durableTopology,
    knownKeys,
    durableKnownKeys,
    nextSeq,
    log,
    preparedGeneration,
    preparedParentRevision,
    preparedTarget,
    preparedProposal,
    rejectedGeneration,
    rejectedGraphRevision,
    rejectedParentRevision,
    rejectedTopology,
    rejectedKnownKeys,
    rejectedNextSeq,
    rejectedLog,
    failedGeneration,
    failedGraphRevision,
    failedParentRevision,
    failedTopology,
    failedKnownKeys,
    failedNextSeq,
    failedLog,
    failureInjected,
    artifactTargets,
    localConflicts,
    abandoned,
    unrelatedRaced,
    generationResetSeen,
    preResetKnownKeys

vars == <<
    phase, networkStable, generation, graphRevision, parentRevision,
    topology, stageTopology, durableTopology, knownKeys, durableKnownKeys,
    nextSeq, log, preparedGeneration, preparedParentRevision,
    preparedTarget, preparedProposal, rejectedGeneration,
    rejectedGraphRevision, rejectedParentRevision, rejectedTopology,
    rejectedKnownKeys, rejectedNextSeq, rejectedLog, failedGeneration,
    failedGraphRevision, failedParentRevision, failedTopology,
    failedKnownKeys, failedNextSeq, failedLog, failureInjected,
    artifactTargets, localConflicts, abandoned, unrelatedRaced,
    generationResetSeen, preResetKnownKeys
>>

InitialTopology == [parent \in Parents |-> IF parent = Root THEN {LayerA} ELSE {}]
InitialParentRevisions == [parent \in Parents |-> 1]
InitialKnownKeys == {Root, LayerA}

Reachable(state) ==
    {Root} \cup state[Root]
        \cup UNION {state[parent] : parent \in Parents \cap ({Root} \cup state[Root])}

InitialRecord == [
    seq |-> 1,
    kind |-> "baseline",
    generation |-> 1,
    graphRevision |-> 1,
    parent |-> Root,
    parentRevision |-> 1,
    topology |-> InitialTopology,
    knownKeys |-> InitialKnownKeys
]

Init ==
    /\ Root # LayerA /\ Root # LayerB /\ LayerA # LayerB
    /\ phase = "idle"
    /\ networkStable = FALSE
    /\ generation = 1
    /\ graphRevision = 1
    /\ parentRevision = InitialParentRevisions
    /\ topology = InitialTopology
    /\ stageTopology = InitialTopology
    /\ durableTopology = InitialTopology
    /\ knownKeys = InitialKnownKeys
    /\ durableKnownKeys = InitialKnownKeys
    /\ nextSeq = 2
    /\ log = <<InitialRecord>>
    /\ preparedGeneration = 1
    /\ preparedParentRevision = 1
    /\ preparedTarget = Root
    /\ preparedProposal = InitialTopology[Root]
    /\ rejectedGeneration = 1
    /\ rejectedGraphRevision = 1
    /\ rejectedParentRevision = InitialParentRevisions
    /\ rejectedTopology = InitialTopology
    /\ rejectedKnownKeys = InitialKnownKeys
    /\ rejectedNextSeq = 2
    /\ rejectedLog = <<InitialRecord>>
    /\ failedGeneration = 1
    /\ failedGraphRevision = 1
    /\ failedParentRevision = InitialParentRevisions
    /\ failedTopology = InitialTopology
    /\ failedKnownKeys = InitialKnownKeys
    /\ failedNextSeq = 2
    /\ failedLog = <<InitialRecord>>
    /\ failureInjected = FALSE
    /\ artifactTargets \in {{LayerA}, {LayerA, Root}}
    /\ localConflicts = artifactTargets
    /\ abandoned = FALSE
    /\ unrelatedRaced = FALSE
    /\ generationResetSeen = FALSE
    /\ preResetKnownKeys = InitialKnownKeys

StabilizeNetwork ==
    /\ ~networkStable
    /\ networkStable' = TRUE
    /\ UNCHANGED <<
        phase, generation, graphRevision, parentRevision, topology,
        stageTopology, durableTopology, knownKeys, durableKnownKeys,
        nextSeq, log, preparedGeneration, preparedParentRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedGraphRevision, rejectedParentRevision, rejectedTopology,
        rejectedKnownKeys, rejectedNextSeq, rejectedLog, failedGeneration,
        failedGraphRevision, failedParentRevision, failedTopology,
        failedKnownKeys, failedNextSeq, failedLog, failureInjected,
        artifactTargets, localConflicts, abandoned, unrelatedRaced,
        generationResetSeen, preResetKnownKeys
       >>

Prepare ==
    /\ phase = "idle"
    /\ phase' = "prepared"
    /\ preparedGeneration' = generation
    /\ preparedParentRevision' = parentRevision[Root]
    /\ preparedTarget' = Root
    /\ preparedProposal' = {LayerA, LayerB}
    /\ unrelatedRaced' = FALSE
    /\ UNCHANGED <<
        networkStable, generation, graphRevision, parentRevision, topology,
        stageTopology, durableTopology, knownKeys, durableKnownKeys,
        nextSeq, log, rejectedGeneration, rejectedGraphRevision,
        rejectedParentRevision, rejectedTopology, rejectedKnownKeys,
        rejectedNextSeq, rejectedLog, failedGeneration, failedGraphRevision,
        failedParentRevision, failedTopology, failedKnownKeys,
        failedNextSeq, failedLog, failureInjected, artifactTargets,
        localConflicts, abandoned, generationResetSeen, preResetKnownKeys
       >>

AppendTopologyCommit(parent, newTopology, newParentRevision, newKnownKeys) ==
    /\ generation' = generation
    /\ graphRevision' = graphRevision + 1
    /\ parentRevision' = newParentRevision
    /\ topology' = newTopology
    /\ stageTopology' = newTopology
    /\ durableTopology' = newTopology
    /\ knownKeys' = newKnownKeys
    /\ durableKnownKeys' = newKnownKeys
    /\ log' = Append(
        log,
        [seq |-> nextSeq, kind |-> "topology", generation |-> generation,
         graphRevision |-> graphRevision + 1, parent |-> parent,
         parentRevision |-> newParentRevision[parent],
         topology |-> newTopology, knownKeys |-> newKnownKeys]
       )
    /\ nextSeq' = nextSeq + 1

ConcurrentOtherParent ==
    /\ phase = "prepared"
    /\ ~networkStable
    /\ graphRevision < 3
    /\ parentRevision[LayerA] < 3
    /\ nextSeq <= MaxLog
    /\ LET newTopology == [topology EXCEPT ![LayerA] = {LayerB}]
           newParentRevision == [parentRevision EXCEPT ![LayerA] = @ + 1]
       IN AppendTopologyCommit(
            LayerA,
            newTopology,
            newParentRevision,
            knownKeys \cup {LayerB}
          )
    /\ unrelatedRaced' = (
        /\ preparedGeneration = generation
        /\ preparedParentRevision = parentRevision[preparedTarget]
        /\ preparedTarget \in Reachable(topology)
       )
    /\ UNCHANGED <<
        phase, networkStable, preparedGeneration, preparedParentRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedGraphRevision, rejectedParentRevision, rejectedTopology,
        rejectedKnownKeys, rejectedNextSeq, rejectedLog, failedGeneration,
        failedGraphRevision, failedParentRevision, failedTopology,
        failedKnownKeys, failedNextSeq, failedLog, failureInjected,
        artifactTargets, localConflicts, abandoned, generationResetSeen,
        preResetKnownKeys
       >>

ConcurrentSameParent ==
    /\ phase = "prepared"
    /\ ~networkStable
    /\ graphRevision < 3
    /\ parentRevision[Root] < 3
    /\ nextSeq <= MaxLog
    /\ LET newTopology == [topology EXCEPT ![Root] = {LayerB}]
           newParentRevision == [parentRevision EXCEPT ![Root] = @ + 1]
       IN AppendTopologyCommit(
            Root,
            newTopology,
            newParentRevision,
            knownKeys \cup {LayerB}
          )
    /\ unrelatedRaced' = FALSE
    /\ UNCHANGED <<
        phase, networkStable, preparedGeneration, preparedParentRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedGraphRevision, rejectedParentRevision, rejectedTopology,
        rejectedKnownKeys, rejectedNextSeq, rejectedLog, failedGeneration,
        failedGraphRevision, failedParentRevision, failedTopology,
        failedKnownKeys, failedNextSeq, failedLog, failureInjected,
        artifactTargets, localConflicts, abandoned, generationResetSeen,
        preResetKnownKeys
       >>

CompactToNewGeneration ==
    /\ phase = "prepared"
    /\ ~networkStable
    /\ generation = 1
    /\ generation' = 2
    /\ graphRevision' = 1
    /\ parentRevision' = [parent \in Parents |-> 1]
    /\ topology' = topology
    /\ stageTopology' = topology
    /\ durableTopology' = topology
    /\ knownKeys' = knownKeys
    /\ durableKnownKeys' = knownKeys
    /\ nextSeq' = 2
    /\ log' = <<[
        seq |-> 1, kind |-> "baseline", generation |-> 2,
        graphRevision |-> 1, parent |-> Root,
        parentRevision |-> 1, topology |-> topology,
        knownKeys |-> knownKeys
       ]>>
    /\ unrelatedRaced' = FALSE
    /\ generationResetSeen' = TRUE
    /\ preResetKnownKeys' = knownKeys
    /\ UNCHANGED <<
        phase, networkStable, preparedGeneration, preparedParentRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedGraphRevision, rejectedParentRevision, rejectedTopology,
        rejectedKnownKeys, rejectedNextSeq, rejectedLog, failedGeneration,
        failedGraphRevision, failedParentRevision, failedTopology,
        failedKnownKeys, failedNextSeq, failedLog, failureInjected,
        artifactTargets, localConflicts, abandoned
       >>

PreparedIsCurrent ==
    /\ preparedGeneration = generation
    /\ preparedParentRevision = parentRevision[preparedTarget]
    /\ preparedTarget \in Reachable(topology)

CommitPrepared ==
    /\ phase = "prepared"
    /\ PreparedIsCurrent
    /\ graphRevision < 4
    /\ parentRevision[preparedTarget] < 4
    /\ nextSeq <= MaxLog
    /\ phase' = "done"
    /\ LET newTopology == [topology EXCEPT ![preparedTarget] = preparedProposal]
           newParentRevision == [parentRevision EXCEPT ![preparedTarget] = @ + 1]
       IN AppendTopologyCommit(
            preparedTarget,
            newTopology,
            newParentRevision,
            knownKeys \cup preparedProposal
          )
    /\ UNCHANGED <<
        networkStable, preparedGeneration, preparedParentRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedGraphRevision, rejectedParentRevision, rejectedTopology,
        rejectedKnownKeys, rejectedNextSeq, rejectedLog, failedGeneration,
        failedGraphRevision, failedParentRevision, failedTopology,
        failedKnownKeys, failedNextSeq, failedLog, failureInjected,
        artifactTargets, localConflicts, abandoned, unrelatedRaced,
        generationResetSeen, preResetKnownKeys
       >>

PersistenceFailure ==
    /\ phase = "prepared"
    /\ PreparedIsCurrent
    /\ ~failureInjected
    /\ phase' = "retry"
    /\ failedGeneration' = generation
    /\ failedGraphRevision' = graphRevision
    /\ failedParentRevision' = parentRevision
    /\ failedTopology' = topology
    /\ failedKnownKeys' = knownKeys
    /\ failedNextSeq' = nextSeq
    /\ failedLog' = log
    /\ failureInjected' = TRUE
    /\ UNCHANGED <<
        networkStable, generation, graphRevision, parentRevision, topology,
        stageTopology, durableTopology, knownKeys, durableKnownKeys,
        nextSeq, log, preparedGeneration, preparedParentRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedGraphRevision, rejectedParentRevision, rejectedTopology,
        rejectedKnownKeys, rejectedNextSeq, rejectedLog, artifactTargets,
        localConflicts, abandoned, unrelatedRaced, generationResetSeen,
        preResetKnownKeys
       >>

RetryAfterFailure ==
    /\ phase = "retry"
    /\ phase' = "prepared"
    /\ preparedGeneration' = generation
    /\ preparedParentRevision' = parentRevision[Root]
    /\ preparedTarget' = Root
    /\ preparedProposal' = {LayerA, LayerB}
    /\ unrelatedRaced' = FALSE
    /\ UNCHANGED <<
        networkStable, generation, graphRevision, parentRevision, topology,
        stageTopology, durableTopology, knownKeys, durableKnownKeys,
        nextSeq, log, rejectedGeneration, rejectedGraphRevision,
        rejectedParentRevision, rejectedTopology, rejectedKnownKeys,
        rejectedNextSeq, rejectedLog, failedGeneration, failedGraphRevision,
        failedParentRevision, failedTopology, failedKnownKeys,
        failedNextSeq, failedLog, failureInjected, artifactTargets,
        localConflicts, abandoned, generationResetSeen, preResetKnownKeys
       >>

RejectStale ==
    /\ phase = "prepared"
    /\ ~PreparedIsCurrent
    /\ phase' = "rejected"
    /\ rejectedGeneration' = generation
    /\ rejectedGraphRevision' = graphRevision
    /\ rejectedParentRevision' = parentRevision
    /\ rejectedTopology' = topology
    /\ rejectedKnownKeys' = knownKeys
    /\ rejectedNextSeq' = nextSeq
    /\ rejectedLog' = log
    /\ UNCHANGED <<
        networkStable, generation, graphRevision, parentRevision, topology,
        stageTopology, durableTopology, knownKeys, durableKnownKeys,
        nextSeq, log, preparedGeneration, preparedParentRevision,
        preparedTarget, preparedProposal, failedGeneration,
        failedGraphRevision, failedParentRevision, failedTopology,
        failedKnownKeys, failedNextSeq, failedLog, failureInjected,
        artifactTargets, localConflicts, abandoned, unrelatedRaced,
        generationResetSeen, preResetKnownKeys
       >>

Repair ==
    /\ phase = "rejected"
    /\ phase' = "repaired"
    /\ preparedGeneration' = generation
    /\ preparedParentRevision' = parentRevision[Root]
    /\ preparedTarget' = Root
    /\ preparedProposal' = {LayerA, LayerB}
    /\ UNCHANGED <<
        networkStable, generation, graphRevision, parentRevision, topology,
        stageTopology, durableTopology, knownKeys, durableKnownKeys,
        nextSeq, log, rejectedGeneration, rejectedGraphRevision,
        rejectedParentRevision, rejectedTopology, rejectedKnownKeys,
        rejectedNextSeq, rejectedLog, failedGeneration, failedGraphRevision,
        failedParentRevision, failedTopology, failedKnownKeys,
        failedNextSeq, failedLog, failureInjected, artifactTargets,
        localConflicts, abandoned, unrelatedRaced, generationResetSeen,
        preResetKnownKeys
       >>

ReconcileExternal ==
    /\ phase = "rejected"
    /\ phase' = "reconciled"
    /\ localConflicts' = {}
    /\ UNCHANGED <<
        networkStable, generation, graphRevision, parentRevision, topology,
        stageTopology, durableTopology, knownKeys, durableKnownKeys,
        nextSeq, log, preparedGeneration, preparedParentRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedGraphRevision, rejectedParentRevision, rejectedTopology,
        rejectedKnownKeys, rejectedNextSeq, rejectedLog, failedGeneration,
        failedGraphRevision, failedParentRevision, failedTopology,
        failedKnownKeys, failedNextSeq, failedLog, failureInjected,
        artifactTargets, abandoned, unrelatedRaced, generationResetSeen,
        preResetKnownKeys
       >>

AbandonResolved ==
    /\ phase \in {"rejected", "reconciled"}
    /\ localConflicts \cap Reachable(topology) = {}
    /\ phase' = "done"
    /\ abandoned' = TRUE
    /\ UNCHANGED <<
        networkStable, generation, graphRevision, parentRevision, topology,
        stageTopology, durableTopology, knownKeys, durableKnownKeys,
        nextSeq, log, preparedGeneration, preparedParentRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedGraphRevision, rejectedParentRevision, rejectedTopology,
        rejectedKnownKeys, rejectedNextSeq, rejectedLog, failedGeneration,
        failedGraphRevision, failedParentRevision, failedTopology,
        failedKnownKeys, failedNextSeq, failedLog, failureInjected,
        artifactTargets, localConflicts, unrelatedRaced,
        generationResetSeen, preResetKnownKeys
       >>

CommitRepaired ==
    /\ phase = "repaired"
    /\ PreparedIsCurrent
    /\ graphRevision < 4
    /\ parentRevision[preparedTarget] < 4
    /\ nextSeq <= MaxLog
    /\ phase' = "done"
    /\ LET newTopology == [topology EXCEPT ![preparedTarget] = preparedProposal]
           newParentRevision == [parentRevision EXCEPT ![preparedTarget] = @ + 1]
       IN AppendTopologyCommit(
            preparedTarget,
            newTopology,
            newParentRevision,
            knownKeys \cup preparedProposal
          )
    /\ UNCHANGED <<
        networkStable, preparedGeneration, preparedParentRevision,
        preparedTarget, preparedProposal, rejectedGeneration,
        rejectedGraphRevision, rejectedParentRevision, rejectedTopology,
        rejectedKnownKeys, rejectedNextSeq, rejectedLog, failedGeneration,
        failedGraphRevision, failedParentRevision, failedTopology,
        failedKnownKeys, failedNextSeq, failedLog, failureInjected,
        artifactTargets, localConflicts, abandoned, unrelatedRaced,
        generationResetSeen, preResetKnownKeys
       >>

Next ==
    \/ StabilizeNetwork
    \/ Prepare
    \/ ConcurrentOtherParent
    \/ ConcurrentSameParent
    \/ CompactToNewGeneration
    \/ CommitPrepared
    \/ PersistenceFailure
    \/ RetryAfterFailure
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
    /\ WF_vars(CommitPrepared \/ PersistenceFailure \/ RejectStale)
    /\ WF_vars(RetryAfterFailure)
    /\ WF_vars(Repair)
    /\ WF_vars(ReconcileExternal)
    /\ WF_vars(AbandonResolved)
    /\ WF_vars(CommitRepaired)

TypeOK ==
    /\ phase \in {"idle", "prepared", "retry", "rejected", "reconciled", "repaired", "done"}
    /\ networkStable \in BOOLEAN
    /\ generation \in 1..2
    /\ graphRevision \in 1..4
    /\ parentRevision \in ParentRevisionState
    /\ topology \in TopologyState
    /\ stageTopology \in TopologyState
    /\ durableTopology \in TopologyState
    /\ knownKeys \in SUBSET Layers
    /\ durableKnownKeys \in SUBSET Layers
    /\ nextSeq \in 1..(MaxLog + 1)
    /\ log \in Seq(LogRecord)
    /\ preparedGeneration \in 1..2
    /\ preparedParentRevision \in 1..4
    /\ preparedTarget \in Parents
    /\ preparedProposal \in SUBSET Layers
    /\ rejectedGeneration \in 1..2
    /\ rejectedGraphRevision \in 1..4
    /\ rejectedParentRevision \in ParentRevisionState
    /\ rejectedTopology \in TopologyState
    /\ rejectedKnownKeys \in SUBSET Layers
    /\ rejectedNextSeq \in 1..(MaxLog + 1)
    /\ rejectedLog \in Seq(LogRecord)
    /\ failedGeneration \in 1..2
    /\ failedGraphRevision \in 1..4
    /\ failedParentRevision \in ParentRevisionState
    /\ failedTopology \in TopologyState
    /\ failedKnownKeys \in SUBSET Layers
    /\ failedNextSeq \in 1..(MaxLog + 1)
    /\ failedLog \in Seq(LogRecord)
    /\ failureInjected \in BOOLEAN
    /\ artifactTargets \in SUBSET Layers
    /\ LayerA \in artifactTargets
    /\ localConflicts \in SUBSET artifactTargets
    /\ abandoned \in BOOLEAN
    /\ unrelatedRaced \in BOOLEAN
    /\ generationResetSeen \in BOOLEAN
    /\ preResetKnownKeys \in SUBSET Layers

RootIsAlwaysReachable == Root \in Reachable(topology)

StageGraphIdentityAndDurabilityAreAtomic ==
    /\ topology = stageTopology
    /\ stageTopology = durableTopology
    /\ knownKeys = durableKnownKeys

ReachableLayersHaveStableKeys == Reachable(topology) \subseteq knownKeys

DetachedIdentityIsRetained == LayerA \in knownKeys

LogSequenceIsContiguous ==
    /\ Len(log) = nextSeq - 1
    /\ \A index \in 1..Len(log): log[index].seq = index

LatestLogMatchesGraph ==
    Len(log) > 0 =>
        /\ log[Len(log)].generation = generation
        /\ log[Len(log)].graphRevision = graphRevision
        /\ log[Len(log)].topology = topology
        /\ log[Len(log)].knownKeys = knownKeys

UnrelatedParentCommitDoesNotStalePrepared ==
    phase = "prepared" /\ unrelatedRaced => PreparedIsCurrent

GenerationResetPreservesKnownKeys ==
    generationResetSeen => preResetKnownKeys \subseteq knownKeys

StaleRejectionDoesNotMutateState ==
    phase = "rejected" =>
        /\ generation = rejectedGeneration
        /\ graphRevision = rejectedGraphRevision
        /\ parentRevision = rejectedParentRevision
        /\ topology = rejectedTopology
        /\ stageTopology = rejectedTopology
        /\ durableTopology = rejectedTopology
        /\ knownKeys = rejectedKnownKeys
        /\ durableKnownKeys = rejectedKnownKeys
        /\ nextSeq = rejectedNextSeq
        /\ log = rejectedLog

PersistenceFailureDoesNotMutateState ==
    phase = "retry" =>
        /\ generation = failedGeneration
        /\ graphRevision = failedGraphRevision
        /\ parentRevision = failedParentRevision
        /\ topology = failedTopology
        /\ stageTopology = failedTopology
        /\ durableTopology = failedTopology
        /\ knownKeys = failedKnownKeys
        /\ durableKnownKeys = failedKnownKeys
        /\ nextSeq = failedNextSeq
        /\ log = failedLog

RepairedEditUsesCurrentParentIdentity ==
    phase = "repaired" => PreparedIsCurrent

AbandonmentRequiresNoLiveLocalConflict ==
    abandoned => localConflicts \cap Reachable(topology) = {}

EventuallyResolves == <> (phase = "done")

=============================================================================
