#pragma once

#include "openusdconnect/client/driver/socket.h"
#include "openusdconnect/client/engine/notification.h"

#include <chrono>
#include <cstdint>
#include <memory>
#include <optional>
#include <thread>

namespace openusdconnect::client
{

class ProducerEndpoint;

namespace detail
{
template <typename Endpoint>
class DriverLoop;
} // namespace detail

enum class FlushResult : std::uint8_t
{
	// Every appended transaction is acknowledged.
	Flushed,
	// The endpoint's Failure must be resolved first.
	RecoveryRequired,
	// The timeout passed, or the loop stopped, first.
	Unfinished,
};

// Reference host loop: one thread with blocking sockets drives a
// ProducerEndpoint. The endpoint connects only when asked, so the thread idles
// between connections until Stop. Hosts with their own scheduler drive the
// endpoint instead.
class ThreadedProducerDriver final
{
public:
	// notifications must be the queue the endpoint pushes to.
	ThreadedProducerDriver(ProducerEndpoint& endpoint, NotificationQueue& notifications,
						   std::shared_ptr<SocketFactory> sockets, DriverCallbacks callbacks = {});
	// Stops and joins the loop; destroyed from Exited, it detaches instead.
	~ThreadedProducerDriver();
	ThreadedProducerDriver(const ThreadedProducerDriver&) = delete;
	ThreadedProducerDriver& operator=(const ThreadedProducerDriver&) = delete;

	// Starts the loop; false when already started.
	[[nodiscard]] bool Start();
	// Stops the endpoint and wakes the loop, which then exits. Never blocks.
	void Stop();
	// Call after an endpoint call that queues actions (Append, QueueControl,
	// RequestConnect, CancelConnect, Disconnect) so the loop applies them.
	void Wake();
	// Waits for the loop to exit; false on timeout or on the driver thread.
	[[nodiscard]] bool Join(std::optional<std::chrono::milliseconds> timeout = std::nullopt);

	// The blocking calls need the loop running on another thread; otherwise they
	// only report the endpoint's state.

	// Makes one attempt once any attempt or close in flight ends, all within
	// timeout and HandshakeTimeout, and returns whether the endpoint is connected.
	[[nodiscard]] bool Connect(std::optional<std::chrono::milliseconds> timeout);
	// Waits until every appended transaction is acknowledged, connecting while
	// disconnected and outside the rate-limit window.
	[[nodiscard]] FlushResult Flush(std::optional<std::chrono::milliseconds> timeout);

	[[nodiscard]] bool Running() const;
	// The loop ran and exited.
	[[nodiscard]] bool Stopped() const;
	[[nodiscard]] std::optional<std::thread::id> ThreadId() const;
	// Why the latest connection attempt failed, if it did.
	[[nodiscard]] std::optional<TransportFailure> LastFailure() const;

private:
	[[nodiscard]] bool CanWait() const;
	// Waits until no attempt or close is in flight; false once deadline passes first.
	[[nodiscard]] bool WaitSettled(TimePoint deadline);
	void Pause(TimePoint until);

	ProducerEndpoint& Endpoint;
	const std::unique_ptr<detail::DriverLoop<ProducerEndpoint>> Loop;
};

} // namespace openusdconnect::client
