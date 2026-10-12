------------------------- MODULE ProducerConnection -------------------------
EXTENDS Integers, Sequences, TLC

(***************************************************************************
Bounded model of the producer endpoint's connection attempts, the host loop
that applies its actions, and one server.

The host's application thread starts attempts, submits, cancels, and
disconnects at any time. Its I/O loop takes the queued actions as a batch,
blocks while a connect is pending, and reads only once a batch is applied,
so it can report a socket's end before it applies a close that the endpoint
queued for that socket. Applying a close always reports, even without a
socket. Endpoint transitions are atomic, as under the endpoint's lock.
Backoff and rate limits only delay attempts and are not modeled.

The server is honest, except that when ServerMayDiverge holds its durable
progress for this session may change once while no socket is open: it loses
its newest transaction, or another producer with the same session id commits
past this client's outbox. Rejections and repair are covered by
TransactionRecovery.tla.
***************************************************************************)

CONSTANTS MaxTxn, MaxFaults, ServerMayDiverge

States == {"idle", "connecting", "handshaking", "connected", "closing"}
Phases == {"disconnected", "awaiting", "ready", "recovery"}
Failures == {"none", "ahead", "regressed", "invalid"}
TxnIds == 1..MaxTxn

Hello == [type |-> "hello"]
Quit == [type |-> "quit"]
Txn(id) == [type |-> "txn", id |-> id]
ClientFrames == {Hello, Quit} \cup [type : {"txn"}, id : TxnIds]
ServerFrames ==
    [type : {"hellook"}, high : 0..MaxTxn] \cup [type : {"ack"}, id : 0..MaxTxn]

ConnectAct == [kind |-> "connect"]
CloseAct == [kind |-> "close"]
Send(frame) == [kind |-> "send", frame |-> frame]
Actions == {ConnectAct, CloseAct} \cup [kind : {"send"}, frame : ClientFrames]

VARIABLES
    state,          \* endpoint connection state
    phase,          \* producer session phase
    generation,     \* session generation counter
    connGen,        \* session generation of the attempt or connection in flight
    failure,
    nextId,
    acked,
    cancelled,      \* generations whose handshake the host abandoned
    queue,          \* actions the endpoint queued that the host has not taken
    batch,          \* actions the host took and applies in order
    socket,
    doubleConnect,  \* the host was asked to open a second socket
    toServer,       \* frames on the open socket from the client
    toClient,       \* frames on the open socket from the server
    serverHigh,
    diverged,
    gap,            \* the server received a transaction past its next one
    faults

endpointVars == <<state, phase, generation, connGen, failure, nextId, acked, cancelled>>
hostVars == <<queue, batch, socket, doubleConnect>>
wireVars == <<toServer, toClient>>
serverVars == <<serverHigh, diverged, gap>>
vars == <<endpointVars, hostVars, wireVars, serverVars, faults>>

RECURSIVE Replay(_, _)
Replay(first, last) ==
    IF first > last THEN <<>> ELSE <<Send(Txn(first))>> \o Replay(first + 1, last)

IsConnectAct(action) == action.kind = "connect"
NotConnect(action) == ~IsConnectAct(action)
NotForTheSocket(action) == action.kind \notin {"close", "send"}

\* ProducerSession::Disconnect: a failure outlives its connection.
EndSession == phase' = IF phase = "recovery" THEN "recovery" ELSE "disconnected"

Init ==
    /\ state = "idle"
    /\ phase = "disconnected"
    /\ generation = 0
    /\ connGen = 0
    /\ failure = "none"
    /\ nextId = 1
    /\ acked = 0
    /\ cancelled = {}
    /\ queue = <<>>
    /\ batch = <<>>
    /\ socket = "none"
    /\ doubleConnect = FALSE
    /\ toServer = <<>>
    /\ toClient = <<>>
    /\ serverHigh = 0
    /\ diverged = FALSE
    /\ gap = FALSE
    /\ faults = 0

---------------------------------------------------------------------------
\* Endpoint reactions to host reports.

\* OnDisconnected: the socket is gone, so its queued frames and close are void.
OnDisconnected ==
    IF state \in {"connecting", "handshaking", "connected", "closing"}
    THEN /\ state' = "idle"
         /\ IF state \in {"handshaking", "connected"} THEN EndSession ELSE UNCHANGED phase
         /\ queue' = SelectSeq(queue, NotForTheSocket)
    ELSE UNCHANGED <<state, phase, queue>>

OnConnected ==
    IF state = "connecting"
    THEN /\ generation' = generation + 1
         /\ connGen' = generation + 1
         /\ phase' = "awaiting"
         /\ state' = "handshaking"
         /\ queue' = Append(queue, Send(Hello))
    ELSE UNCHANGED <<generation, connGen, phase, state, queue>>

Fail(kind) ==
    /\ failure' = kind
    /\ phase' = "recovery"
    /\ state' = "closing"
    /\ queue' = Append(queue, CloseAct)
    /\ UNCHANGED acked

\* A Hello for an ended generation would quarantine a healthy session.
AcceptHello(high) ==
    IF connGen # generation \/ phase # "awaiting" THEN Fail("invalid")
    ELSE IF high > nextId - 1 THEN Fail("ahead")
    ELSE IF high < acked THEN Fail("regressed")
    ELSE /\ acked' = high
         /\ phase' = "ready"
         /\ state' = "connected"
         /\ queue' = queue \o Replay(high + 1, nextId - 1)
         /\ UNCHANGED failure

Acknowledge(id) ==
    IF id > nextId - 1 THEN Fail("ahead")
    ELSE IF id < acked THEN Fail("regressed")
    ELSE /\ acked' = id
         /\ UNCHANGED <<failure, phase, state, queue>>

OnFrame(frame) ==
    IF state = "handshaking" /\ frame.type = "hellook" THEN AcceptHello(frame.high)
    ELSE IF state = "connected" /\ frame.type = "ack" THEN Acknowledge(frame.id)
    ELSE UNCHANGED <<failure, phase, state, queue, acked>>

---------------------------------------------------------------------------
\* The host's application thread.

\* RequestConnect or Connect.
StartAttempt ==
    /\ state = "idle"
    /\ failure = "none"
    /\ state' = "connecting"
    /\ queue' = Append(queue, ConnectAct)
    /\ UNCHANGED <<phase, generation, connGen, failure, nextId, acked, cancelled>>
    /\ UNCHANGED <<batch, socket, doubleConnect, wireVars, serverVars, faults>>

\* CancelConnect, Disconnect, Stop, or the handshake deadline. An attempt the
\* host has not taken is withdrawn; any other is closed.
AbandonAttempt ==
    /\ faults < MaxFaults
    /\ state \in {"connecting", "handshaking"}
    /\ faults' = faults + 1
    /\ IF state = "connecting" /\ \E index \in 1..Len(queue) : IsConnectAct(queue[index])
       THEN /\ state' = "idle"
            /\ queue' = SelectSeq(queue, NotConnect)
       ELSE /\ state' = "closing"
            /\ queue' = Append(queue, CloseAct)
    /\ IF state = "handshaking"
       THEN /\ EndSession
            /\ cancelled' = cancelled \cup {connGen}
       ELSE UNCHANGED <<phase, cancelled>>
    /\ UNCHANGED <<generation, connGen, failure, nextId, acked>>
    /\ UNCHANGED <<batch, socket, doubleConnect, wireVars, serverVars>>

DisconnectPublished ==
    /\ faults < MaxFaults
    /\ state = "connected"
    /\ faults' = faults + 1
    /\ state' = "closing"
    /\ EndSession
    /\ queue' = queue \o <<Send(Quit), CloseAct>>
    /\ UNCHANGED <<generation, connGen, failure, nextId, acked, cancelled>>
    /\ UNCHANGED <<batch, socket, doubleConnect, wireVars, serverVars>>

Submit ==
    /\ state = "connected"
    /\ nextId <= MaxTxn
    /\ nextId' = nextId + 1
    /\ queue' = Append(queue, Send(Txn(nextId)))
    /\ UNCHANGED <<state, phase, generation, connGen, failure, acked, cancelled>>
    /\ UNCHANGED <<batch, socket, doubleConnect, wireVars, serverVars, faults>>

---------------------------------------------------------------------------
\* The host's I/O loop.

Take ==
    /\ batch = <<>>
    /\ queue # <<>>
    /\ socket # "connecting"
    /\ batch' = queue
    /\ queue' = <<>>
    /\ UNCHANGED <<endpointVars, socket, doubleConnect, wireVars, serverVars, faults>>

ApplyConnect ==
    /\ batch # <<>>
    /\ socket # "connecting"
    /\ IsConnectAct(Head(batch))
    /\ batch' = Tail(batch)
    /\ doubleConnect' = (doubleConnect \/ socket # "none")
    /\ socket' = "connecting"
    /\ UNCHANGED <<endpointVars, queue, wireVars, serverVars, faults>>

ApplySend ==
    /\ batch # <<>>
    /\ socket # "connecting"
    /\ Head(batch).kind = "send"
    /\ batch' = Tail(batch)
    /\ toServer' = IF socket = "open" THEN Append(toServer, Head(batch).frame) ELSE toServer
    /\ UNCHANGED <<endpointVars, queue, socket, doubleConnect, toClient, serverVars, faults>>

ApplyClose ==
    /\ batch # <<>>
    /\ socket # "connecting"
    /\ Head(batch).kind = "close"
    /\ batch' = Tail(batch)
    /\ socket' = "none"
    /\ toServer' = <<>>
    /\ toClient' = <<>>
    /\ OnDisconnected
    /\ UNCHANGED <<generation, connGen, failure, nextId, acked, cancelled>>
    /\ UNCHANGED <<doubleConnect, serverVars, faults>>

ConnectSucceeds ==
    /\ socket = "connecting"
    /\ socket' = "open"
    /\ OnConnected
    /\ UNCHANGED <<failure, nextId, acked, cancelled>>
    /\ UNCHANGED <<batch, doubleConnect, wireVars, serverVars, faults>>

\* The host interrupts the pending connect of an abandoned attempt.
ConnectInterrupted ==
    /\ socket = "connecting"
    /\ state = "closing"
    /\ socket' = "none"
    /\ OnDisconnected
    /\ UNCHANGED <<generation, connGen, failure, nextId, acked, cancelled>>
    /\ UNCHANGED <<batch, doubleConnect, wireVars, serverVars, faults>>

ConnectFails ==
    /\ faults < MaxFaults
    /\ socket = "connecting"
    /\ faults' = faults + 1
    /\ socket' = "none"
    /\ OnDisconnected
    /\ UNCHANGED <<generation, connGen, failure, nextId, acked, cancelled>>
    /\ UNCHANGED <<batch, doubleConnect, wireVars, serverVars>>

Deliver ==
    /\ batch = <<>>
    /\ socket = "open"
    /\ toClient # <<>>
    /\ toClient' = Tail(toClient)
    /\ OnFrame(Head(toClient))
    /\ UNCHANGED <<generation, connGen, nextId, cancelled>>
    /\ UNCHANGED <<batch, socket, doubleConnect, toServer, serverVars, faults>>

PeerCloses ==
    /\ faults < MaxFaults
    /\ batch = <<>>
    /\ socket = "open"
    /\ faults' = faults + 1
    /\ socket' = "none"
    /\ toServer' = <<>>
    /\ toClient' = <<>>
    /\ OnDisconnected
    /\ UNCHANGED <<generation, connGen, failure, nextId, acked, cancelled>>
    /\ UNCHANGED <<batch, doubleConnect, serverVars>>

---------------------------------------------------------------------------
\* The server.

ServerStep ==
    /\ socket = "open"
    /\ toServer # <<>>
    /\ toServer' = Tail(toServer)
    /\ LET frame == Head(toServer)
       IN CASE frame.type = "hello" ->
                /\ toClient' = Append(toClient, [type |-> "hellook", high |-> serverHigh])
                /\ UNCHANGED <<serverHigh, gap>>
            [] frame.type = "txn" /\ frame.id = serverHigh + 1 ->
                /\ serverHigh' = frame.id
                /\ toClient' = Append(toClient, [type |-> "ack", id |-> frame.id])
                /\ UNCHANGED gap
            [] frame.type = "txn" /\ frame.id <= serverHigh ->
                /\ toClient' = Append(toClient, [type |-> "ack", id |-> serverHigh])
                /\ UNCHANGED <<serverHigh, gap>>
            [] frame.type = "txn" ->
                /\ gap' = TRUE
                /\ UNCHANGED <<toClient, serverHigh>>
            [] OTHER ->
                UNCHANGED <<toClient, serverHigh, gap>>
    /\ UNCHANGED <<endpointVars, hostVars, diverged, faults>>

ServerLosesProgress ==
    /\ ServerMayDiverge
    /\ ~diverged
    /\ socket = "none"
    /\ serverHigh > 0
    /\ serverHigh' = serverHigh - 1
    /\ diverged' = TRUE
    /\ UNCHANGED <<endpointVars, hostVars, wireVars, gap, faults>>

ServerRunsAhead ==
    /\ ServerMayDiverge
    /\ ~diverged
    /\ socket = "none"
    /\ nextId <= MaxTxn
    /\ serverHigh' = nextId
    /\ diverged' = TRUE
    /\ UNCHANGED <<endpointVars, hostVars, wireVars, gap, faults>>

---------------------------------------------------------------------------

Next ==
    \/ StartAttempt
    \/ AbandonAttempt
    \/ DisconnectPublished
    \/ Submit
    \/ Take
    \/ ApplyConnect
    \/ ApplySend
    \/ ApplyClose
    \/ ConnectSucceeds
    \/ ConnectInterrupted
    \/ ConnectFails
    \/ Deliver
    \/ PeerCloses
    \/ ServerStep
    \/ ServerLosesProgress
    \/ ServerRunsAhead

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(StartAttempt)
    /\ WF_vars(Submit)
    /\ WF_vars(Take)
    /\ WF_vars(ApplyConnect)
    /\ WF_vars(ApplySend)
    /\ WF_vars(ApplyClose)
    /\ WF_vars(ConnectSucceeds)
    /\ WF_vars(ConnectInterrupted)
    /\ WF_vars(Deliver)
    /\ WF_vars(ServerStep)

TypeOK ==
    /\ state \in States
    /\ phase \in Phases
    /\ generation \in Nat
    /\ connGen \in 0..generation
    /\ failure \in Failures
    /\ nextId \in 1..(MaxTxn + 1)
    /\ acked \in 0..MaxTxn
    /\ cancelled \subseteq 1..generation
    /\ queue \in Seq(Actions)
    /\ batch \in Seq(Actions)
    /\ socket \in {"none", "connecting", "open"}
    /\ doubleConnect \in BOOLEAN
    /\ toServer \in Seq(ClientFrames)
    /\ toClient \in Seq(ServerFrames)
    /\ serverHigh \in 0..MaxTxn
    /\ diverged \in BOOLEAN
    /\ gap \in BOOLEAN
    /\ faults \in 0..MaxFaults

OneHostSocket == ~doubleConnect

\* Every report the endpoint acts on belongs to the socket it tracks.
NoForgottenSocket == state = "idle" => socket = "none"

ReadyExactlyWhileConnected == (phase = "ready") <=> (state = "connected")

FailureExactlyWhileRecovering == (failure # "none") <=> (phase = "recovery")

AttemptsStartHealthy == state \in {"connecting", "handshaking"} => failure = "none"

CancelledHandshakeNeverPublishes == state = "connected" => connGen \notin cancelled

HighwaterFailsOnlyAfterDivergence ==
    failure \in IF diverged THEN {"none", "ahead", "regressed"} ELSE {"none"}

AcknowledgesOnlySubmitted == acked < nextId

AcknowledgedIsDurable == ~diverged => acked <= serverHigh

ReplayNeverSkips == ~gap

OrderedWire ==
    \A first, second \in 1..Len(toServer) :
        (first < second /\ toServer[first].type = "txn" /\ toServer[second].type = "txn")
            => toServer[first].id < toServer[second].id

AcknowledgementNeverRegresses == [][acked' >= acked]_acked

EventuallyComplete == <>(acked = MaxTxn)

=============================================================================
