#pragma once

#include "openusdconnect/client/engine/actions.h"
#include "openusdconnect/client/engine/notification.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>

namespace openusdconnect::client
{

enum class SocketResult : std::uint8_t
{
	Success,
	// The call passed its deadline.
	Timeout,
	Interrupted,
	// The peer closed the connection.
	Closed,
	// SystemError describes the failure.
	Failed,
};

// A blocking TCP connection owned by one thread. Destroying it closes it.
class Socket
{
public:
	virtual ~Socket() = default;

	[[nodiscard]] virtual SocketResult Connect(const std::string& host, std::uint16_t port,
											   TimePoint deadline) = 0;
	// Timeout once it has to wait for the peer past deadline.
	[[nodiscard]] virtual SocketResult SendAll(const std::uint8_t* data, std::size_t size,
											   TimePoint deadline) = 0;
	// Waits for at least one byte until deadline, or without one until it arrives.
	[[nodiscard]] virtual SocketResult Receive(std::uint8_t* buffer, std::size_t capacity,
											   std::optional<TimePoint> deadline,
											   std::size_t& received) = 0;
	// The operating system error of the last Timeout or Failed result.
	[[nodiscard]] virtual int SystemError() const noexcept = 0;

	// Thread-safe. Ends the blocked call and every later one with Interrupted.
	virtual void Interrupt() noexcept = 0;
	// Thread-safe. Ends the blocked or the next Receive with Interrupted.
	virtual void Wake() noexcept = 0;

protected:
	Socket() = default;
	Socket(const Socket&) = delete;
	Socket& operator=(const Socket&) = delete;
};

class SocketFactory
{
public:
	virtual ~SocketFactory() = default;

	// An unconnected socket, so another thread can interrupt its Connect.
	[[nodiscard]] virtual std::unique_ptr<Socket> Create() = 0;

protected:
	SocketFactory() = default;
	SocketFactory(const SocketFactory&) = delete;
	SocketFactory& operator=(const SocketFactory&) = delete;
};

// Winsock or BSD sockets with TCP_NODELAY, so small control frames go out at once.
class TcpSocketFactory final : public SocketFactory
{
public:
	TcpSocketFactory();

	[[nodiscard]] std::unique_ptr<Socket> Create() override;
};

enum class SocketOperation : std::uint8_t
{
	Connect,
	Send,
	Receive,
};

struct TransportFailure final
{
	SocketOperation Operation = SocketOperation::Connect;
	// Timeout or Failed.
	SocketResult Result = SocketResult::Failed;
	int SystemError = 0;
};

[[nodiscard]] std::string DescribeSystemError(int system_error);
[[nodiscard]] std::string Describe(const TransportFailure& failure);

// What a reference driver calls on its host. Every callback is optional, must
// return normally, and runs on the driver thread, which holds no driver or
// endpoint lock while calling it.
struct DriverCallbacks final
{
	// Read just before each handshake; nullopt abandons that connection attempt.
	std::function<std::optional<std::string>()> Token;
	// When set, the driver drains the notification queue into it after every
	// endpoint call, so a notification is handled before the next attempt.
	std::function<void(Notification)> Notifications;
	std::function<void(LogLevel, const std::string&)> Log;
};

} // namespace openusdconnect::client
