#ifdef NDEBUG
#undef NDEBUG
#endif

#include "openusdconnect/client/replay_identity.h"

#include <cassert>

using namespace openusdconnect::client;

int main()
{
	ReceiverReplayIdentity initial_replay;
	assert(!initial_replay.BeginConnection());
	initial_replay.AcceptHello(1, true, "server-a", 0);
	assert(initial_replay.IsConnectionPrefixProven());
	initial_replay.AcceptReplayComplete(0);
	assert((initial_replay.Pending() == ReplayIdentity{"server-a", 0}));
	initial_replay.MarkReplayApplied();
	assert((initial_replay.Applied() == ReplayIdentity{"server-a", 0}));

	const ReplayPrefixClaim matching_claim = initial_replay.BeginConnection();
	assert(matching_claim);
	assert(matching_claim->IsKnown());
	assert(matching_claim->ServerInstance() == "server-a");
	assert(matching_claim->Epoch() == 0);
	initial_replay.AcceptHello(2, true, "server-a", 0);
	assert(initial_replay.IsConnectionPrefixProven());
	initial_replay.AcceptReplayComplete(0);
	initial_replay.MarkReplayApplied();
	assert((initial_replay.Applied() == ReplayIdentity{"server-a", 0}));

	const ReplayPrefixClaim stale_claim = initial_replay.BeginConnection();
	assert(stale_claim && stale_claim->Epoch() == 0);
	initial_replay.AcceptHello(2, true, "server-b", 1);
	assert(!initial_replay.IsConnectionPrefixProven());
	initial_replay.AcceptReplayComplete(1);
	assert(!initial_replay.Pending());
	initial_replay.AcceptResync();
	assert(initial_replay.IsConnectionPrefixProven());
	initial_replay.AcceptReplayComplete(1);
	assert((initial_replay.Pending() == ReplayIdentity{"server-b", 1}));
	initial_replay.MarkReplayApplied();
	assert((initial_replay.Applied() == ReplayIdentity{"server-b", 1}));

	ReceiverReplayIdentity external_prefix;
	assert(!external_prefix.BeginConnection());
	external_prefix.AcceptHello(4, true, "server-a", 0);
	assert(!external_prefix.IsConnectionPrefixProven());
	external_prefix.AcceptReplayComplete(0);
	external_prefix.MarkReplayApplied();
	assert(!external_prefix.Applied());
	const ReplayPrefixClaim unknown_claim = external_prefix.BeginConnection();
	assert(unknown_claim);
	assert(!unknown_claim->IsKnown());
	assert(unknown_claim->ServerInstance().empty());
	assert(!unknown_claim->Epoch());

	ReceiverReplayIdentity legacy_server;
	assert(!legacy_server.BeginConnection());
	legacy_server.AcceptHello(1, false, {}, std::nullopt);
	legacy_server.AcceptReplayComplete(0);
	legacy_server.MarkReplayApplied();
	assert(!legacy_server.Applied());
	return 0;
}
