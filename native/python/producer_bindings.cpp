#include "driver_bindings.h"

#include "openusdconnect/client/driver/socket.h"
#include "openusdconnect/client/driver/threaded_producer_driver.h"
#include "openusdconnect/client/engine/producer_endpoint.h"
#include "openusdconnect/client/frame_codec.h"

#include <nanobind/nanobind.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/shared_ptr.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <new>
#include <optional>
#include <string>
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

using PythonProducerDriver = openusdconnect::python::PythonDriver<ThreadedProducerDriver, ProducerEndpoint>;

// Python passes and receives bare envelopes; the endpoint keeps them framed.
// An envelope that cannot be framed leaves the frame empty, which the endpoint
// refuses like any incomplete frame.
[[nodiscard]] std::vector<std::uint8_t> Frame(const nb::bytes& envelope)
{
	std::vector<std::uint8_t> frame;
	static_cast<void>(
		EncodeFrame(static_cast<const std::uint8_t*>(envelope.data()), envelope.size(), frame));
	return frame;
}

[[nodiscard]] nb::bytes Envelope(const SharedByteBuffer& frame)
{
	return nb::bytes(frame->data() + kFrameHeaderSize, frame->size() - kFrameHeaderSize);
}

void BindTypes(nb::module_& module)
{
	nb::class_<ProducerConfig>(module, "ProducerConfig")
		.def(nb::init<>())
		.def_rw("host", &ProducerConfig::Host)
		.def_rw("port", &ProducerConfig::Port)
		.def_rw("client_id", &ProducerConfig::ClientId)
		.def_rw("origin", &ProducerConfig::Origin)
		.def_rw("department", &ProducerConfig::Department)
		.def_rw("layer_mode", &ProducerConfig::LayerMode)
		.def_rw("session_id", &ProducerConfig::SessionId)
		.def_rw("max_pending_transactions", &ProducerConfig::MaxPendingTransactions)
		.def_prop_rw(
			"handshake_timeout",
			[](const ProducerConfig& config)
			{
				return Seconds(config.HandshakeTimeout);
			},
			[](ProducerConfig& config, double seconds)
			{
				config.HandshakeTimeout = Milliseconds(seconds);
			});

	nb::class_<ProducerStatus>(module, "ProducerStatus")
		.def_ro("connected", &ProducerStatus::Connected)
		.def_ro("rejection", &ProducerStatus::Rejection)
		.def_ro("layer_mode_active", &ProducerStatus::LayerModeActive)
		.def_ro("metadata", &ProducerStatus::Metadata)
		.def_ro("session_id", &ProducerStatus::SessionId)
		.def_ro("pending_transactions", &ProducerStatus::PendingTransactions)
		.def_ro("pending_events", &ProducerStatus::PendingEvents)
		.def_ro("acknowledged_transactions", &ProducerStatus::AcknowledgedTransactions)
		.def_ro("acknowledged_events", &ProducerStatus::AcknowledgedEvents);

	nb::class_<MirrorCheckpoint>(module, "MirrorCheckpoint")
		.def_ro("server_instance", &MirrorCheckpoint::ServerInstance)
		.def_ro("epoch", &MirrorCheckpoint::Epoch)
		.def_ro("head_sequence", &MirrorCheckpoint::HeadSequence);

	nb::class_<TransactionFailure>(module, "TransactionFailure")
		.def_ro("transaction_id", &TransactionFailure::TransactionId)
		.def_ro("code", &TransactionFailure::Code)
		.def_ro("reason", &TransactionFailure::Reason)
		.def_ro("expected_transaction_id", &TransactionFailure::ExpectedTransactionId);

	nb::class_<ProducerSessionEntry>(module, "ProducerSessionEntry")
		.def_ro("transaction_id", &ProducerSessionEntry::TransactionId)
		.def_prop_ro("payload",
					 [](const ProducerSessionEntry& entry)
					 {
						 return Envelope(entry.Payload);
					 })
		.def_ro("event_count", &ProducerSessionEntry::EventCount)
		.def_ro("layer_key", &ProducerSessionEntry::LayerKey);

	nb::class_<RecoveryArtifact>(module, "RecoveryArtifact")
		.def_ro("session_id", &RecoveryArtifact::SessionId)
		.def_ro("failure", &RecoveryArtifact::Failure)
		.def_ro("transactions", &RecoveryArtifact::Transactions);

	nb::enum_<FlushResult>(module, "FlushResult")
		.value("FLUSHED", FlushResult::Flushed)
		.value("RECOVERY_REQUIRED", FlushResult::RecoveryRequired)
		.value("UNFINISHED", FlushResult::Unfinished);
}

void BindEndpoint(nb::module_& module)
{
	nb::class_<ProducerEndpoint>(module, "ProducerEndpoint")
		.def(
			"__init__",
			[](ProducerEndpoint* endpoint, const ProducerConfig& config,
			   NotificationQueue& notifications)
			{
				if (!ProducerEndpoint::IsValidConfiguration(config))
				{
					throw nb::value_error("invalid producer configuration");
				}
				new (endpoint) ProducerEndpoint(config, notifications);
			},
			"config"_a, "notifications"_a, nb::keep_alive<1, 3>())
		.def("status", &ProducerEndpoint::Status)
		.def(
			"request_connect",
			[](ProducerEndpoint& endpoint, std::optional<double> timeout)
			{
				// The endpoint caps the attempt at its handshake timeout.
				const std::chrono::milliseconds budget =
					timeout ? Milliseconds(*timeout) : endpoint.Configuration().HandshakeTimeout;
				const TimePoint now = std::chrono::steady_clock::now();
				return endpoint.RequestConnect(now, now + budget);
			},
			"timeout"_a = nb::none())
		.def("cancel_connect", &ProducerEndpoint::CancelConnect)
		.def("disconnect", &ProducerEndpoint::Disconnect)
		.def("next_transaction_id", &ProducerEndpoint::NextTransactionId)
		.def(
			"append",
			[](ProducerEndpoint& endpoint, std::uint64_t transaction_id, const nb::bytes& envelope,
			   std::size_t event_count, std::string layer_key)
			{
				return endpoint.Append(transaction_id, Frame(envelope), event_count,
									   std::move(layer_key));
			},
			"transaction_id"_a, "envelope"_a, "event_count"_a, "layer_key"_a = "")
		.def(
			"queue_control",
			[](ProducerEndpoint& endpoint, const nb::bytes& envelope)
			{
				return endpoint.QueueControl(Frame(envelope));
			},
			"envelope"_a)
		.def("drain_acknowledged_event_count", &ProducerEndpoint::DrainAcknowledgedEventCount)
		.def("acknowledged_checkpoint", &ProducerEndpoint::AcknowledgedCheckpoint)
		.def("failure", &ProducerEndpoint::Failure)
		.def("artifact", &ProducerEndpoint::Artifact)
		.def(
			"repair_rejected",
			[](ProducerEndpoint& endpoint, const nb::bytes& envelope, std::size_t event_count,
			   std::string layer_key)
			{
				return endpoint.RepairRejected(Frame(envelope), event_count, std::move(layer_key));
			},
			"envelope"_a, "event_count"_a, "layer_key"_a = "")
		.def("abandon_rejected_session", &ProducerEndpoint::AbandonRejectedSession,
			 "session_id"_a);
}

void BindDriver(nb::module_& module)
{
	nb::class_<PythonProducerDriver>(module, "ProducerDriver")
		.def(
			"__init__",
			[](PythonProducerDriver* driver, nb::object endpoint, nb::object notifications,
			   std::shared_ptr<SocketFactory> sockets, nb::object token_provider,
			   nb::object notification_sink, nb::object log)
			{
				new (driver) PythonProducerDriver("producer", std::move(endpoint),
												  std::move(notifications), std::move(sockets),
												  std::move(token_provider),
												  std::move(notification_sink), std::move(log));
			},
			"endpoint"_a, "notifications"_a, "sockets"_a, nb::kw_only(),
			"token_provider"_a = nb::none(), "notification_sink"_a = nb::none(),
			"log"_a = nb::none())
		.def("start", &PythonProducerDriver::Start)
		.def("stop",
			 [](PythonProducerDriver& driver)
			 {
				 driver.Get().Stop();
			 })
		.def("wake",
			 [](PythonProducerDriver& driver)
			 {
				 driver.Get().Wake();
			 })
		.def(
			"connect",
			[](PythonProducerDriver& driver, std::optional<double> timeout)
			{
				return driver.Get().Connect(Timeout(timeout));
			},
			"timeout"_a = nb::none(), nb::call_guard<nb::gil_scoped_release>())
		.def(
			"flush",
			[](PythonProducerDriver& driver, std::optional<double> timeout)
			{
				return driver.Get().Flush(Timeout(timeout));
			},
			"timeout"_a = nb::none(), nb::call_guard<nb::gil_scoped_release>());

	nb::module_::import_("atexit").attr("register")(
		nb::cpp_function(&PythonProducerDriver::StopAll));
}

} // namespace

void BindProducer(nb::module_& module)
{
	BindTypes(module);
	BindEndpoint(module);
	BindDriver(module);
}
