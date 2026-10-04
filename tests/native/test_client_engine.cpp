#include "openusdconnect/client/engine/status.h"
#include "openusdconnect/client/producer_recovery.h"
#include "openusdconnect/client/receiver_session.h"
#include "openusdconnect/client/schema/messages_generated.h"

#include "test_check.h"

#include <cstddef>
#include <cstdint>
#include <iterator>
#include <optional>
#include <string_view>

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
	return 0;
}
