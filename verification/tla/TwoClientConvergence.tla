------------------------ MODULE TwoClientConvergence ------------------------
EXTENDS Integers, Sequences, TLC

(***************************************************************************
Two clients author conflicting values for one logical USD field.  The server
chooses one durable total order, live delivery may be interrupted, and a
reconnect replays every durable record after the client's applied cursor.

Flat and layered receivers both apply the same complete server order,
including the author's own committed record.  Receiver mode changes USD
projection, not membership in the durable commit stream.

Local authoring is split into prepare and publish steps.  A prepared edit is
retained while an earlier authoritative prefix applies, then published after
the receiver returns to idle.  This models the client freeze/apply/send ordering
and exposes loss caused by recomputing the local delta after apply.

Remote application is split into begin/mutate/notice/finish steps so the
feedback guard is observable.  A remote USD notice is consumed while
suppression is active and cannot create a producer submission.

This is intentionally a payload-semantic abstraction.  Each edit is a
last-writer-wins assignment to the same field, which is sufficient to expose
ordering mistakes without modeling the full Sdf data model.
***************************************************************************)

CONSTANTS ClientA, ClientB, NoEdit

Clients == {ClientA, ClientB}
Edits == 1..2
Values == {NoEdit} \cup Edits
Phases == {"idle", "mutate", "notice", "finish"}

Frame == [seq : 1..2, edit : Edits]

VARIABLES
    authored,             \* locally authored edits
    prepared,             \* frozen local edits not yet published
    submitted,            \* producer submissions (local causes only)
    wire,                 \* submitted edits awaiting server order
    serverLog,            \* durable global order, represented by edit IDs
    serverValue,
    connected,
    networkStable,        \* after this becomes true, disconnects stop
    queues,               \* per-client live/replay delivery queues
    cursor,               \* last successfully applied global sequence
    localValue,
    phase,
    applySeq,
    applyEdit,
    suppression,
    remoteNotices,
    handledRemoteNotices,
    localSubmissions,
    echoSubmissions

vars == <<
    authored, prepared, submitted, wire, serverLog, serverValue, connected,
    networkStable, queues, cursor, localValue, phase, applySeq, applyEdit,
    suppression, remoteNotices, handledRemoteNotices, localSubmissions,
    echoSubmissions
>>

RECURSIVE ReplayFrom(_, _, _)
ReplayFrom(log, after, index) ==
    IF index > Len(log)
    THEN <<>>
    ELSE IF index > after
         THEN <<[seq |-> index, edit |-> log[index]]>>
              \o ReplayFrom(log, after, index + 1)
         ELSE ReplayFrom(log, after, index + 1)

Init ==
    /\ ClientA # ClientB
    /\ NoEdit \notin Edits
    /\ authored = {}
    /\ prepared = {}
    /\ submitted = {}
    /\ wire = {}
    /\ serverLog = <<>>
    /\ serverValue = NoEdit
    /\ connected = [c \in Clients |-> FALSE]
    /\ networkStable = FALSE
    /\ queues = [c \in Clients |-> <<>>]
    /\ cursor = [c \in Clients |-> 0]
    /\ localValue = [c \in Clients |-> NoEdit]
    /\ phase = [c \in Clients |-> "idle"]
    /\ applySeq = [c \in Clients |-> 0]
    /\ applyEdit = [c \in Clients |-> NoEdit]
    /\ suppression = [c \in Clients |-> FALSE]
    /\ remoteNotices = [c \in Clients |-> 0]
    /\ handledRemoteNotices = [c \in Clients |-> 0]
    /\ localSubmissions = [c \in Clients |-> 0]
    /\ echoSubmissions = [c \in Clients |-> 0]

StabilizeNetwork ==
    /\ ~networkStable
    /\ networkStable' = TRUE
    /\ UNCHANGED <<
        authored, prepared, submitted, wire, serverLog, serverValue, connected,
        queues, cursor, localValue, phase, applySeq, applyEdit, suppression,
        remoteNotices, handledRemoteNotices, localSubmissions, echoSubmissions
       >>

PrepareLocal(c) ==
    /\ phase[c] = "idle"
    /\ LET edit == IF c = ClientA THEN 1 ELSE 2
       IN /\ edit \notin authored
          /\ authored' = authored \cup {edit}
          /\ prepared' = prepared \cup {edit}
          /\ localValue' = [localValue EXCEPT ![c] = edit]
    /\ UNCHANGED <<
        submitted, wire, serverLog, serverValue, connected, networkStable,
        queues, cursor, phase, applySeq, applyEdit, suppression, remoteNotices,
        handledRemoteNotices, localSubmissions, echoSubmissions
       >>

PublishPrepared(c) ==
    /\ phase[c] = "idle"
    /\ LET edit == IF c = ClientA THEN 1 ELSE 2
       IN /\ edit \in prepared
          /\ edit \notin submitted
          /\ prepared' = prepared \ {edit}
          /\ submitted' = submitted \cup {edit}
          /\ wire' = wire \cup {edit}
    /\ localSubmissions' = [localSubmissions EXCEPT ![c] = @ + 1]
    /\ UNCHANGED <<
        authored, serverLog, serverValue, connected, networkStable, queues,
        cursor, localValue, phase, applySeq, applyEdit, suppression,
        remoteNotices, handledRemoteNotices, echoSubmissions
       >>

Commit(edit) ==
    /\ edit \in wire
    /\ LET seq == Len(serverLog) + 1
           frame == [seq |-> seq, edit |-> edit]
       IN /\ serverLog' = Append(serverLog, edit)
          /\ serverValue' = edit
          /\ queues' = [c \in Clients |->
                IF connected[c]
                THEN Append(queues[c], frame)
                ELSE queues[c]]
    /\ wire' = wire \ {edit}
    /\ UNCHANGED <<
        authored, prepared, submitted, connected, networkStable, cursor, localValue,
        phase, applySeq, applyEdit, suppression, remoteNotices,
        handledRemoteNotices, localSubmissions, echoSubmissions
       >>

ServerCommit == \E edit \in Edits: Commit(edit)

Connect(c) ==
    /\ ~connected[c]
    /\ connected' = [connected EXCEPT ![c] = TRUE]
    /\ queues' = [queues EXCEPT ![c] = ReplayFrom(serverLog, cursor[c], 1)]
    /\ UNCHANGED <<
        authored, prepared, submitted, wire, serverLog, serverValue, networkStable,
        cursor, localValue, phase, applySeq, applyEdit, suppression,
        remoteNotices, handledRemoteNotices, localSubmissions, echoSubmissions
       >>

Disconnect(c) ==
    /\ connected[c]
    /\ ~networkStable
    /\ phase[c] = "idle"
    /\ connected' = [connected EXCEPT ![c] = FALSE]
    /\ queues' = [queues EXCEPT ![c] = <<>>]
    /\ UNCHANGED <<
        authored, prepared, submitted, wire, serverLog, serverValue, networkStable,
        cursor, localValue, phase, applySeq, applyEdit, suppression,
        remoteNotices, handledRemoteNotices, localSubmissions, echoSubmissions
       >>

BeginApply(c) ==
    /\ connected[c]
    /\ phase[c] = "idle"
    /\ Len(queues[c]) > 0
    /\ LET frame == Head(queues[c])
       IN /\ phase' = [phase EXCEPT ![c] = "mutate"]
          /\ applySeq' = [applySeq EXCEPT ![c] = frame.seq]
          /\ applyEdit' = [applyEdit EXCEPT ![c] = frame.edit]
    /\ queues' = [queues EXCEPT ![c] = Tail(@)]
    /\ suppression' = [suppression EXCEPT ![c] = TRUE]
    /\ UNCHANGED <<
        authored, prepared, submitted, wire, serverLog, serverValue, connected,
        networkStable, cursor, localValue, remoteNotices,
        handledRemoteNotices, localSubmissions, echoSubmissions
       >>

ApplyMutation(c) ==
    /\ phase[c] = "mutate"
    /\ localValue' = [localValue EXCEPT ![c] = applyEdit[c]]
    /\ cursor' = [cursor EXCEPT ![c] = applySeq[c]]
    /\ phase' = [phase EXCEPT ![c] = "notice"]
    /\ remoteNotices' = [remoteNotices EXCEPT ![c] = @ + 1]
    /\ UNCHANGED <<
        authored, prepared, submitted, wire, serverLog, serverValue, connected,
        networkStable, queues, applySeq, applyEdit, suppression,
        handledRemoteNotices, localSubmissions, echoSubmissions
       >>

HandleRemoteNotice(c) ==
    /\ phase[c] = "notice"
    /\ phase' = [phase EXCEPT ![c] = "finish"]
    /\ handledRemoteNotices' = [handledRemoteNotices EXCEPT
        ![c] = @ + IF suppression[c] THEN 1 ELSE 0]
    /\ echoSubmissions' = [echoSubmissions EXCEPT
        ![c] = @ + IF suppression[c] THEN 0 ELSE 1]
    /\ UNCHANGED <<
        authored, prepared, submitted, wire, serverLog, serverValue, connected,
        networkStable, queues, cursor, localValue, applySeq, applyEdit,
        suppression, remoteNotices, localSubmissions
       >>

FinishApply(c) ==
    /\ phase[c] = "finish"
    /\ phase' = [phase EXCEPT ![c] = "idle"]
    /\ applySeq' = [applySeq EXCEPT ![c] = 0]
    /\ applyEdit' = [applyEdit EXCEPT ![c] = NoEdit]
    /\ suppression' = [suppression EXCEPT ![c] = FALSE]
    /\ UNCHANGED <<
        authored, prepared, submitted, wire, serverLog, serverValue, connected,
        networkStable, queues, cursor, localValue, remoteNotices,
        handledRemoteNotices, localSubmissions, echoSubmissions
       >>

Next ==
    \/ StabilizeNetwork
    \/ (\E c \in Clients: PrepareLocal(c))
    \/ (\E c \in Clients: PublishPrepared(c))
    \/ ServerCommit
    \/ (\E c \in Clients: Connect(c))
    \/ (\E c \in Clients: Disconnect(c))
    \/ (\E c \in Clients: BeginApply(c))
    \/ (\E c \in Clients: ApplyMutation(c))
    \/ (\E c \in Clients: HandleRemoteNotice(c))
    \/ (\E c \in Clients: FinishApply(c))

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(StabilizeNetwork)
    /\ (\A c \in Clients: WF_vars(PrepareLocal(c)))
    /\ (\A c \in Clients: WF_vars(PublishPrepared(c)))
    /\ WF_vars(ServerCommit)
    /\ (\A c \in Clients: WF_vars(Connect(c)))
    /\ (\A c \in Clients: WF_vars(BeginApply(c)))
    /\ (\A c \in Clients: WF_vars(ApplyMutation(c)))
    /\ (\A c \in Clients: WF_vars(HandleRemoteNotice(c)))
    /\ (\A c \in Clients: WF_vars(FinishApply(c)))

TypeOK ==
    /\ authored \subseteq Edits
    /\ prepared \subseteq Edits
    /\ submitted \subseteq Edits
    /\ wire \subseteq Edits
    /\ serverLog \in Seq(Edits)
    /\ serverValue \in Values
    /\ connected \in [Clients -> BOOLEAN]
    /\ networkStable \in BOOLEAN
    /\ queues \in [Clients -> Seq(Frame)]
    /\ cursor \in [Clients -> 0..2]
    /\ localValue \in [Clients -> Values]
    /\ phase \in [Clients -> Phases]
    /\ applySeq \in [Clients -> 0..2]
    /\ applyEdit \in [Clients -> Values]
    /\ suppression \in [Clients -> BOOLEAN]
    /\ remoteNotices \in [Clients -> 0..2]
    /\ handledRemoteNotices \in [Clients -> 0..2]
    /\ localSubmissions \in [Clients -> 0..1]
    /\ echoSubmissions \in [Clients -> 0..2]

ServerLogIsAUniqueSubmittedPrefix ==
    /\ Len(serverLog) <= 2
    /\ \A index \in 1..Len(serverLog): serverLog[index] \in submitted
    /\ \A first, second \in 1..Len(serverLog):
        first # second => serverLog[first] # serverLog[second]
    /\ wire = submitted \ {serverLog[index] : index \in 1..Len(serverLog)}

ServerValueMatchesLog ==
    IF Len(serverLog) = 0
    THEN serverValue = NoEdit
    ELSE serverValue = serverLog[Len(serverLog)]

DeliveryStateIsSound ==
    /\ \A c \in Clients: cursor[c] <= Len(serverLog)
    /\ \A c \in Clients:
        \A index \in 1..Len(queues[c]):
            /\ queues[c][index].seq > cursor[c]
            /\ queues[c][index].seq <= Len(serverLog)
            /\ serverLog[queues[c][index].seq] = queues[c][index].edit
    /\ \A c \in Clients:
        \A first, second \in 1..Len(queues[c]):
            first < second => queues[c][first].seq < queues[c][second].seq
    /\ \A c \in Clients:
        phase[c] # "idle" =>
            /\ applySeq[c] \in 1..Len(serverLog)
            /\ serverLog[applySeq[c]] = applyEdit[c]

CompleteStreamHasNoGaps ==
    \A c \in Clients:
        connected[c] =>
            IF phase[c] = "mutate"
            THEN /\ applySeq[c] = cursor[c] + 1
                 /\ Len(queues[c]) = Len(serverLog) - cursor[c] - 1
                 /\ \A index \in 1..Len(queues[c]):
                        queues[c][index].seq = cursor[c] + index + 1
            ELSE /\ Len(queues[c]) = Len(serverLog) - cursor[c]
                 /\ \A index \in 1..Len(queues[c]):
                        queues[c][index].seq = cursor[c] + index
                 /\ (phase[c] \in {"notice", "finish"} =>
                        applySeq[c] = cursor[c])

RemoteApplyIsGuarded ==
    \A c \in Clients:
        /\ (phase[c] = "idle") = (~suppression[c])
        /\ handledRemoteNotices[c] + echoSubmissions[c] <= remoteNotices[c]
        /\ remoteNotices[c] <= handledRemoteNotices[c] + echoSubmissions[c] + 1

NoRemoteEchoSubmission ==
    /\ submitted \subseteq authored
    /\ \A c \in Clients:
        /\ echoSubmissions[c] = 0
        /\ localSubmissions[c] = IF (IF c = ClientA THEN 1 ELSE 2) \in submitted THEN 1 ELSE 0

PreparedLocalEditsAreRetained ==
    prepared = authored \ submitted

QuiescentConvergence ==
    (   /\ networkStable
        /\ authored = Edits
        /\ Len(serverLog) = 2
        /\ wire = {}
        /\ prepared = {}
        /\ \A c \in Clients:
            /\ connected[c]
            /\ Len(queues[c]) = 0
            /\ phase[c] = "idle"
    ) =>
        /\ \A c \in Clients: localValue[c] = serverValue
        /\ localValue[ClientA] = localValue[ClientB]

EventuallyConverged ==
    <> (
        /\ networkStable
        /\ authored = Edits
        /\ Len(serverLog) = 2
        /\ wire = {}
        /\ \A c \in Clients:
            /\ connected[c]
            /\ cursor[c] = 2
            /\ Len(queues[c]) = 0
            /\ phase[c] = "idle"
            /\ localValue[c] = serverValue
        /\ \A c \in Clients: echoSubmissions[c] = 0
    )

=============================================================================
