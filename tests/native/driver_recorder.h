#pragma once

#include "openusdconnect/client/driver/socket.h"
#include "openusdconnect/client/driver/testing/scripted_socket.h"
#include "openusdconnect/client/engine/notification.h"

#include "frames.h"
#include "test_check.h"

#include <chrono>
#include <cstddef>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <variant>
#include <vector>

// What the driver tests share: patience, a recorder, and a harness.
namespace driver_test
{

using namespace openusdconnect::client;

// Bounds every wait for the driver thread, which a passing test never reaches.
inline constexpr std::chrono::milliseconds kPatience{5'000};
// A scripted refusal's system error.
inline constexpr int kRefused = 10061;

// Polls for state the driver thread reports after the event a test waited for.
template <typename Ready>
[[nodiscard]] bool Eventually(Ready ready)
{
	const auto deadline = std::chrono::steady_clock::now() + kPatience;
	while (!ready())
	{
		if (std::chrono::steady_clock::now() >= deadline)
		{
			return false;
		}
		std::this_thread::sleep_for(std::chrono::milliseconds(1));
	}
	return true;
}

// Records what the driver thread reports.
class Recorder final
{
public:
	void Add(Notification notification)
	{
		std::lock_guard lock(Mutex);
		if (const TokenIssued* issued = std::get_if<TokenIssued>(&notification))
		{
			Token = issued->Token;
		}
		Notices.push_back(std::move(notification));
	}

	void Log(const std::string& message)
	{
		std::lock_guard lock(Mutex);
		Logs.push_back(message);
	}

	// Callbacks that record notifications, unless callbacks has its own, and logs.
	[[nodiscard]] DriverCallbacks Recording(DriverCallbacks callbacks)
	{
		if (!callbacks.Notifications)
		{
			callbacks.Notifications = [this](Notification notification)
			{
				Add(std::move(notification));
			};
		}
		callbacks.Log = [this](LogLevel, const std::string& message)
		{
			Log(message);
		};
		return callbacks;
	}

	[[nodiscard]] std::string IssuedToken() const
	{
		std::lock_guard lock(Mutex);
		return Token;
	}

	[[nodiscard]] bool Logged(std::string_view text) const
	{
		std::lock_guard lock(Mutex);
		for (const std::string& log : Logs)
		{
			if (log.find(text) != std::string::npos)
			{
				return true;
			}
		}
		return false;
	}

	template <typename T>
	[[nodiscard]] std::size_t Count() const
	{
		std::lock_guard lock(Mutex);
		std::size_t count = 0;
		for (const Notification& notice : Notices)
		{
			count += std::holds_alternative<T>(notice) ? 1 : 0;
		}
		return count;
	}

	template <typename T>
	[[nodiscard]] std::vector<T> All() const
	{
		std::lock_guard lock(Mutex);
		std::vector<T> matching;
		for (const Notification& notice : Notices)
		{
			if (const T* value = std::get_if<T>(&notice))
			{
				matching.push_back(*value);
			}
		}
		return matching;
	}

private:
	mutable std::mutex Mutex;
	std::vector<Notification> Notices;
	std::vector<std::string> Logs;
	std::string Token;
};

// One endpoint driven by its reference driver over scripted sockets.
template <typename DriverType>
class DriverHarness
{
public:
	using EndpointType = typename DriverType::EndpointType;

	template <typename Config>
	explicit DriverHarness(const Config& config, DriverCallbacks callbacks = {})
		: Endpoint(config, Notifications)
		, Sockets(std::make_shared<ScriptedSocketFactory>())
		, Driver(std::make_unique<DriverType>(Endpoint, Notifications, Sockets,
											  Record.Recording(std::move(callbacks))))
	{
	}

	~DriverHarness()
	{
		Driver->Stop();
		CHECK(Driver->Join(kPatience));
	}

	DriverHarness(const DriverHarness&) = delete;
	DriverHarness& operator=(const DriverHarness&) = delete;

	// Accepts the pending connect and returns once the client sent its Hello.
	[[nodiscard]] std::shared_ptr<ScriptedConnection> Accept()
	{
		std::shared_ptr<ScriptedConnection> connection = Sockets->Accept(kPatience);
		CHECK(connection != nullptr);
		CHECK(connection->WaitIdle(kPatience));
		return connection;
	}

	void Deliver(ScriptedConnection& connection, const endpoint_test::Bytes& frame)
	{
		CHECK(connection.Deliver(frame));
		CHECK(connection.WaitIdle(kPatience));
	}

	// Starts the loop if needed, then accepts the pending attempt with a HelloOk.
	[[nodiscard]] std::shared_ptr<ScriptedConnection>
	Handshake(const endpoint_test::server::Hello& hello = {})
	{
		if (!Driver->Running())
		{
			CHECK(Driver->Start());
		}
		std::shared_ptr<ScriptedConnection> connection = Accept();
		Deliver(*connection, endpoint_test::server::HelloOk(hello));
		CHECK(Eventually(
			[this]
			{
				return Endpoint.Status().Connected;
			}));
		return connection;
	}

	NotificationQueue Notifications;
	EndpointType Endpoint;
	Recorder Record;
	const std::shared_ptr<ScriptedSocketFactory> Sockets;
	const std::unique_ptr<DriverType> Driver;
};

} // namespace driver_test
