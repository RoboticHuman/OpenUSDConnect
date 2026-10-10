#pragma once

#include "openusdconnect/client/driver/threaded_driver.h"
#include "openusdconnect/client/engine/receiver_endpoint.h"

#include <chrono>
#include <memory>
#include <optional>
#include <utility>

namespace openusdconnect::client
{

// Drives a ReceiverEndpoint on one thread. After a consumer-thread endpoint
// call that can close the connection or complete the replay, such as
// RequestReplayFrom or MarkReplayApplied, call Wake so the loop applies its
// actions and the waits re-check.
class ThreadedReceiverDriver final : public ThreadedDriver<ReceiverEndpoint>
{
public:
	ThreadedReceiverDriver(ReceiverEndpoint& endpoint, NotificationQueue& notifications,
						   std::shared_ptr<SocketFactory> sockets, DriverCallbacks callbacks = {})
		: ThreadedDriver(endpoint, notifications, std::move(sockets), std::move(callbacks),
						 endpoint.Configuration().SocketTimeout, &ReceiverEndpoint::OnReadTimeout)
	{
	}

	// Starts the endpoint, then the loop; false when already started.
	[[nodiscard]] bool Start()
	{
		static_cast<void>(Target.Start(Now()));
		return ThreadedDriver::Start();
	}

	// Stops the loop and waits up to timeout, without one indefinitely, for it to
	// exit; returns whether it exited. A receiver queues nothing to write first.
	[[nodiscard]] bool Close(std::optional<std::chrono::milliseconds> timeout)
	{
		Stop();
		return Join(timeout);
	}

	// Each returns the state once it holds, the loop stops, or timeout passes.
	[[nodiscard]] bool WaitConnected(std::optional<std::chrono::milliseconds> timeout)
	{
		return WaitFor(&ReceiverStatus::Connected, timeout);
	}

	[[nodiscard]] bool WaitSynchronized(std::optional<std::chrono::milliseconds> timeout)
	{
		return WaitFor(&ReceiverStatus::Synchronized, timeout);
	}

private:
	[[nodiscard]] bool WaitFor(bool ReceiverStatus::* state,
							   std::optional<std::chrono::milliseconds> timeout)
	{
		return Wait(
			[this, state]
			{
				return Target.Status().*state;
			},
			timeout);
	}
};

} // namespace openusdconnect::client
