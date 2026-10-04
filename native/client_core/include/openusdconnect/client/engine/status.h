#pragma once

#include "openusdconnect/client/producer_recovery.h"

#include <cstdint>
#include <optional>
#include <string_view>
#include <utility>

namespace openusdconnect::client
{

enum class ClientPhase : std::uint8_t
{
	Offline,
	Connecting,
	Replaying,
	Ready,
	RecoveryRequired,
	Rejected,
	Closed,
	Parked,
};

struct PhaseInputs final
{
	bool Closed = false;
	bool RecoveryRequired = false;
	bool Rejected = false;
	bool Parked = false;
	bool Replaying = false;
	bool Ready = false;
	bool Connecting = false;
};

[[nodiscard]] inline ClientPhase ComputePhase(const PhaseInputs& inputs) noexcept
{
	const std::pair<bool, ClientPhase> precedence[] = {
		{inputs.Closed, ClientPhase::Closed},
		{inputs.RecoveryRequired, ClientPhase::RecoveryRequired},
		{inputs.Rejected, ClientPhase::Rejected},
		{inputs.Parked, ClientPhase::Parked},
		{inputs.Replaying, ClientPhase::Replaying},
		{inputs.Ready, ClientPhase::Ready},
		{inputs.Connecting, ClientPhase::Connecting},
	};
	for (const auto& [active, phase] : precedence)
	{
		if (active)
		{
			return phase;
		}
	}
	return ClientPhase::Offline;
}

// Wire values of OpenUSDConnect::TransactionRejectionCode.
enum class RejectionCode : std::uint8_t
{
	None = 0,
	InvalidIdentity = 1,
	UnexpectedId = 2,
	StaleLayerGraph = 3,
	InvalidTransaction = 4,
};

namespace detail
{

struct RejectionPolicy final
{
	RejectionCode Code;
	std::string_view Name;
	ProducerRecoveryDisposition Disposition;
};

inline constexpr RejectionPolicy kRejectionPolicies[] = {
	{RejectionCode::InvalidIdentity, "invalid_identity", ProducerRecoveryDisposition::SessionFatal},
	{RejectionCode::UnexpectedId, "unexpected_id", ProducerRecoveryDisposition::SessionFatal},
	{RejectionCode::StaleLayerGraph, "stale_layer_graph",
	 ProducerRecoveryDisposition::RecoverableConflict},
	{RejectionCode::InvalidTransaction, "invalid_transaction",
	 ProducerRecoveryDisposition::InvalidOperation},
};

[[nodiscard]] inline const RejectionPolicy* FindRejectionPolicy(std::uint8_t code) noexcept
{
	for (const RejectionPolicy& policy : kRejectionPolicies)
	{
		if (static_cast<std::uint8_t>(policy.Code) == code)
		{
			return &policy;
		}
	}
	return nullptr;
}

} // namespace detail

[[nodiscard]] inline std::optional<std::string_view> RejectionCodeName(std::uint8_t code) noexcept
{
	const detail::RejectionPolicy* policy = detail::FindRejectionPolicy(code);
	return policy ? std::optional<std::string_view>(policy->Name) : std::nullopt;
}

// Unknown codes from newer servers fail closed.
[[nodiscard]] inline ProducerRecoveryDisposition RejectionDisposition(std::uint8_t code) noexcept
{
	const detail::RejectionPolicy* policy = detail::FindRejectionPolicy(code);
	return policy ? policy->Disposition : ProducerRecoveryDisposition::SessionFatal;
}

} // namespace openusdconnect::client
