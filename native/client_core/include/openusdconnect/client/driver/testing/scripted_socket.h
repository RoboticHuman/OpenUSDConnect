#pragma once

#include "openusdconnect/client/driver/socket.h"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

namespace openusdconnect::client
{

namespace detail
{
struct ScriptState;
struct ScriptedChannel;
} // namespace detail

// The server's end of one accepted scripted connection.
class ScriptedConnection final
{
public:
	ScriptedConnection(std::shared_ptr<detail::ScriptState> state,
					   std::shared_ptr<detail::ScriptedChannel> channel);

	// One Receive returns each delivery. False once the client closed its socket.
	[[nodiscard]] bool Deliver(std::vector<std::uint8_t> bytes);
	// The next Receive reports Timeout without waiting.
	[[nodiscard]] bool DeliverTimeout();
	// Receive reports Closed once the earlier deliveries are read.
	void Close();

	[[nodiscard]] std::vector<std::uint8_t> Sent() const;
	// The client waits in Receive with every delivery read, so it handled them all.
	[[nodiscard]] bool WaitIdle(std::chrono::milliseconds timeout) const;
	[[nodiscard]] bool WaitClosed(std::chrono::milliseconds timeout) const;
	[[nodiscard]] bool ClosedByClient() const;

private:
	std::shared_ptr<detail::ScriptState> State;
	std::shared_ptr<detail::ScriptedChannel> Channel;
};

// A test seam: each Connect waits until the test accepts or refuses it.
class ScriptedSocketFactory final : public SocketFactory
{
public:
	ScriptedSocketFactory();

	[[nodiscard]] std::unique_ptr<Socket> Create() override;

	// Completes the oldest waiting Connect; nullptr when none begins within timeout.
	[[nodiscard]] std::shared_ptr<ScriptedConnection> Accept(std::chrono::milliseconds timeout);
	[[nodiscard]] bool Refuse(std::chrono::milliseconds timeout, int system_error);
	[[nodiscard]] std::size_t Attempts() const;

private:
	std::shared_ptr<detail::ScriptState> State;
};

} // namespace openusdconnect::client
