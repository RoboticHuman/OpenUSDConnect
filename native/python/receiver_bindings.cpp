#include "openusdconnect/client/driver/socket.h"
#include "openusdconnect/client/driver/threaded_receiver_driver.h"
#include "openusdconnect/client/engine/receiver_endpoint.h"

#include <nanobind/nanobind.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/shared_ptr.h>
#include <nanobind/stl/string.h>

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <new>
#include <optional>
#include <set>
#include <string>
#include <thread>
#include <utility>
#include <variant>
#include <vector>

namespace nb = nanobind;
using namespace nb::literals;
using namespace openusdconnect::client;
using OpenUSDConnect::LayerMode;

namespace
{

// Bounds every wait so a caller-supplied number cannot overflow a deadline.
constexpr std::chrono::hours kLongestWait{24 * 365};

[[nodiscard]] std::chrono::milliseconds Milliseconds(double seconds)
{
	if (!(seconds > 0.0))
	{
		return std::chrono::milliseconds::zero();
	}
	const std::chrono::duration<double> requested(seconds);
	if (requested >= kLongestWait)
	{
		return kLongestWait;
	}
	return std::chrono::ceil<std::chrono::milliseconds>(requested);
}

[[nodiscard]] double Seconds(std::chrono::milliseconds duration)
{
	return std::chrono::duration<double>(duration).count();
}

[[nodiscard]] std::optional<std::chrono::milliseconds> Timeout(std::optional<double> seconds)
{
	return seconds ? std::optional(Milliseconds(*seconds)) : std::nullopt;
}

[[nodiscard]] nb::list ToPythonBytes(const std::vector<std::vector<std::uint8_t>>& frames)
{
	nb::list result;
	for (const std::vector<std::uint8_t>& frame : frames)
	{
		result.append(nb::bytes(frame.data(), frame.size()));
	}
	return result;
}

[[nodiscard]] nb::object ToPython(Notification notification)
{
	return std::visit(
		[](auto&& value)
		{
			return nb::cast(std::move(value));
		},
		std::move(notification));
}

// Owns a driver whose thread calls Python. Every Python object here is
// touched only with the GIL, and the GIL is released around every wait.
class PythonReceiverDriver final
{
public:
	PythonReceiverDriver(nb::object endpoint, nb::object notifications,
						 std::shared_ptr<SocketFactory> sockets, nb::object token_provider,
						 nb::object notification_sink, nb::object log)
		: EndpointObject(std::move(endpoint))
		, QueueObject(std::move(notifications))
		, TokenProvider(std::move(token_provider))
		, Sink(std::move(notification_sink))
		, LogCallback(std::move(log))
		, Native(std::make_unique<ThreadedReceiverDriver>(
			  nb::cast<ReceiverEndpoint&>(EndpointObject),
			  nb::cast<NotificationQueue&>(QueueObject), std::move(sockets), Callbacks()))
	{
		std::lock_guard lock(RegistryMutex());
		Registry().insert(this);
	}

	~PythonReceiverDriver()
	{
		{
			nb::gil_scoped_release release;
			Native->Stop();
			static_cast<void>(Native->Join());
		}
		std::lock_guard lock(RegistryMutex());
		Registry().erase(this);
	}

	PythonReceiverDriver(const PythonReceiverDriver&) = delete;
	PythonReceiverDriver& operator=(const PythonReceiverDriver&) = delete;

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

	[[nodiscard]] ThreadedReceiverDriver& Driver() noexcept
	{
		return *Native;
	}

	// Runs at interpreter exit, before threads may no longer take the GIL.
	static void StopAll()
	{
		std::vector<std::pair<nb::object, ThreadedReceiverDriver*>> running;
		{
			std::lock_guard lock(RegistryMutex());
			for (PythonReceiverDriver* driver : Registry())
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

	[[nodiscard]] static std::set<PythonReceiverDriver*>& Registry()
	{
		static std::set<PythonReceiverDriver*> drivers;
		return drivers;
	}

	[[nodiscard]] DriverCallbacks Callbacks()
	{
		DriverCallbacks callbacks;
		if (!TokenProvider.is_none())
		{
			callbacks.Token = [this]
			{
				return ReadToken();
			};
		}
		if (!Sink.is_none())
		{
			callbacks.Notifications = [this](Notification notification)
			{
				Notify(std::move(notification));
			};
		}
		if (!LogCallback.is_none())
		{
			callbacks.Log = [this](LogLevel level, const std::string& message)
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
			error.discard_as_unraisable("receiver token provider");
		}
		return std::nullopt;
	}

	void Notify(Notification notification)
	{
		nb::gil_scoped_acquire gil;
		try
		{
			Sink(ToPython(std::move(notification)));
		}
		catch (nb::python_error& error)
		{
			error.discard_as_unraisable("receiver notification sink");
		}
	}

	void Log(LogLevel level, const std::string& message)
	{
		nb::gil_scoped_acquire gil;
		try
		{
			LogCallback(level, message);
		}
		catch (nb::python_error& error)
		{
			error.discard_as_unraisable("receiver log");
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

	nb::object EndpointObject;
	nb::object QueueObject;
	nb::object TokenProvider;
	nb::object Sink;
	nb::object LogCallback;
	nb::object Self;
	// Destroyed first, so its thread has exited before the objects above go.
	const std::unique_ptr<ThreadedReceiverDriver> Native;
};

void BindEnums(nb::module_& module)
{
	nb::enum_<LogLevel>(module, "LogLevel")
		.value("DEBUG", LogLevel::Debug)
		.value("INFO", LogLevel::Info)
		.value("WARNING", LogLevel::Warning)
		.value("ERROR", LogLevel::Error);

	nb::enum_<LayerMode>(module, "LayerMode")
		.value("MANAGED", LayerMode::Managed)
		.value("SHARED_STAGE", LayerMode::SharedStage);

	nb::enum_<DisconnectReason>(module, "DisconnectReason")
		.value("CONNECT_FAILED", DisconnectReason::ConnectFailed)
		.value("PEER_CLOSED", DisconnectReason::PeerClosed)
		.value("TRANSPORT_ERROR", DisconnectReason::TransportError)
		.value("STOPPED", DisconnectReason::Stopped)
		.value("HANDSHAKE_REJECTED", DisconnectReason::HandshakeRejected)
		.value("REPLAY_REQUESTED", DisconnectReason::ReplayRequested)
		.value("SEQUENCE_GAP", DisconnectReason::SequenceGap)
		.value("QUEUE_FULL", DisconnectReason::QueueFull)
		.value("READ_TIMEOUT", DisconnectReason::ReadTimeout)
		.value("PROTOCOL_ERROR", DisconnectReason::ProtocolError);

	nb::enum_<SocketResult>(module, "SocketResult")
		.value("SUCCESS", SocketResult::Success)
		.value("TIMEOUT", SocketResult::Timeout)
		.value("INTERRUPTED", SocketResult::Interrupted)
		.value("CLOSED", SocketResult::Closed)
		.value("FAILED", SocketResult::Failed);

	nb::enum_<SocketOperation>(module, "SocketOperation")
		.value("CONNECT", SocketOperation::Connect)
		.value("SEND", SocketOperation::Send)
		.value("RECEIVE", SocketOperation::Receive);
}

void BindNotifications(nb::module_& module)
{
	nb::class_<Connected>(module, "Connected");

	nb::class_<Disconnected>(module, "Disconnected").def_ro("reason", &Disconnected::Reason);

	nb::class_<HandshakeRejected>(module, "HandshakeRejected")
		.def_ro("authentication", &HandshakeRejected::Authentication)
		.def_prop_ro("code",
					 [](const HandshakeRejected& rejected)
					 {
						 return static_cast<int>(rejected.Code);
					 })
		.def_ro("reason", &HandshakeRejected::Reason);

	nb::class_<TokenIssued>(module, "TokenIssued").def_ro("token", &TokenIssued::Token);

	nb::class_<StageMetadata>(module, "StageMetadata")
		.def_ro("time_codes_per_second", &StageMetadata::TimeCodesPerSecond)
		.def_ro("frames_per_second", &StageMetadata::FramesPerSecond)
		.def_ro("start_time_code", &StageMetadata::StartTimeCode)
		.def_ro("end_time_code", &StageMetadata::EndTimeCode)
		.def_ro("meters_per_unit", &StageMetadata::MetersPerUnit)
		.def_ro("up_axis", &StageMetadata::UpAxis);

	nb::class_<PlaybackState>(module, "PlaybackState")
		.def_ro("time", &PlaybackState::Time)
		.def_ro("playing", &PlaybackState::Playing)
		.def_ro("rate", &PlaybackState::Rate)
		.def_ro("leader_client_id", &PlaybackState::LeaderClientId);

	nb::class_<PlaybackClaimed>(module, "PlaybackClaimed")
		.def_ro("leader_client_id", &PlaybackClaimed::LeaderClientId);

	nb::class_<PlaybackRejected>(module, "PlaybackRejected")
		.def_ro("reason", &PlaybackRejected::Reason)
		.def_ro("current_leader_client_id", &PlaybackRejected::CurrentLeaderClientId);

	nb::class_<NotificationQueue>(module, "NotificationQueue")
		.def(nb::init<>())
		.def("drain",
			 [](NotificationQueue& queue)
			 {
				 nb::list result;
				 for (Notification& notification : queue.Drain())
				 {
					 result.append(ToPython(std::move(notification)));
				 }
				 return result;
			 });
}

void BindEndpoint(nb::module_& module)
{
	nb::class_<ReceiverConfig>(module, "ReceiverConfig")
		.def(nb::init<>())
		.def_rw("host", &ReceiverConfig::Host)
		.def_rw("port", &ReceiverConfig::Port)
		.def_rw("client_id", &ReceiverConfig::ClientId)
		.def_rw("origin", &ReceiverConfig::Origin)
		.def_rw("department", &ReceiverConfig::Department)
		.def_rw("layered_replay", &ReceiverConfig::LayeredReplay)
		.def_rw("layer_mode", &ReceiverConfig::LayerMode)
		.def_rw("sync_from", &ReceiverConfig::SyncFrom)
		.def_rw("max_queue", &ReceiverConfig::MaxQueue)
		.def_rw("max_consecutive_timeouts", &ReceiverConfig::MaxConsecutiveTimeouts)
		.def_rw("reconnect", &ReceiverConfig::Reconnect)
		.def_prop_rw(
			"socket_timeout",
			[](const ReceiverConfig& config)
			{
				return Seconds(config.SocketTimeout);
			},
			[](ReceiverConfig& config, double seconds)
			{
				config.SocketTimeout = Milliseconds(seconds);
			})
		.def_prop_rw(
			"reconnect_base_delay",
			[](const ReceiverConfig& config)
			{
				return Seconds(config.ReconnectBaseDelay);
			},
			[](ReceiverConfig& config, double seconds)
			{
				config.ReconnectBaseDelay = Milliseconds(seconds);
			})
		.def_prop_rw(
			"reconnect_max_delay",
			[](const ReceiverConfig& config)
			{
				return Seconds(config.ReconnectMaxDelay);
			},
			[](ReceiverConfig& config, double seconds)
			{
				config.ReconnectMaxDelay = Milliseconds(seconds);
			});

	nb::class_<ReceiverStatus>(module, "ReceiverStatus")
		.def_ro("connected", &ReceiverStatus::Connected)
		.def_ro("synchronized", &ReceiverStatus::Synchronized)
		.def_ro("stopped", &ReceiverStatus::Stopped)
		.def_ro("replay_head_sequence", &ReceiverStatus::ReplayHeadSequence)
		.def_ro("replay_epoch", &ReceiverStatus::ReplayEpoch)
		.def_ro("server_instance", &ReceiverStatus::ServerInstance)
		.def_ro("layered_replay_active", &ReceiverStatus::LayeredReplayActive)
		.def_ro("layer_mode_active", &ReceiverStatus::LayerModeActive)
		.def_ro("rejection", &ReceiverStatus::Rejection)
		.def_ro("metadata", &ReceiverStatus::Metadata)
		.def_ro("queued_frames", &ReceiverStatus::QueuedFrames)
		.def_ro("last_sequence", &ReceiverStatus::LastSequence)
		.def_ro("last_applied_sequence", &ReceiverStatus::LastAppliedSequence);

	nb::class_<ReceiverEndpoint>(module, "ReceiverEndpoint")
		.def(
			"__init__",
			[](ReceiverEndpoint* endpoint, const ReceiverConfig& config,
			   NotificationQueue& notifications)
			{
				if (!ReceiverEndpoint::IsValidConfiguration(config))
				{
					throw nb::value_error("invalid receiver configuration");
				}
				new (endpoint) ReceiverEndpoint(config, notifications);
			},
			"config"_a, "notifications"_a, nb::keep_alive<1, 3>())
		.def("status", &ReceiverEndpoint::Status)
		.def("stop", &ReceiverEndpoint::Stop)
		.def("set_reconnect", &ReceiverEndpoint::SetReconnect, "enabled"_a)
		.def(
			"drain_frames",
			[](ReceiverEndpoint& endpoint, std::optional<std::size_t> max_frames)
			{
				if (max_frames && *max_frames == 0)
				{
					throw nb::value_error("max_frames must be non-zero when specified");
				}
				return ToPythonBytes(endpoint.DrainFrames(max_frames));
			},
			"max_frames"_a = nb::none())
		.def_prop_ro("generation", &ReceiverEndpoint::Generation)
		.def("mark_applied_through", &ReceiverEndpoint::MarkAppliedThrough, "generation"_a,
			 "sequence"_a)
		.def("reset_applied_progress", &ReceiverEndpoint::ResetAppliedProgress)
		.def("mark_replay_applied", &ReceiverEndpoint::MarkReplayApplied)
		.def("request_replay_from", &ReceiverEndpoint::RequestReplayFrom, "sequence"_a)
		.def("freeze_marker", &ReceiverEndpoint::FreezeMarker)
		.def("drained_through", &ReceiverEndpoint::DrainedThrough, "marker"_a);
}

void BindSockets(nb::module_& module)
{
	nb::class_<SocketFactory>(module, "SocketFactory");

	nb::class_<TcpSocketFactory, SocketFactory>(module, "TcpSocketFactory").def(nb::init<>());
}

void BindDriver(nb::module_& module)
{
	nb::class_<TransportFailure>(module, "TransportFailure")
		.def_ro("operation", &TransportFailure::Operation)
		.def_ro("result", &TransportFailure::Result)
		.def_ro("system_error", &TransportFailure::SystemError)
		.def_prop_ro("description",
					 [](const TransportFailure& failure)
					 {
						 return Describe(failure);
					 });

	nb::class_<PythonReceiverDriver>(module, "ReceiverDriver")
		.def(nb::init<nb::object, nb::object, std::shared_ptr<SocketFactory>, nb::object,
					  nb::object, nb::object>(),
			 "endpoint"_a, "notifications"_a, "sockets"_a, nb::kw_only(),
			 "token_provider"_a = nb::none(), "notification_sink"_a = nb::none(),
			 "log"_a = nb::none())
		.def("start", &PythonReceiverDriver::Start)
		.def("stop",
			 [](PythonReceiverDriver& driver)
			 {
				 driver.Driver().Stop();
			 })
		.def("wake",
			 [](PythonReceiverDriver& driver)
			 {
				 driver.Driver().Wake();
			 })
		.def(
			"join",
			[](PythonReceiverDriver& driver, std::optional<double> timeout)
			{
				return driver.Driver().Join(Timeout(timeout));
			},
			"timeout"_a = nb::none(), nb::call_guard<nb::gil_scoped_release>())
		.def(
			"wait_connected",
			[](PythonReceiverDriver& driver, std::optional<double> timeout)
			{
				return driver.Driver().WaitConnected(Timeout(timeout));
			},
			"timeout"_a = nb::none(), nb::call_guard<nb::gil_scoped_release>())
		.def(
			"wait_synchronized",
			[](PythonReceiverDriver& driver, std::optional<double> timeout)
			{
				return driver.Driver().WaitSynchronized(Timeout(timeout));
			},
			"timeout"_a = nb::none(), nb::call_guard<nb::gil_scoped_release>())
		.def_prop_ro("running",
					 [](PythonReceiverDriver& driver)
					 {
						 return driver.Driver().Running();
					 })
		.def_prop_ro("stopped",
					 [](PythonReceiverDriver& driver)
					 {
						 return driver.Driver().Stopped();
					 })
		.def_prop_ro("ident",
					 [](PythonReceiverDriver& driver) -> std::optional<std::size_t>
					 {
						 const std::optional<std::thread::id> id = driver.Driver().ThreadId();
						 return id ? std::optional(std::hash<std::thread::id>()(*id))
								   : std::nullopt;
					 })
		.def_prop_ro("last_failure",
					 [](PythonReceiverDriver& driver)
					 {
						 return driver.Driver().LastFailure();
					 });

	nb::module_::import_("atexit").attr("register")(
		nb::cpp_function(&PythonReceiverDriver::StopAll));
}

} // namespace

void BindReceiver(nb::module_& module)
{
	BindEnums(module);
	BindNotifications(module);
	BindEndpoint(module);
	BindSockets(module);
	BindDriver(module);
}
