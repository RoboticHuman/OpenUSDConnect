#pragma once

#include "openusdconnect/client/engine/actions.h"
#include "openusdconnect/client/schema/messages_generated.h"

#include <mutex>
#include <optional>
#include <string>
#include <utility>
#include <variant>
#include <vector>

namespace openusdconnect::client
{

struct Connected final
{
};

// Follows Connected when that session ends.
struct Disconnected final
{
	DisconnectReason Reason = DisconnectReason::PeerClosed;
};

struct HandshakeRejected final
{
	// AuthRejected rather than HelloRejected; Code is then Unspecified.
	bool Authentication = false;
	OpenUSDConnect::HelloRejectionCode Code = OpenUSDConnect::HelloRejectionCode::Unspecified;
	std::string Reason;
};

struct TokenIssued final
{
	std::string Token;
};

// Only the fields the server authored are set.
struct StageMetadata final
{
	std::optional<double> TimeCodesPerSecond;
	std::optional<double> FramesPerSecond;
	std::optional<double> StartTimeCode;
	std::optional<double> EndTimeCode;
	std::optional<double> MetersPerUnit;
	std::optional<std::string> UpAxis;
};

struct PlaybackState final
{
	double Time = 0.0;
	bool Playing = false;
	double Rate = 0.0;
	std::string LeaderClientId;
};

struct PlaybackClaimed final
{
	std::string LeaderClientId;
};

struct PlaybackRejected final
{
	std::string Reason;
	std::string CurrentLeaderClientId;
};

using Notification = std::variant<Connected, Disconnected, HandshakeRejected, TokenIssued,
								  StageMetadata, PlaybackState, PlaybackClaimed, PlaybackRejected>;

// Endpoints push while holding their own lock; the host drains on any thread.
class NotificationQueue final
{
public:
	void Push(Notification notification)
	{
		std::lock_guard lock(Mutex);
		Pending.push_back(std::move(notification));
	}

	[[nodiscard]] std::vector<Notification> Drain()
	{
		std::lock_guard lock(Mutex);
		return std::exchange(Pending, {});
	}

private:
	std::mutex Mutex;
	std::vector<Notification> Pending;
};

} // namespace openusdconnect::client
