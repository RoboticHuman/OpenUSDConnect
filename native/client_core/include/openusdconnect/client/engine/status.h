#pragma once

#include <cstdint>
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

} // namespace openusdconnect::client
