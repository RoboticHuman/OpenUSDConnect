#include "openusdconnect/client/engine/status.h"
#include "openusdconnect/client/producer_recovery.h"
#include "openusdconnect/client/producer_session.h"
#include "openusdconnect/client/receiver_session.h"
#include "openusdconnect/client/schema/messages_generated.h"

#include "test_check.h"

#include <cstddef>
#include <cstdint>
#include <iterator>
#include <optional>
#include <string_view>
#include <vector>

using namespace openusdconnect::client;

[[nodiscard]] constexpr bool MatchesWire(RejectionCode code,
										 OpenUSDConnect::TransactionRejectionCode wire) noexcept
{
	return static_cast<std::uint8_t>(code) == static_cast<std::uint8_t>(wire);
}

static_assert(MatchesWire(RejectionCode::None, OpenUSDConnect::TransactionRejectionCode::None));
static_assert(MatchesWire(RejectionCode::InvalidIdentity,
						  OpenUSDConnect::TransactionRejectionCode::InvalidIdentity));
static_assert(MatchesWire(RejectionCode::UnexpectedId,
						  OpenUSDConnect::TransactionRejectionCode::UnexpectedId));
static_assert(MatchesWire(RejectionCode::StaleLayerGraph,
						  OpenUSDConnect::TransactionRejectionCode::StaleLayerGraph));
static_assert(MatchesWire(RejectionCode::InvalidTransaction,
						  OpenUSDConnect::TransactionRejectionCode::InvalidTransaction));
// A new wire code needs a rejection policy entry.
static_assert(MatchesWire(RejectionCode::InvalidTransaction,
						  OpenUSDConnect::TransactionRejectionCode::MAX));

struct PhaseCase final
{
	bool PhaseInputs::* Input;
	ClientPhase Phase;
};

constexpr PhaseCase kPhasePrecedence[] = {
	{&PhaseInputs::Closed, ClientPhase::Closed},
	{&PhaseInputs::RecoveryRequired, ClientPhase::RecoveryRequired},
	{&PhaseInputs::Rejected, ClientPhase::Rejected},
	{&PhaseInputs::Parked, ClientPhase::Parked},
	{&PhaseInputs::Replaying, ClientPhase::Replaying},
	{&PhaseInputs::Ready, ClientPhase::Ready},
	{&PhaseInputs::Connecting, ClientPhase::Connecting},
};

static void TestEachPhaseOutranksThePhasesAfterIt()
{
	const std::size_t count = std::size(kPhasePrecedence);
	for (std::size_t index = 0; index < count; ++index)
	{
		PhaseInputs inputs;
		for (std::size_t lower = index; lower < count; ++lower)
		{
			inputs.*kPhasePrecedence[lower].Input = true;
		}
		CHECK(ComputePhase(inputs) == kPhasePrecedence[index].Phase);
	}
	CHECK(ComputePhase(PhaseInputs{}) == ClientPhase::Offline);
}

struct RejectionCase final
{
	std::uint8_t Code;
	std::optional<std::string_view> Name;
	ProducerRecoveryDisposition Disposition;
};

static void TestRejectionNamesAndDispositions()
{
	const RejectionCase cases[] = {
		{0, std::nullopt, ProducerRecoveryDisposition::SessionFatal},
		{1, "invalid_identity", ProducerRecoveryDisposition::SessionFatal},
		{2, "unexpected_id", ProducerRecoveryDisposition::SessionFatal},
		{3, "stale_layer_graph", ProducerRecoveryDisposition::RecoverableConflict},
		{4, "invalid_transaction", ProducerRecoveryDisposition::InvalidOperation},
		{5, std::nullopt, ProducerRecoveryDisposition::SessionFatal},
		{255, std::nullopt, ProducerRecoveryDisposition::SessionFatal},
	};
	for (const RejectionCase& expected : cases)
	{
		CHECK(RejectionCodeName(expected.Code) == expected.Name);
		CHECK(RejectionDisposition(expected.Code) == expected.Disposition);
	}
}

static void TestTransactionFailureDescription()
{
	TransactionFailure failure{3, 3, "layer was remapped", 2};
	CHECK(failure.Disposition() == ProducerRecoveryDisposition::RecoverableConflict);
	CHECK(failure.Describe() ==
		  "transaction 3 rejected (stale_layer_graph, expected transaction 2): layer was remapped");
	failure = {7, 9, "", 0};
	CHECK(failure.Disposition() == ProducerRecoveryDisposition::SessionFatal);
	CHECK(failure.Describe() == "transaction 7 rejected (unknown_9): no reason supplied");
}

using TestInbox = OrderedReceiverSession<int>;

static void AcceptFrames(TestInbox& inbox, std::uint64_t generation, int count)
{
	for (int frame = 0; frame < count; ++frame)
	{
		CHECK(inbox.Accept(generation, ReceiverMessageKind::Other, 0, frame) ==
			  AcceptResult::Accepted);
	}
}

static void TestHoldCoversOnlyFramesQueuedBeforeTheMarker()
{
	TestInbox inbox(1, 8);
	const std::uint64_t generation = inbox.BeginConnection().Generation;
	CHECK(inbox.DrainedThrough(inbox.FreezeMarker()));
	AcceptFrames(inbox, generation, 3);
	const std::uint64_t marker = inbox.FreezeMarker();
	AcceptFrames(inbox, generation, 2);
	CHECK(inbox.Drain(2).size() == 2);
	CHECK(!inbox.DrainedThrough(marker));
	AcceptFrames(inbox, generation, 2);
	int frame = -1;
	CHECK(inbox.TryPop(frame));
	CHECK(inbox.DrainedThrough(marker));
	CHECK(inbox.Size() == 4);
}

static void TestRejectedFramesDoNotExtendTheHold()
{
	TestInbox inbox(1, 2);
	const std::uint64_t generation = inbox.BeginConnection().Generation;
	AcceptFrames(inbox, generation, 2);
	CHECK(inbox.Accept(generation, ReceiverMessageKind::Other, 0, 2) == AcceptResult::QueueFull);
	const std::uint64_t marker = inbox.FreezeMarker();
	CHECK(inbox.Drain().size() == 2);
	CHECK(inbox.DrainedThrough(marker));
}

static void TestReplayRequestReleasesTheHold()
{
	TestInbox inbox(1, 8);
	const std::uint64_t generation = inbox.BeginConnection().Generation;
	AcceptFrames(inbox, generation, 3);
	const std::uint64_t marker = inbox.FreezeMarker();
	CHECK(!inbox.DrainedThrough(marker));
	CHECK(inbox.RequestReplayFrom(1));
	CHECK(inbox.DrainedThrough(marker));
}

static void TestReplayMarkerRequiresItsRecords()
{
	TestInbox contiguous(1, 8, true);
	const std::uint64_t generation = contiguous.BeginConnection().Generation;
	CHECK(contiguous.Accept(generation, ReceiverMessageKind::Event, 1, 1) ==
		  AcceptResult::Accepted);
	CHECK(contiguous.AcceptReplayComplete(generation, 2, 0) == AcceptResult::SequenceGap);
	CHECK(contiguous.AcceptReplayComplete(generation, 1, 0) == AcceptResult::Accepted);

	TestInbox unordered(1, 8);
	CHECK(unordered.AcceptReplayComplete(unordered.BeginConnection().Generation, 2, 0) ==
		  AcceptResult::Accepted);
}

static void TestResetPendingUntilAppliedOrDiscarded()
{
	TestInbox inbox(1, 8, true);
	std::uint64_t generation = inbox.BeginConnection().Generation;
	const auto accept_reset = [&]
	{
		CHECK(inbox.Accept(generation, ReceiverMessageKind::Resync, 0, 0) ==
			  AcceptResult::Accepted);
	};
	CHECK(!inbox.ResetPending());
	accept_reset();
	CHECK(inbox.ResetPending());
	int frame = -1;
	CHECK(inbox.TryPop(frame));
	CHECK(inbox.ResetPending());
	inbox.ResetAppliedProgress();
	CHECK(!inbox.ResetPending());

	// One report covers every reset drained before it.
	accept_reset();
	CHECK(inbox.Accept(generation, ReceiverMessageKind::Event, 1, 1) == AcceptResult::Accepted);
	accept_reset();
	CHECK(inbox.Drain().size() == 3);
	inbox.ResetAppliedProgress();
	CHECK(!inbox.ResetPending());

	// A replay request settles queued and drained resets alike.
	accept_reset();
	CHECK(inbox.TryPop(frame));
	accept_reset();
	CHECK(inbox.RequestReplayFrom(1));
	CHECK(!inbox.ResetPending());

	// A late report for the drained reset cannot settle a newer one.
	generation = inbox.BeginConnection().Generation;
	accept_reset();
	inbox.ResetAppliedProgress();
	CHECK(inbox.ResetPending());
}

// Queues an event whose payload is its sequence.
[[nodiscard]] static AcceptResult AcceptEvent(TestInbox& inbox, std::uint64_t generation,
											  std::int32_t sequence)
{
	return inbox.Accept(generation, ReceiverMessageKind::Event, sequence, sequence);
}

[[nodiscard]] static AcceptResult AcceptReset(TestInbox& inbox, std::uint64_t generation)
{
	return inbox.Accept(generation, ReceiverMessageKind::Resync, 0, 0);
}

static void TestInboxRejectsInvalidArguments()
{
	CHECK(!TestInbox::IsValidConfiguration(0, 1));
	CHECK(!TestInbox::IsValidConfiguration(1, 0));
	TestInbox inbox(1, 1);
	const std::uint64_t generation = inbox.BeginConnection().Generation;
	CHECK(AcceptEvent(inbox, generation, 0) == AcceptResult::InvalidSequence);
	CHECK(inbox.Accept(generation, ReceiverMessageKind::LayerGraphState, 0, 0) ==
		  AcceptResult::InvalidSequence);
	CHECK(inbox.AcceptReplayComplete(generation, -1, 0) == AcceptResult::InvalidSequence);
	CHECK(!inbox.RequestReplayFrom(0));
	CHECK(inbox.Size() == 0);
}

static void TestReplayAppliesBeforeLiveFramesDrain()
{
	TestInbox inbox(1, 8);
	const ConnectionStart connection = inbox.BeginConnection();
	CHECK(connection.SyncFrom == 1);
	CHECK(AcceptEvent(inbox, connection.Generation, 1) == AcceptResult::Accepted);
	CHECK(inbox.AcceptReplayComplete(connection.Generation, 1, 7) == AcceptResult::Accepted);
	CHECK(AcceptEvent(inbox, connection.Generation, 2) == AcceptResult::Accepted);

	CHECK(!inbox.MarkReplayApplied());
	CHECK(inbox.Drain(1) == std::vector<int>{1});
	CHECK(inbox.MarkReplayApplied());
	CHECK(inbox.ReplayHeadSequence() == 1);
	CHECK(inbox.ReplayEpoch() == 7);
	CHECK(inbox.Drain() == std::vector<int>{2});
}

static void TestStaleGenerationIsRejectedWithoutMutation()
{
	TestInbox inbox(4, 8);
	const ConnectionStart first = inbox.BeginConnection();
	const ConnectionStart second = inbox.BeginConnection();
	CHECK(AcceptEvent(inbox, first.Generation, 4) == AcceptResult::StaleGeneration);
	CHECK(inbox.Size() == 0);
	CHECK(inbox.LastSequence() == 3);
	CHECK(second.SyncFrom == 4);
}

static void TestOverflowIsBoundedAndReplayable()
{
	TestInbox inbox(1, 1);
	const std::uint64_t generation = inbox.BeginConnection().Generation;
	CHECK(AcceptEvent(inbox, generation, 1) == AcceptResult::Accepted);
	CHECK(AcceptEvent(inbox, generation, 2) == AcceptResult::QueueFull);
	CHECK(inbox.Overflowed());
	CHECK(inbox.Drain() == std::vector<int>{1});

	CHECK(inbox.RequestReplayFrom(2));
	CHECK(inbox.BeginConnection().SyncFrom == 2);
	CHECK(!inbox.Overflowed());
}

static void TestResetReconnectsFromOneWithoutDiscardingTheQueue(bool require_contiguous,
																bool queued_prefix)
{
	TestInbox inbox(4, queued_prefix ? 2 : 1, require_contiguous);
	ConnectionStart connection = inbox.BeginConnection();
	CHECK(connection.SyncFrom == 4);
	std::vector<int> expected;
	if (queued_prefix)
	{
		CHECK(AcceptEvent(inbox, connection.Generation, 4) == AcceptResult::Accepted);
		expected.push_back(4);
	}
	CHECK(AcceptReset(inbox, connection.Generation) == AcceptResult::Accepted);
	expected.push_back(0);
	CHECK(inbox.Size() == expected.size());
	CHECK(inbox.LastSequence() == 0);
	CHECK(AcceptEvent(inbox, connection.Generation, 1) == AcceptResult::QueueFull);
	// Disconnects before the first new event keep the reset cursor and every
	// queued frame, even before the consumer drains.
	for (int attempt = 0; attempt < 2; ++attempt)
	{
		inbox.Disconnect(connection.Generation);
		connection = inbox.BeginConnection();
		CHECK(connection.SyncFrom == 1);
		CHECK(inbox.Size() == expected.size());
	}
	CHECK(inbox.Drain() == expected);
	inbox.ClearOverflow();
	CHECK(AcceptEvent(inbox, connection.Generation, 1) == AcceptResult::Accepted);
	inbox.Disconnect(connection.Generation);
	CHECK(inbox.BeginConnection().SyncFrom == 2);
	CHECK(inbox.Drain() == std::vector<int>{1});
}

static void TestFullReplayCursorSurvivesDisconnectBeforeAnyFrames()
{
	TestInbox inbox(4, 1);
	CHECK(inbox.BeginConnection().SyncFrom == 4);
	CHECK(inbox.RequestReplayFrom(1));
	for (int attempt = 0; attempt < 2; ++attempt)
	{
		const ConnectionStart connection = inbox.BeginConnection();
		CHECK(connection.SyncFrom == 1);
		inbox.Disconnect(connection.Generation);
	}
}

static void TestRejectedResetPreservesTheSnapshotCursorAndQueue()
{
	TestInbox inbox(4, 1);
	const std::uint64_t generation = inbox.BeginConnection().Generation;
	CHECK(AcceptEvent(inbox, generation, 4) == AcceptResult::Accepted);
	CHECK(AcceptReset(inbox, generation) == AcceptResult::QueueFull);
	CHECK(inbox.LastSequence() == 4);
	inbox.Disconnect(generation);
	CHECK(inbox.BeginConnection().SyncFrom == 5);
	CHECK(inbox.Drain() == std::vector<int>{4});
}

static void TestContiguousDelivery()
{
	TestInbox inbox(1, 4, true);
	const std::uint64_t generation = inbox.BeginConnection().Generation;
	CHECK(AcceptEvent(inbox, generation, 2) == AcceptResult::SequenceGap);
	CHECK(AcceptEvent(inbox, generation, 1) == AcceptResult::Accepted);
	CHECK(AcceptEvent(inbox, generation, 1) == AcceptResult::Duplicate);
	CHECK(inbox.Drain() == std::vector<int>{1});
	CHECK(inbox.MarkAppliedThrough(generation, 1));
	CHECK(inbox.LastAppliedSequence() == 1);
}

static void TestHighwaterAheadQuarantinesTheProducerSession()
{
	OrderedProducerSession<int> session(2);
	const std::optional<ProducerConnectionStart> connection = session.BeginConnection();
	CHECK(connection);
	CHECK(session.AcceptHello(connection->Generation, 1) == ProducerResult::HighwaterAhead);
	CHECK(session.Phase() == ProducerPhase::RecoveryRequired);
	CHECK(session.RecoveryRequired());
	CHECK(!session.BeginConnection());
}

int main()
{
	TestEachPhaseOutranksThePhasesAfterIt();
	TestRejectionNamesAndDispositions();
	TestTransactionFailureDescription();
	TestHoldCoversOnlyFramesQueuedBeforeTheMarker();
	TestRejectedFramesDoNotExtendTheHold();
	TestReplayRequestReleasesTheHold();
	TestReplayMarkerRequiresItsRecords();
	TestResetPendingUntilAppliedOrDiscarded();
	TestInboxRejectsInvalidArguments();
	TestReplayAppliesBeforeLiveFramesDrain();
	TestStaleGenerationIsRejectedWithoutMutation();
	TestOverflowIsBoundedAndReplayable();
	for (const bool require_contiguous : {false, true})
	{
		for (const bool queued_prefix : {false, true})
		{
			TestResetReconnectsFromOneWithoutDiscardingTheQueue(require_contiguous, queued_prefix);
		}
	}
	TestFullReplayCursorSurvivesDisconnectBeforeAnyFrames();
	TestRejectedResetPreservesTheSnapshotCursorAndQueue();
	TestContiguousDelivery();
	TestHighwaterAheadQuarantinesTheProducerSession();
	return 0;
}
