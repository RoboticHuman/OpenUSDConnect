#include "openusdconnect/client/replay_identity.h"
#include "openusdconnect/client/receiver_session.h"

#include "test_check.h"

using namespace openusdconnect::client;

static void TestQueuedResync(bool empty_replay)
{
	OrderedReceiverSession<int> session(1, 8, true);
	ReceiverReplayIdentity identity;
	const auto connection = session.BeginConnection();
	CHECK(!identity.BeginConnection());
	identity.AcceptHello(1, true, "server", 0);
	CHECK(session.Accept(connection.Generation, ReceiverMessageKind::Resync, 0, 0) ==
		  AcceptResult::Accepted);
	identity.AcceptResync();
	if (!empty_replay)
	{
		CHECK(session.Accept(connection.Generation, ReceiverMessageKind::Event, 1, 1) ==
			  AcceptResult::Accepted);
	}
	const int head = empty_replay ? 0 : 1;
	CHECK(session.AcceptReplayComplete(connection.Generation, head, 1) == AcceptResult::Accepted);
	identity.AcceptReplayComplete(1);
	CHECK(!session.TryMarkReplayApplied());
	int frame = -1;
	CHECK(session.TryPop(frame) && frame == 0);
	session.ResetAppliedProgress();
	CHECK(!session.Synchronized());
	if (!empty_replay)
	{
		CHECK(!session.TryMarkReplayApplied());
		CHECK(session.TryPop(frame) && frame == 1);
		CHECK(!session.TryMarkReplayApplied());
		CHECK(session.MarkAppliedThrough(connection.Generation, 1));
	}
	CHECK(session.TryMarkReplayApplied());
	identity.MarkReplayApplied();
	CHECK(session.Synchronized());
	CHECK(session.ReplayEpoch() == 1);
	CHECK((identity.Applied() == ReplayIdentity{"server", 1}));
}

enum class Interruption
{
	Disconnect,
	FailureAfterReset,
	FailureOnReset,
};

static void TestInterruptedReplay(Interruption interruption)
{
	OrderedReceiverSession<int> session(1, 8, true);
	ReceiverReplayIdentity identity;
	auto connection = session.BeginConnection();
	CHECK(!identity.BeginConnection());
	identity.AcceptHello(1, true, "server", 0);
	CHECK(session.Accept(connection.Generation, ReceiverMessageKind::Resync, 0, 0) ==
		  AcceptResult::Accepted);
	identity.AcceptResync();
	CHECK(session.Accept(connection.Generation, ReceiverMessageKind::Event, 1, 1) ==
		  AcceptResult::Accepted);
	CHECK(session.AcceptReplayComplete(connection.Generation, 1, 1) == AcceptResult::Accepted);
	identity.AcceptReplayComplete(1);
	const bool apply_failure = interruption != Interruption::Disconnect;
	if (apply_failure)
	{
		int frame;
		CHECK(session.TryPop(frame) && frame == 0);
		if (interruption == Interruption::FailureAfterReset)
		{
			session.ResetAppliedProgress();
			CHECK(session.TryPop(frame) && frame == 1);
		}
		// Applying the drained frame failed; never advance the applied cursor.
		CHECK(!session.TryMarkReplayApplied());
		CHECK(session.ResetPending() == (interruption == Interruption::FailureOnReset));
		const std::int32_t replay_from = session.LastAppliedSequence() + 1;
		identity.RequestReplayFrom(replay_from, session.ResetPending());
		CHECK(session.RequestReplayFrom(replay_from));
		CHECK(!session.ResetPending());
	}
	session.Disconnect(connection.Generation);
	CHECK(!session.TryMarkReplayApplied());
	CHECK(!identity.Applied());
	connection = session.BeginConnection();
	const auto claim = identity.BeginConnection();
	// The applied reset keeps the received identity; an unapplied one leaves
	// only the applied prefix, which has no identity yet.
	CHECK(claim);
	if (interruption == Interruption::FailureOnReset)
	{
		CHECK(!claim->IsKnown());
	}
	else
	{
		CHECK(claim->ServerInstance() == "server" && claim->Epoch() == 1);
	}
	CHECK(identity.ResetRequired() == apply_failure);
	CHECK(!identity.Pending());
	identity.AcceptHello(connection.SyncFrom, true, "server", 1);
	CHECK(identity.IsConnectionPrefixProven());
	if (apply_failure)
	{
		// The receiver queues the reset for a full replay itself.
		CHECK(session.Accept(connection.Generation, ReceiverMessageKind::Resync, 0, 0) ==
			  AcceptResult::Accepted);
		identity.AcceptResync();
		CHECK(!identity.ResetRequired());
		CHECK(session.Accept(connection.Generation, ReceiverMessageKind::Event, 1, 1) ==
			  AcceptResult::Accepted);
	}
	CHECK(session.AcceptReplayComplete(connection.Generation, 1, 1) == AcceptResult::Accepted);
	identity.AcceptReplayComplete(1);
	int frame;
	while (session.TryPop(frame))
	{
		if (frame == 0)
			session.ResetAppliedProgress();
		else
			CHECK(session.MarkAppliedThrough(connection.Generation, frame));
	}
	CHECK(session.TryMarkReplayApplied());
	identity.MarkReplayApplied();
	CHECK((identity.Applied() == ReplayIdentity{"server", 1}));
}

int main()
{
	TestQueuedResync(false);
	TestQueuedResync(true);
	TestInterruptedReplay(Interruption::Disconnect);
	TestInterruptedReplay(Interruption::FailureAfterReset);
	TestInterruptedReplay(Interruption::FailureOnReset);
	ReceiverReplayIdentity initial_replay;
	CHECK(!initial_replay.BeginConnection());
	initial_replay.AcceptHello(1, true, "server-a", 0);
	CHECK(initial_replay.IsConnectionPrefixProven());
	initial_replay.AcceptReplayComplete(0);
	CHECK((initial_replay.Pending() == ReplayIdentity{"server-a", 0}));
	initial_replay.MarkReplayApplied();
	CHECK((initial_replay.Applied() == ReplayIdentity{"server-a", 0}));

	const ReplayPrefixClaim matching_claim = initial_replay.BeginConnection();
	CHECK(matching_claim);
	CHECK(matching_claim->IsKnown());
	CHECK(matching_claim->ServerInstance() == "server-a");
	CHECK(matching_claim->Epoch() == 0);
	initial_replay.AcceptHello(2, true, "server-a", 0);
	CHECK(initial_replay.IsConnectionPrefixProven());
	initial_replay.AcceptReplayComplete(0);
	initial_replay.MarkReplayApplied();
	CHECK((initial_replay.Applied() == ReplayIdentity{"server-a", 0}));

	const ReplayPrefixClaim stale_claim = initial_replay.BeginConnection();
	CHECK(stale_claim && stale_claim->Epoch() == 0);
	initial_replay.AcceptHello(2, true, "server-b", 1);
	CHECK(!initial_replay.IsConnectionPrefixProven());
	initial_replay.AcceptReplayComplete(1);
	CHECK(!initial_replay.Pending());
	initial_replay.AcceptResync();
	CHECK(initial_replay.IsConnectionPrefixProven());
	initial_replay.AcceptReplayComplete(1);
	CHECK((initial_replay.Pending() == ReplayIdentity{"server-b", 1}));
	initial_replay.MarkReplayApplied();
	CHECK((initial_replay.Applied() == ReplayIdentity{"server-b", 1}));

	ReceiverReplayIdentity external_prefix;
	CHECK(!external_prefix.BeginConnection());
	external_prefix.AcceptHello(4, true, "server-a", 0);
	CHECK(!external_prefix.IsConnectionPrefixProven());
	external_prefix.AcceptReplayComplete(0);
	external_prefix.MarkReplayApplied();
	CHECK(!external_prefix.Applied());
	const ReplayPrefixClaim unknown_claim = external_prefix.BeginConnection();
	CHECK(unknown_claim);
	CHECK(!unknown_claim->IsKnown());
	CHECK(unknown_claim->ServerInstance().empty());
	CHECK(!unknown_claim->Epoch());

	ReceiverReplayIdentity legacy_server;
	CHECK(!legacy_server.BeginConnection());
	legacy_server.AcceptHello(1, false, {}, std::nullopt);
	legacy_server.AcceptReplayComplete(0);
	legacy_server.MarkReplayApplied();
	CHECK(!legacy_server.Applied());
	return 0;
}
