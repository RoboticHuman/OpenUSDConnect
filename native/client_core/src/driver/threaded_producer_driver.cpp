#include "openusdconnect/client/driver/threaded_producer_driver.h"

#include "driver_loop.h"
#include "openusdconnect/client/engine/producer_endpoint.h"

#include <algorithm>
#include <chrono>
#include <utility>

namespace openusdconnect::client
{
namespace
{

// How long Flush waits after a failed attempt before it connects again.
constexpr std::chrono::milliseconds kFlushRetryPause{100};

[[nodiscard]] TimePoint Now() noexcept
{
	return std::chrono::steady_clock::now();
}

[[nodiscard]] std::chrono::milliseconds Until(TimePoint deadline) noexcept
{
	return std::max(std::chrono::ceil<std::chrono::milliseconds>(deadline - Now()),
					std::chrono::milliseconds::zero());
}

[[nodiscard]] std::optional<std::chrono::milliseconds> Until(std::optional<TimePoint> deadline)
{
	return deadline ? std::optional(Until(*deadline)) : std::nullopt;
}

} // namespace

ThreadedProducerDriver::ThreadedProducerDriver(ProducerEndpoint& endpoint,
											   NotificationQueue& notifications,
											   std::shared_ptr<SocketFactory> sockets,
											   DriverCallbacks callbacks)
	: Endpoint(endpoint)
	, Loop(std::make_unique<detail::DriverLoop<ProducerEndpoint>>(
		  endpoint,
		  detail::LoopRole<ProducerEndpoint>{endpoint.Configuration().HandshakeTimeout},
		  notifications, std::move(sockets), std::move(callbacks)))
{
}

ThreadedProducerDriver::~ThreadedProducerDriver() = default;

bool ThreadedProducerDriver::Start()
{
	return Loop->Start();
}

void ThreadedProducerDriver::Stop()
{
	Loop->Stop();
}

void ThreadedProducerDriver::Wake()
{
	Loop->Wake();
}

bool ThreadedProducerDriver::Join(std::optional<std::chrono::milliseconds> timeout)
{
	return Loop->Join(timeout);
}

bool ThreadedProducerDriver::Connect(std::optional<std::chrono::milliseconds> timeout)
{
	if (!CanWait())
	{
		return Endpoint.Status().Connected;
	}
	const std::chrono::milliseconds handshake = Endpoint.Configuration().HandshakeTimeout;
	const TimePoint deadline = Now() + (timeout ? std::min(*timeout, handshake) : handshake);
	for (;;)
	{
		const ConnectResult result = Endpoint.Connect(Now(), deadline);
		if (result == ConnectResult::Connected || result == ConnectResult::Refused)
		{
			return result == ConnectResult::Connected;
		}
		if (result == ConnectResult::Started)
		{
			Loop->Wake();
		}
		// A Busy result retries once the attempt or close in flight ends.
		if (!WaitSettled(deadline) || result == ConnectResult::Started)
		{
			return Endpoint.Status().Connected;
		}
	}
}

FlushResult ThreadedProducerDriver::Flush(std::optional<std::chrono::milliseconds> timeout)
{
	const std::optional<TimePoint> deadline =
		timeout ? std::optional(Now() + *timeout) : std::nullopt;
	for (;;)
	{
		const ProducerStatus status = Endpoint.Status();
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
			static_cast<void>(Loop->Wait(
				[this]
				{
					const ProducerStatus current = Endpoint.Status();
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

bool ThreadedProducerDriver::Running() const
{
	return Loop->Running();
}

bool ThreadedProducerDriver::Stopped() const
{
	return Loop->Stopped();
}

std::optional<std::thread::id> ThreadedProducerDriver::ThreadId() const
{
	return Loop->ThreadId();
}

std::optional<TransportFailure> ThreadedProducerDriver::LastFailure() const
{
	return Loop->LastFailure();
}

bool ThreadedProducerDriver::CanWait() const
{
	// On the loop thread a wait would wait for itself.
	return Loop->Running() && Loop->ThreadId() != std::this_thread::get_id();
}

bool ThreadedProducerDriver::WaitSettled(TimePoint deadline)
{
	return Loop->Wait(
		[this]
		{
			const ProducerStatus status = Endpoint.Status();
			return !status.Handshaking && !status.Closing;
		},
		Until(deadline));
}

void ThreadedProducerDriver::Pause(TimePoint until)
{
	static_cast<void>(Loop->Wait(
		[]
		{
			return false;
		},
		Until(until)));
}

} // namespace openusdconnect::client
