#pragma once

#include "openusdconnect/client/driver/socket.h"
#include "openusdconnect/client/engine/notification.h"

#include <nanobind/nanobind.h>

#include <chrono>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

// What the receiver and producer bindings share: durations in seconds, the
// notification types, and the Python side of a reference driver.
namespace openusdconnect::python
{

namespace nb = nanobind;

// Binds the types both roles use. Call before either role's bindings.
void BindDriverTypes(nb::module_& module);

// Seconds from Python, clamped to zero and bounded so no deadline overflows.
[[nodiscard]] std::chrono::milliseconds Milliseconds(double seconds);
[[nodiscard]] double Seconds(std::chrono::milliseconds duration);
[[nodiscard]] std::optional<std::chrono::milliseconds> Timeout(std::optional<double> seconds);

[[nodiscard]] nb::object ToPython(client::Notification notification);

// Owns a reference driver whose thread calls Python. Every Python object here
// is touched only with the GIL, and the GIL is released around every wait.
template <typename Driver, typename Endpoint>
class PythonDriver final
{
public:
	// role names the callbacks in unraisable-exception reports.
	PythonDriver(const std::string& role, nb::object endpoint, nb::object notifications,
				 std::shared_ptr<client::SocketFactory> sockets, nb::object token_provider,
				 nb::object notification_sink, nb::object log)
		: TokenContext(role + " token provider")
		, SinkContext(role + " notification sink")
		, LogContext(role + " log")
		, EndpointObject(std::move(endpoint))
		, QueueObject(std::move(notifications))
		, TokenProvider(std::move(token_provider))
		, Sink(std::move(notification_sink))
		, LogCallback(std::move(log))
		, Native(std::make_unique<Driver>(nb::cast<Endpoint&>(EndpointObject),
										  nb::cast<client::NotificationQueue&>(QueueObject),
										  std::move(sockets), Callbacks()))
	{
		std::lock_guard lock(RegistryMutex());
		Registry().insert(this);
	}

	~PythonDriver()
	{
		{
			nb::gil_scoped_release release;
			Native->Stop();
			static_cast<void>(Native->Join());
		}
		std::lock_guard lock(RegistryMutex());
		Registry().erase(this);
	}

	PythonDriver(const PythonDriver&) = delete;
	PythonDriver& operator=(const PythonDriver&) = delete;

	// The running thread keeps this object alive, as Python keeps a running
	// thread; its Exited callback, which needs the GIL held here, releases it.
	[[nodiscard]] bool Start()
	{
		if (!Native->Start())
		{
			return false;
		}
		Self = nb::find(this);
		return true;
	}

	[[nodiscard]] Driver& Get() noexcept
	{
		return *Native;
	}

	// Runs at interpreter exit, before threads may no longer take the GIL.
	static void StopAll()
	{
		std::vector<std::pair<nb::object, Driver*>> running;
		{
			std::lock_guard lock(RegistryMutex());
			for (PythonDriver* driver : Registry())
			{
				if (driver->Self.is_valid())
				{
					running.emplace_back(driver->Self, driver->Native.get());
				}
			}
		}
		nb::gil_scoped_release release;
		for (const auto& [object, driver] : running)
		{
			driver->Stop();
		}
		for (const auto& [object, driver] : running)
		{
			static_cast<void>(driver->Join());
		}
	}

private:
	[[nodiscard]] static std::mutex& RegistryMutex()
	{
		static std::mutex mutex;
		return mutex;
	}

	[[nodiscard]] static std::set<PythonDriver*>& Registry()
	{
		static std::set<PythonDriver*> drivers;
		return drivers;
	}

	[[nodiscard]] client::DriverCallbacks Callbacks()
	{
		client::DriverCallbacks callbacks;
		if (!TokenProvider.is_none())
		{
			callbacks.Token = [this]
			{
				return ReadToken();
			};
		}
		if (!Sink.is_none())
		{
			callbacks.Notifications = [this](client::Notification notification)
			{
				Notify(std::move(notification));
			};
		}
		if (!LogCallback.is_none())
		{
			callbacks.Log = [this](client::LogLevel level, const std::string& message)
			{
				Log(level, message);
			};
		}
		callbacks.Exited = [this]
		{
			Exit();
		};
		return callbacks;
	}

	// Anything but a str abandons the connection attempt.
	[[nodiscard]] std::optional<std::string> ReadToken()
	{
		nb::gil_scoped_acquire gil;
		try
		{
			std::string token;
			if (nb::try_cast(TokenProvider(), token))
			{
				return token;
			}
		}
		catch (nb::python_error& error)
		{
			error.discard_as_unraisable(TokenContext.c_str());
		}
		return std::nullopt;
	}

	void Notify(client::Notification notification)
	{
		nb::gil_scoped_acquire gil;
		try
		{
			Sink(ToPython(std::move(notification)));
		}
		catch (nb::python_error& error)
		{
			error.discard_as_unraisable(SinkContext.c_str());
		}
	}

	void Log(client::LogLevel level, const std::string& message)
	{
		nb::gil_scoped_acquire gil;
		try
		{
			LogCallback(level, message);
		}
		catch (nb::python_error& error)
		{
			error.discard_as_unraisable(LogContext.c_str());
		}
	}

	void Exit()
	{
		nb::gil_scoped_acquire gil;
		TokenProvider = nb::none();
		Sink = nb::none();
		LogCallback = nb::none();
		// Released last: it may destroy this object.
		const nb::object self = std::move(Self);
	}

	const std::string TokenContext;
	const std::string SinkContext;
	const std::string LogContext;
	nb::object EndpointObject;
	nb::object QueueObject;
	nb::object TokenProvider;
	nb::object Sink;
	nb::object LogCallback;
	nb::object Self;
	// Destroyed first, so its thread has exited before the objects above go.
	const std::unique_ptr<Driver> Native;
};

} // namespace openusdconnect::python
