#include "driver_bindings.h"

#include <nanobind/stl/optional.h>
#include <nanobind/stl/string.h>

#include <chrono>
#include <optional>
#include <utility>
#include <variant>

namespace openusdconnect::python
{
namespace
{

using namespace client;
using OpenUSDConnect::LayerMode;

// Bounds every wait so a caller-supplied number cannot overflow a deadline.
constexpr std::chrono::hours kLongestWait{24 * 365};

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
		.value("PROTOCOL_ERROR", DisconnectReason::ProtocolError)
		.value("CANCELLED", DisconnectReason::Cancelled)
		.value("HANDSHAKE_TIMEOUT", DisconnectReason::HandshakeTimeout)
		.value("RECOVERY_REQUIRED", DisconnectReason::RecoveryRequired)
		.value("RATE_LIMITED", DisconnectReason::RateLimited);

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

void BindTransport(nb::module_& module)
{
	nb::class_<SocketFactory>(module, "SocketFactory");

	nb::class_<TcpSocketFactory, SocketFactory>(module, "TcpSocketFactory").def(nb::init<>());

	nb::class_<TransportFailure>(module, "TransportFailure")
		.def_ro("operation", &TransportFailure::Operation)
		.def_ro("result", &TransportFailure::Result)
		.def_ro("system_error", &TransportFailure::SystemError)
		.def_prop_ro("description",
					 [](const TransportFailure& failure)
					 {
						 return Describe(failure);
					 });
}

} // namespace

void BindDriverTypes(nb::module_& module)
{
	BindEnums(module);
	BindNotifications(module);
	BindTransport(module);
}

std::chrono::milliseconds Milliseconds(double seconds)
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

double Seconds(std::chrono::milliseconds duration)
{
	return std::chrono::duration<double>(duration).count();
}

std::optional<std::chrono::milliseconds> Timeout(std::optional<double> seconds)
{
	return seconds ? std::optional(Milliseconds(*seconds)) : std::nullopt;
}

nb::object ToPython(Notification notification)
{
	return std::visit(
		[](auto&& value)
		{
			return nb::cast(std::move(value));
		},
		std::move(notification));
}

} // namespace openusdconnect::python
