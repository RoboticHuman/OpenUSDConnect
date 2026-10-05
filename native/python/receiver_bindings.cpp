#include "driver_bindings.h"

#include "openusdconnect/client/driver/socket.h"
#include "openusdconnect/client/driver/threaded_receiver_driver.h"
#include "openusdconnect/client/engine/receiver_endpoint.h"

#include <nanobind/nanobind.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/shared_ptr.h>
#include <nanobind/stl/string.h>

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <new>
#include <optional>
#include <thread>
#include <utility>
#include <vector>

namespace nb = nanobind;
using namespace nb::literals;
using namespace openusdconnect::client;
using openusdconnect::python::Milliseconds;
using openusdconnect::python::Seconds;
using openusdconnect::python::Timeout;

namespace
{

using PythonReceiverDriver = openusdconnect::python::PythonDriver<ThreadedReceiverDriver, ReceiverEndpoint>;

[[nodiscard]] nb::list ToPythonBytes(const std::vector<std::vector<std::uint8_t>>& frames)
{
	nb::list result;
	for (const std::vector<std::uint8_t>& frame : frames)
	{
		result.append(nb::bytes(frame.data(), frame.size()));
	}
	return result;
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

void BindDriver(nb::module_& module)
{
	nb::class_<PythonReceiverDriver>(module, "ReceiverDriver")
		.def(
			"__init__",
			[](PythonReceiverDriver* driver, nb::object endpoint, nb::object notifications,
			   std::shared_ptr<SocketFactory> sockets, nb::object token_provider,
			   nb::object notification_sink, nb::object log)
			{
				new (driver) PythonReceiverDriver("receiver", std::move(endpoint),
												  std::move(notifications), std::move(sockets),
												  std::move(token_provider),
												  std::move(notification_sink), std::move(log));
			},
			"endpoint"_a, "notifications"_a, "sockets"_a, nb::kw_only(),
			"token_provider"_a = nb::none(), "notification_sink"_a = nb::none(),
			"log"_a = nb::none())
		.def("start", &PythonReceiverDriver::Start)
		.def("stop",
			 [](PythonReceiverDriver& driver)
			 {
				 driver.Get().Stop();
			 })
		.def("wake",
			 [](PythonReceiverDriver& driver)
			 {
				 driver.Get().Wake();
			 })
		.def(
			"join",
			[](PythonReceiverDriver& driver, std::optional<double> timeout)
			{
				return driver.Get().Join(Timeout(timeout));
			},
			"timeout"_a = nb::none(), nb::call_guard<nb::gil_scoped_release>())
		.def(
			"wait_connected",
			[](PythonReceiverDriver& driver, std::optional<double> timeout)
			{
				return driver.Get().WaitConnected(Timeout(timeout));
			},
			"timeout"_a = nb::none(), nb::call_guard<nb::gil_scoped_release>())
		.def(
			"wait_synchronized",
			[](PythonReceiverDriver& driver, std::optional<double> timeout)
			{
				return driver.Get().WaitSynchronized(Timeout(timeout));
			},
			"timeout"_a = nb::none(), nb::call_guard<nb::gil_scoped_release>())
		.def_prop_ro("running",
					 [](PythonReceiverDriver& driver)
					 {
						 return driver.Get().Running();
					 })
		.def_prop_ro("stopped",
					 [](PythonReceiverDriver& driver)
					 {
						 return driver.Get().Stopped();
					 })
		.def_prop_ro("ident",
					 [](PythonReceiverDriver& driver) -> std::optional<std::size_t>
					 {
						 const std::optional<std::thread::id> id = driver.Get().ThreadId();
						 return id ? std::optional(std::hash<std::thread::id>()(*id))
								   : std::nullopt;
					 })
		.def_prop_ro("last_failure",
					 [](PythonReceiverDriver& driver)
					 {
						 return driver.Get().LastFailure();
					 });

	nb::module_::import_("atexit").attr("register")(
		nb::cpp_function(&PythonReceiverDriver::StopAll));
}

} // namespace

void BindReceiver(nb::module_& module)
{
	BindEndpoint(module);
	BindDriver(module);
}
