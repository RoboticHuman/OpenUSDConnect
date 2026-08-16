--------------------- MODULE SharedLayerRestartRecovery ---------------------
EXTENDS Integers, Sequences, TLC

(***************************************************************************
Crash/restart model for the durable shared-layer graph checkpoint. Volatile
state is deliberately discarded on crash and must be reconstructed from the
same durable generation, revision, topology, key set, sequence, and log.
***************************************************************************)

CONSTANTS Root, LayerA, LayerB

Layers == {Root, LayerA, LayerB}
TopologyType == SUBSET Layers
Record == [
    seq : 1..3,
    generation : 1..2,
    revision : 1..3,
    topology : TopologyType,
    knownKeys : SUBSET Layers
]

Baseline == [
    seq |-> 1,
    generation |-> 1,
    revision |-> 1,
    topology |-> {LayerA},
    knownKeys |-> {Root, LayerA}
]

VARIABLES
    serverUp,
    restartSeen,
    generation,
    revision,
    topology,
    knownKeys,
    nextSeq,
    log,
    durableGeneration,
    durableRevision,
    durableTopology,
    durableKnownKeys,
    durableNextSeq,
    durableLog

vars == <<
    serverUp, restartSeen, generation, revision, topology, knownKeys,
    nextSeq, log, durableGeneration, durableRevision, durableTopology,
    durableKnownKeys, durableNextSeq, durableLog
>>

Init ==
    /\ Root # LayerA /\ Root # LayerB /\ LayerA # LayerB
    /\ serverUp = TRUE
    /\ restartSeen = FALSE
    /\ generation = 1
    /\ revision = 1
    /\ topology = {LayerA}
    /\ knownKeys = {Root, LayerA}
    /\ nextSeq = 2
    /\ log = <<Baseline>>
    /\ durableGeneration = generation
    /\ durableRevision = revision
    /\ durableTopology = topology
    /\ durableKnownKeys = knownKeys
    /\ durableNextSeq = nextSeq
    /\ durableLog = log

CommitTopology(newTopology, newKnownKeys) ==
    /\ generation' = generation
    /\ revision' = revision + 1
    /\ topology' = newTopology
    /\ knownKeys' = newKnownKeys
    /\ log' = Append(log, [
        seq |-> nextSeq,
        generation |-> generation,
        revision |-> revision + 1,
        topology |-> newTopology,
        knownKeys |-> newKnownKeys
       ])
    /\ nextSeq' = nextSeq + 1
    /\ durableGeneration' = generation
    /\ durableRevision' = revision'
    /\ durableTopology' = topology'
    /\ durableKnownKeys' = knownKeys'
    /\ durableNextSeq' = nextSeq'
    /\ durableLog' = log'

Attach ==
    /\ serverUp
    /\ ~restartSeen
    /\ LayerB \notin topology
    /\ revision < 3
    /\ nextSeq <= 3
    /\ CommitTopology(topology \union {LayerB}, knownKeys \union {LayerB})
    /\ UNCHANGED <<serverUp, restartSeen>>

Detach ==
    /\ serverUp
    /\ ~restartSeen
    /\ LayerA \in topology
    /\ revision < 3
    /\ nextSeq <= 3
    /\ CommitTopology(topology \ {LayerA}, knownKeys)
    /\ UNCHANGED <<serverUp, restartSeen>>

Compact ==
    /\ serverUp
    /\ ~restartSeen
    /\ generation = 1
    /\ generation' = 2
    /\ revision' = 1
    /\ nextSeq' = 2
    /\ log' = <<[
        seq |-> 1,
        generation |-> 2,
        revision |-> 1,
        topology |-> topology,
        knownKeys |-> knownKeys
       ]>>
    /\ durableGeneration' = generation'
    /\ durableRevision' = revision'
    /\ durableTopology' = topology
    /\ durableKnownKeys' = knownKeys
    /\ durableNextSeq' = nextSeq'
    /\ durableLog' = log'
    /\ UNCHANGED <<serverUp, restartSeen, topology, knownKeys>>

Crash ==
    /\ serverUp
    /\ ~restartSeen
    /\ serverUp' = FALSE
    /\ generation' = 1
    /\ revision' = 1
    /\ topology' = {}
    /\ knownKeys' = {Root}
    /\ nextSeq' = 1
    /\ log' = <<>>
    /\ UNCHANGED <<
        restartSeen, durableGeneration, durableRevision, durableTopology,
        durableKnownKeys, durableNextSeq, durableLog
       >>

Restart ==
    /\ ~serverUp
    /\ serverUp' = TRUE
    /\ restartSeen' = TRUE
    /\ generation' = durableGeneration
    /\ revision' = durableRevision
    /\ topology' = durableTopology
    /\ knownKeys' = durableKnownKeys
    /\ nextSeq' = durableNextSeq
    /\ log' = durableLog
    /\ UNCHANGED <<
        durableGeneration, durableRevision, durableTopology,
        durableKnownKeys, durableNextSeq, durableLog
       >>

Next == Attach \/ Detach \/ Compact \/ Crash \/ Restart

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(Crash)
    /\ WF_vars(Restart)

TypeOK ==
    /\ serverUp \in BOOLEAN
    /\ restartSeen \in BOOLEAN
    /\ generation \in 1..2
    /\ revision \in 1..3
    /\ topology \in TopologyType
    /\ knownKeys \subseteq Layers
    /\ nextSeq \in 1..4
    /\ log \in Seq(Record)
    /\ durableGeneration \in 1..2
    /\ durableRevision \in 1..3
    /\ durableTopology \in TopologyType
    /\ durableKnownKeys \subseteq Layers
    /\ durableNextSeq \in 2..4
    /\ durableLog \in Seq(Record)

DurableCheckpointMatchesLog ==
    /\ Len(durableLog) > 0
    /\ durableNextSeq = durableLog[Len(durableLog)].seq + 1
    /\ durableGeneration = durableLog[Len(durableLog)].generation
    /\ durableRevision = durableLog[Len(durableLog)].revision
    /\ durableTopology = durableLog[Len(durableLog)].topology
    /\ durableKnownKeys = durableLog[Len(durableLog)].knownKeys

KnownKeysSurviveDetach ==
    {Root} \union durableTopology \subseteq durableKnownKeys

RunningStateMatchesDurableCheckpoint ==
    serverUp =>
        /\ generation = durableGeneration
        /\ revision = durableRevision
        /\ topology = durableTopology
        /\ knownKeys = durableKnownKeys
        /\ nextSeq = durableNextSeq
        /\ log = durableLog

RestartRestoresCheckpoint == restartSeen => serverUp

EventuallyRestarts == <>restartSeen

=============================================================================
