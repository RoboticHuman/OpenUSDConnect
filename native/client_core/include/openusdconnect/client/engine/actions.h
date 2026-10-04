#pragma once

#include <chrono>
#include <cstdint>
#include <memory>
#include <string>
#include <variant>
#include <vector>

namespace openusdconnect::client
{

// Hosts pass the current time in; the engine never reads a clock.
using TimePoint = std::chrono::steady_clock::time_point;

enum class DisconnectReason : std::uint8_t
{
	// Reported by the host.
	ConnectFailed,
	PeerClosed,
	TransportError,
	// Requested by the endpoint with a CloseAction.
	Stopped,
	HandshakeRejected,
	ReplayRequested,
	SequenceGap,
	QueueFull,
	ReadTimeout,
	ProtocolError,
	Cancelled,
	HandshakeTimeout,
	RecoveryRequired,
	RateLimited,
};

enum class LogLevel : std::uint8_t
{
	Debug,
	Info,
	Warning,
	Error,
};

// Open a socket, then report OnConnected, or OnDisconnected once the attempt
// fails or Deadline passes.
struct ConnectAction final
{
	std::string Host;
	std::uint16_t Port = 0;
	TimePoint Deadline;
};

// Write complete length-prefixed frames, in order.
struct SendAction final
{
	// Shared with the producer outbox, so replaying a transaction copies nothing.
	std::shared_ptr<const std::vector<std::uint8_t>> Bytes;
};

// How a connection or attempt ends, in both endpoints and the reference driver:
//   Stop            the host ends it, and no attempt follows.
//   Disconnect      the host ends the producer's; later attempts may follow.
//   Close           the endpoint ends it with a CloseAction; the driver's Close applies one.
//   Quit            the producer's Close that first says Quit to a published connection.
//   EndConnection   the endpoint's accounting for any end, whichever side caused it.
//   OnDisconnected  the host reports that the socket or attempt is gone.

// Close the socket, or abandon the connect attempt, then report OnDisconnected.
struct CloseAction final
{
	DisconnectReason Reason = DisconnectReason::Stopped;
};

// Call OnTick at Time.
struct WakeAction final
{
	TimePoint Time;
};

struct LogAction final
{
	LogLevel Level = LogLevel::Info;
	std::string Message;
};

using Action = std::variant<ConnectAction, SendAction, CloseAction, WakeAction, LogAction>;

} // namespace openusdconnect::client
