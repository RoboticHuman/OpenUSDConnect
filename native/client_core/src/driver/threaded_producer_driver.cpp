#include "openusdconnect/client/driver/threaded_producer_driver.h"

#include <algorithm>
#include <chrono>
#include <optional>
#include <thread>

namespace openusdconnect::client
{
namespace
{

// How long Flush waits after a failed attempt before it connects again.
constexpr std::chrono::milliseconds kFlushRetryPause{100};
// How long Close without a timeout lets the loop write and close the connection.
constexpr std::chrono::milliseconds kCloseGrace{1'000};

[[nodiscard]] std::chrono::milliseconds Until(TimePoint deadline) noexcept
{
	return std::max(
		std::chrono::ceil<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now()),
		std::chrono::milliseconds::zero());
}

[[nodiscard]] std::optional<std::chrono::milliseconds> Until(std::optional<TimePoint> deadline)
{
	return deadline ? std::optional(Until(*deadline)) : std::nullopt;
}

} // namespace

bool ThreadedProducerDriver::Connect(std::optional<std::chrono::milliseconds> timeout)
{
	if (!CanWait())
	{
		return Target.Status().Connected;
	}
	const std::chrono::milliseconds handshake = Target.Configuration().HandshakeTimeout;
	const TimePoint deadline = Now() + (timeout ? std::min(*timeout, handshake) : handshake);
	for (;;)
	{
		switch (Target.Connect(Now(), deadline))
		{
		case ConnectResult::Connected:
			return true;
		case ConnectResult::Refused:
			return false;
		case ConnectResult::Started:
			Wake();
			static_cast<void>(WaitSettled(deadline));
			return Target.Status().Connected;
		case ConnectResult::Busy:
			// Retry once the attempt or close in flight ends.
			if (!WaitSettled(deadline))
			{
				return Target.Status().Connected;
			}
			break;
		}
	}
}

FlushResult ThreadedProducerDriver::Flush(std::optional<std::chrono::milliseconds> timeout)
{
	const std::optional<TimePoint> deadline =
		timeout ? std::optional(Now() + *timeout) : std::nullopt;
	for (;;)
	{
		const ProducerStatus status = Target.Status();
		if (status.Failure)
		{
			return FlushResult::RecoveryRequired;
		}
		if (status.PendingTransactions == 0)
		{
			return FlushResult::Flushed;
		}
		const TimePoint now = Now();
		if (status.Stopped || !CanWait() || (deadline && now >= *deadline))
		{
			return FlushResult::Unfinished;
		}
		if (status.Connected)
		{
			static_cast<void>(Wait(
				[this]
				{
					const ProducerStatus current = Target.Status();
					return !current.Connected || current.PendingTransactions == 0 ||
						   current.Failure;
				},
				Until(deadline)));
		}
		else if (now < status.RetryAfter)
		{
			if (deadline && status.RetryAfter >= *deadline)
			{
				return FlushResult::Unfinished;
			}
			Pause(status.RetryAfter);
		}
		else if (!Connect(Until(deadline)))
		{
			const TimePoint retry = Now() + kFlushRetryPause;
			Pause(deadline ? std::min(retry, *deadline) : retry);
		}
	}
}

bool ThreadedProducerDriver::Close(std::optional<std::chrono::milliseconds> timeout)
{
	const std::optional<TimePoint> deadline =
		timeout ? std::optional(Now() + *timeout) : std::nullopt;
	if (CanWait())
	{
		// Disconnecting queues the Quit and the close after the pending sends.
		Target.Disconnect();
		Wake();
		static_cast<void>(WaitDisconnected(deadline.value_or(Now() + kCloseGrace)));
	}
	Stop();
	return Join(Until(deadline));
}

bool ThreadedProducerDriver::CanWait() const
{
	// On the loop thread a wait would wait for itself.
	return Running() && ThreadId() != std::this_thread::get_id();
}

bool ThreadedProducerDriver::WaitSettled(TimePoint deadline)
{
	return Wait(
		[this]
		{
			const ProducerStatus status = Target.Status();
			return !status.Handshaking && !status.Closing;
		},
		Until(deadline));
}

bool ThreadedProducerDriver::WaitDisconnected(TimePoint deadline)
{
	return Wait(
		[this]
		{
			const ProducerStatus status = Target.Status();
			return !status.Connected && !status.Handshaking && !status.Closing;
		},
		Until(deadline));
}

void ThreadedProducerDriver::Pause(TimePoint until)
{
	static_cast<void>(Wait(
		[]
		{
			return false;
		},
		Until(until)));
}

} // namespace openusdconnect::client
