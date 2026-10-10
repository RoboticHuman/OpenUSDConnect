#pragma once

#include <algorithm>
#include <cassert>
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

// Exponential backoff between connection attempts, expressed as due times.
class ReconnectPolicy final
{
public:
	ReconnectPolicy(bool enabled, std::chrono::milliseconds base_delay,
					std::chrono::milliseconds max_delay) noexcept
		: EnabledValue(enabled)
		, BaseDelay(base_delay)
		, MaxDelay(max_delay)
		, Delay(base_delay)
	{
		assert(IsValidConfiguration(BaseDelay, MaxDelay));
	}

	[[nodiscard]] static bool IsValidConfiguration(std::chrono::milliseconds base_delay,
												   std::chrono::milliseconds max_delay) noexcept
	{
		return base_delay.count() > 0 && max_delay >= base_delay;
	}

	[[nodiscard]] bool Enabled() const noexcept
	{
		return EnabledValue;
	}

	void SetEnabled(bool enabled) noexcept
	{
		EnabledValue = enabled;
	}

	// A session reached its connected state; the next wait starts from the base.
	void Reset() noexcept
	{
		Delay = BaseDelay;
	}

	[[nodiscard]] TimePoint NextAttempt(TimePoint now) noexcept
	{
		const TimePoint due = now + Delay;
		Delay = std::min(Delay * 2, MaxDelay);
		return due;
	}

	// After an overflow the next attempt waits for the consumer to drain the
	// queue, but no longer than this deadline.
	[[nodiscard]] TimePoint DrainDeadline(TimePoint now) noexcept
	{
		Reset();
		return now + MaxDelay;
	}

private:
	bool EnabledValue;
	const std::chrono::milliseconds BaseDelay;
	const std::chrono::milliseconds MaxDelay;
	std::chrono::milliseconds Delay;
};

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

// Both endpoints Close with ProtocolError on an undecodable frame or a handshake answer other than
// HelloOk, HelloRejected, or AuthRejected. After the handshake the receiver closes on a payload
// type it does not know; the producer ignores everything but transaction results and RateLimited.

// Close the socket, or abandon the connect attempt, then report OnDisconnected.
struct CloseAction final
{
	DisconnectReason Reason = DisconnectReason::Stopped;
};

struct LogAction final
{
	LogLevel Level = LogLevel::Info;
	std::string Message;
};

using Action = std::variant<ConnectAction, SendAction, CloseAction, LogAction>;

} // namespace openusdconnect::client
