#pragma once

#include "openusdconnect/client/driver/driver_callbacks.h"
#include "openusdconnect/client/driver/socket.h"
#include "openusdconnect/client/engine/notification.h"

#include <chrono>
#include <memory>
#include <optional>
#include <thread>

namespace openusdconnect::client
{

class ReceiverEndpoint;

namespace detail
{
template <typename Endpoint>
class DriverLoop;
} // namespace detail

// Reference host loop: one thread with blocking sockets drives a
// ReceiverEndpoint. Hosts with their own scheduler drive the endpoint instead.
class ThreadedReceiverDriver final
{
public:
	// notifications must be the queue the endpoint pushes to.
	ThreadedReceiverDriver(ReceiverEndpoint& endpoint, NotificationQueue& notifications,
						   std::shared_ptr<SocketFactory> sockets, DriverCallbacks callbacks = {});
	// Stops and joins the loop; destroyed from Exited, it detaches instead.
	~ThreadedReceiverDriver();
	ThreadedReceiverDriver(const ThreadedReceiverDriver&) = delete;
	ThreadedReceiverDriver& operator=(const ThreadedReceiverDriver&) = delete;

	// False when already started.
	[[nodiscard]] bool Start();
	// Stops the endpoint and wakes the loop, which then exits. Never blocks.
	void Stop();
	// Call after a consumer-thread endpoint call (RequestReplayFrom,
	// MarkReplayApplied) so the loop applies its actions and waits re-check.
	void Wake();
	// Waits for the loop to exit; false on timeout or on the driver thread.
	[[nodiscard]] bool Join(std::optional<std::chrono::milliseconds> timeout = std::nullopt);

	// Each returns the state once it holds, the loop stops, or timeout passes.
	[[nodiscard]] bool WaitConnected(std::optional<std::chrono::milliseconds> timeout);
	[[nodiscard]] bool WaitSynchronized(std::optional<std::chrono::milliseconds> timeout);

	[[nodiscard]] bool Running() const;
	// The loop ran and exited.
	[[nodiscard]] bool Stopped() const;
	[[nodiscard]] std::optional<std::thread::id> ThreadId() const;
	// Why the latest connection attempt failed, if it did.
	[[nodiscard]] std::optional<TransportFailure> LastFailure() const;

private:
	ReceiverEndpoint& Endpoint;
	const std::unique_ptr<detail::DriverLoop<ReceiverEndpoint>> Loop;
};

} // namespace openusdconnect::client
