#pragma once

#include "openusdconnect/client/driver/socket.h"
#include "openusdconnect/client/engine/notification.h"

#include <nanobind/nanobind.h>
#include <nanobind/stl/optional.h>

#include <chrono>
#include <cstddef>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <thread>
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

// Binds a duration field as a property in seconds.
template <typename Config>
void DurationProperty(nb::class_<Config>& cls, const char* name,
					  std::chrono::milliseconds Config::* member)
{
	cls.def_prop_rw(
		name,
		[member](const Config& config)
		{
			return Seconds(config.*member);
		},
		[member](Config& config, double seconds)
		{
			config.*member = Milliseconds(seconds);
		});
}

[[nodiscard]] nb::object ToPython(client::Notification notification);

// Owns a reference driver whose thread calls Python, and closes it when
// destroyed, so the loop writes what was queued. Every Python object here is
// touched only with the GIL, and the GIL is released around every wait.
template <typename Driver>
class PythonDriver final
{
public:
	using Endpoint = typename Driver::EndpointType;

	// role names the callbacks in unraisable-exception reports.
	PythonDriver(const std::string& role, Endpoint& endpoint,
				 client::NotificationQueue& notifications,
				 std::shared_ptr<client::SocketFactory> sockets, nb::object token_provider,
				 nb::object token_issued, nb::object notification_sink, nb::object log)
		: TokenContext(role + " token provider")
		, IssuedContext(role + " token issued hook")
		, SinkContext(role + " notification sink")
		, LogContext(role + " log")
		, TokenProvider(std::move(token_provider))
		, IssuedHook(std::move(token_issued))
		, Sink(std::move(notification_sink))
		, LogCallback(std::move(log))
		, Native(std::make_unique<Driver>(endpoint, notifications, std::move(sockets), Callbacks()))
	{
		std::lock_guard lock(RegistryMutex());
		Registry().insert(this);
	}

	~PythonDriver()
	{
		Destroying = true;
		{
			std::lock_guard lock(RegistryMutex());
			Registry().erase(this);
		}
		nb::gil_scoped_release release;
		static_cast<void>(Native->Close(std::nullopt));
	}

	PythonDriver(const PythonDriver&) = delete;
	PythonDriver& operator=(const PythonDriver&) = delete;

	[[nodiscard]] Driver& Get() noexcept
	{
		return *Native;
	}

	// Runs at interpreter exit, before threads may no longer take the GIL.
	static void StopAll()
	{
		std::vector<std::pair<nb::object, Driver*>> drivers;
		{
			std::lock_guard lock(RegistryMutex());
			for (PythonDriver* driver : Registry())
			{
				drivers.emplace_back(nb::find(driver), driver->Native.get());
			}
		}
		nb::gil_scoped_release release;
		for (const auto& [object, driver] : drivers)
		{
			static_cast<void>(driver->Close(std::nullopt));
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
			// Anything but a str abandons the connection attempt.
			callbacks.Token = [this]
			{
				std::optional<std::string> token;
				CallPython(TokenContext,
						   [&]
						   {
							   std::string text;
							   if (nb::try_cast(TokenProvider(), text))
							   {
								   token = std::move(text);
							   }
						   });
				return token;
			};
		}
		if (!IssuedHook.is_none())
		{
			callbacks.TokenIssued = [this](const std::string& token)
			{
				CallPython(IssuedContext,
						   [&]
						   {
							   IssuedHook(token);
						   });
			};
		}
		if (!Sink.is_none())
		{
			callbacks.Notifications = [this](client::Notification notification)
			{
				CallPython(SinkContext,
						   [&]
						   {
							   Sink(ToPython(std::move(notification)));
						   });
			};
		}
		if (!LogCallback.is_none())
		{
			callbacks.Log = [this](client::LogLevel level, const std::string& message)
			{
				CallPython(LogContext,
						   [&]
						   {
							   LogCallback(level, message);
						   });
			};
		}
		return callbacks;
	}

	// Runs call, which calls into Python, on the driver thread with the GIL
	// held; a Python exception is reported as unraisable under context.
	template <typename Call>
	void CallPython(const std::string& context, Call call)
	{
		nb::gil_scoped_acquire gil;
		// Python may drop every other reference to this driver meanwhile, and
		// destroying it on its own thread would join that thread from itself.
		nb::object self = Destroying ? nb::object() : nb::find(this);
		try
		{
			call();
		}
		catch (nb::python_error& error)
		{
			error.discard_as_unraisable(context.c_str());
		}
		if (self.is_valid() && Py_REFCNT(self.ptr()) == 1)
		{
			// Its owner is gone: stop now, and destroy it on the main thread. A
			// full pending-call queue leaks the stopped driver instead.
			Native->Stop();
			static_cast<void>(Py_AddPendingCall(
				[](void* object)
				{
					nb::handle(static_cast<PyObject*>(object)).dec_ref();
					return 0;
				},
				self.release().ptr()));
		}
	}

	const std::string TokenContext;
	const std::string IssuedContext;
	const std::string SinkContext;
	const std::string LogContext;
	nb::object TokenProvider;
	nb::object IssuedHook;
	nb::object Sink;
	nb::object LogCallback;
	bool Destroying = false;
	// Destroyed first, so its thread has exited before the objects above go.
	const std::unique_ptr<Driver> Native;
};

// Binds the lifecycle every reference driver shares; a role adds its own methods.
template <typename Driver>
void BindThreadedDriver(nb::class_<PythonDriver<Driver>>& cls)
{
	using Bound = PythonDriver<Driver>;
	cls.def("start",
			[](Bound& driver)
			{
				return driver.Get().Start();
			})
		.def("stop",
			 [](Bound& driver)
			 {
				 driver.Get().Stop();
			 })
		.def("wake",
			 [](Bound& driver)
			 {
				 driver.Get().Wake();
			 })
		.def(
			"join",
			[](Bound& driver, std::optional<double> timeout)
			{
				return driver.Get().Join(Timeout(timeout));
			},
			nb::arg("timeout") = nb::none(), nb::call_guard<nb::gil_scoped_release>())
		.def(
			"close",
			[](Bound& driver, std::optional<double> timeout)
			{
				return driver.Get().Close(Timeout(timeout));
			},
			nb::arg("timeout") = nb::none(), nb::call_guard<nb::gil_scoped_release>())
		.def_prop_ro("running",
					 [](Bound& driver)
					 {
						 return driver.Get().Running();
					 })
		.def_prop_ro("stopped",
					 [](Bound& driver)
					 {
						 return driver.Get().Stopped();
					 })
		.def_prop_ro("ident",
					 [](Bound& driver) -> std::optional<std::size_t>
					 {
						 const std::optional<std::thread::id> id = driver.Get().ThreadId();
						 return id ? std::optional(std::hash<std::thread::id>()(*id))
								   : std::nullopt;
					 })
		.def_prop_ro("last_failure",
					 [](Bound& driver)
					 {
						 return driver.Get().LastFailure();
					 });
	nb::module_::import_("atexit").attr("register")(nb::cpp_function(&Bound::StopAll));
}

} // namespace openusdconnect::python
