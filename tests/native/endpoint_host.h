#pragma once

#include "openusdconnect/client/engine/actions.h"
#include "openusdconnect/client/engine/notification.h"

#include "frames.h"
#include "test_check.h"

#include <algorithm>
#include <chrono>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

namespace endpoint_test
{

template <typename Config, typename Field>
[[nodiscard]] Config With(Config config, Field Config::* field, std::common_type_t<Field> value)
{
	config.*field = value;
	return config;
}

template <typename T>
[[nodiscard]] const T& As(const Notification& notification)
{
	CHECK(std::holds_alternative<T>(notification));
	return std::get<T>(notification);
}

// Plays the host around one endpoint, on a clock the test advances.
template <typename EndpointType>
class Host
{
public:
	template <typename Config>
	explicit Host(const Config& config)
		: Endpoint(config, Notifications)
	{
	}

	// Actions since the last call, without log lines.
	[[nodiscard]] std::vector<Action> Commands()
	{
		Collect();
		return std::exchange(Pending, {});
	}

	template <typename T>
	[[nodiscard]] T Single()
	{
		std::vector<Action> commands = Commands();
		CHECK(commands.size() == 1);
		CHECK(std::holds_alternative<T>(commands.front()));
		return std::get<T>(std::move(commands.front()));
	}

	// The oldest action not yet taken, which must be a T.
	template <typename T>
	[[nodiscard]] T Next()
	{
		Collect();
		CHECK(!Pending.empty() && std::holds_alternative<T>(Pending.front()));
		T next = std::get<T>(std::move(Pending.front()));
		Pending.erase(Pending.begin());
		return next;
	}

	// Whether the endpoint logged at level since the last call.
	[[nodiscard]] bool Logged(LogLevel level)
	{
		Collect();
		const bool logged = std::any_of(Logs.begin(), Logs.end(),
										[level](const LogAction& log)
										{
											return log.Level == level;
										});
		Logs.clear();
		return logged;
	}

	[[nodiscard]] std::vector<Notification> Notices()
	{
		return Notifications.Drain();
	}

	template <typename T>
	[[nodiscard]] T Notice()
	{
		std::vector<Notification> notices = Notices();
		CHECK(notices.size() == 1);
		CHECK(std::holds_alternative<T>(notices.front()));
		return std::get<T>(std::move(notices.front()));
	}

	[[nodiscard]] auto Status() const
	{
		return Endpoint.Status();
	}

	void Feed(const Bytes& bytes)
	{
		Endpoint.OnBytes(bytes.data(), bytes.size());
	}

	void Disconnect(DisconnectReason reason = DisconnectReason::PeerClosed)
	{
		Endpoint.OnDisconnected(reason, Now);
	}

	void Advance(std::chrono::milliseconds elapsed)
	{
		Now += elapsed;
		Endpoint.OnTick(Now);
	}

	NotificationQueue Notifications;
	EndpointType Endpoint;
	// Away from the clock's epoch, which no deadline may depend on.
	TimePoint Now = TimePoint{} + std::chrono::hours(1);

private:
	void Collect()
	{
		for (Action& action : Endpoint.TakeActions())
		{
			if (LogAction* log = std::get_if<LogAction>(&action))
			{
				Logs.push_back(std::move(*log));
			}
			else
			{
				Pending.push_back(std::move(action));
			}
		}
	}

	std::vector<Action> Pending;
	std::vector<LogAction> Logs;
};

} // namespace endpoint_test
