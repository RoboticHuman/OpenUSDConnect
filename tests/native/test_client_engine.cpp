#include "openusdconnect/client/engine/status.h"
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

int main()
{
	TestEachPhaseOutranksThePhasesAfterIt();
	TestRejectionNamesAndDispositions();
	return 0;
}
