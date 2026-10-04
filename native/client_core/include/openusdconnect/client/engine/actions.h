#pragma once

#include "openusdconnect/client/engine/clock.h"

#include <cstdint>
#include <string>
#include <variant>
#include <vector>

namespace openusdconnect::client
{

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
	std::vector<std::uint8_t> Bytes;
};

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
