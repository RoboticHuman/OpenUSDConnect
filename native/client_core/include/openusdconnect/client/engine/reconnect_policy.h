#pragma once

#include "openusdconnect/client/engine/actions.h"

#include <algorithm>
#include <cassert>
#include <chrono>

namespace openusdconnect::client
{

// Exponential backoff between connection attempts, expressed as due times.
class ReconnectPolicy final
{
public:
	ReconnectPolicy(bool enabled, std::chrono::milliseconds base_delay,
					std::chrono::milliseconds max_delay) noexcept
		: EnabledValue(enabled)
		, BaseDelay(base_delay)
		, MaxDelay(max_delay)
		, Delay(base_delay)
	{
		assert(IsValidConfiguration(BaseDelay, MaxDelay));
	}

	[[nodiscard]] static bool IsValidConfiguration(std::chrono::milliseconds base_delay,
												   std::chrono::milliseconds max_delay) noexcept
	{
		return base_delay.count() > 0 && max_delay >= base_delay;
	}

	[[nodiscard]] bool Enabled() const noexcept
	{
		return EnabledValue;
	}

	void SetEnabled(bool enabled) noexcept
	{
		EnabledValue = enabled;
	}

	// A session reached its connected state; the next wait starts from the base.
	void Reset() noexcept
	{
		Delay = BaseDelay;
	}

	[[nodiscard]] TimePoint NextAttempt(TimePoint now) noexcept
	{
		const TimePoint due = now + Delay;
		Delay = std::min(Delay * 2, MaxDelay);
		return due;
	}

	// After an overflow the next attempt waits for the consumer to drain the
	// queue, but no longer than this deadline.
	[[nodiscard]] TimePoint DrainDeadline(TimePoint now) noexcept
	{
		Reset();
		return now + MaxDelay;
	}

private:
	bool EnabledValue;
	const std::chrono::milliseconds BaseDelay;
	const std::chrono::milliseconds MaxDelay;
	std::chrono::milliseconds Delay;
};

} // namespace openusdconnect::client
