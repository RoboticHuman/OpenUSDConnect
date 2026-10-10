#pragma once

#include "openusdconnect/client/driver/threaded_driver.h"
#include "openusdconnect/client/engine/producer_endpoint.h"

#include <chrono>
#include <cstdint>
#include <memory>
#include <optional>
#include <utility>

namespace openusdconnect::client
{

enum class FlushResult : std::uint8_t
{
	// Every appended transaction is acknowledged.
	Flushed,
	// The endpoint's Failure must be resolved first.
	RecoveryRequired,
	// The timeout passed, or the loop stopped, first.
	Unfinished,
};

// Drives a ProducerEndpoint on one thread. The endpoint connects only when
// asked, so the thread idles between connections until Stop. Call Wake after
// an endpoint call that queues actions (Append, QueueControl, RequestConnect,
// CancelConnect, Disconnect) so the loop applies them.
class ThreadedProducerDriver final : public ThreadedDriver<ProducerEndpoint>
{
public:
	ThreadedProducerDriver(ProducerEndpoint& endpoint, NotificationQueue& notifications,
						   std::shared_ptr<SocketFactory> sockets, DriverCallbacks callbacks = {})
		: ThreadedDriver(endpoint, notifications, std::move(sockets), std::move(callbacks),
						 endpoint.Configuration().HandshakeTimeout)
	{
	}

	// The blocking calls need the loop running on another thread; otherwise they
	// only report the endpoint's state.

	// Makes one attempt once any attempt or close in flight ends, all within
	// timeout and HandshakeTimeout, and returns whether the endpoint is connected.
	[[nodiscard]] bool Connect(std::optional<std::chrono::milliseconds> timeout);
	// Waits until every appended transaction is acknowledged, connecting while
	// disconnected and outside the rate-limit window.
	[[nodiscard]] FlushResult Flush(std::optional<std::chrono::milliseconds> timeout);
	// Lets the loop write the queued frames and a Quit, and close the connection,
	// for up to timeout or else a one-second grace; then stops the loop and waits
	// the rest of timeout, without one indefinitely, for it to exit. Returns
	// whether it exited. Never waits for acknowledgements, which Flush does, and
	// on the loop thread only stops.
	[[nodiscard]] bool Close(std::optional<std::chrono::milliseconds> timeout);

private:
	[[nodiscard]] bool CanWait() const;
	// Waits until no attempt or close is in flight; false once deadline passes first.
	[[nodiscard]] bool WaitSettled(TimePoint deadline);
	// WaitSettled, and also until no connection is open.
	[[nodiscard]] bool WaitDisconnected(TimePoint deadline);
	void Pause(TimePoint until);
};

} // namespace openusdconnect::client
