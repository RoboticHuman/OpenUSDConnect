#include "openusdconnect/client/frame_codec.h"
#include "openusdconnect/client/producer_session.h"
#include "openusdconnect/client/receiver_session.h"

#include <nanobind/nanobind.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/string.h>

#include <cstdint>
#include <memory>
#include <new>
#include <optional>
#include <stdexcept>
#include <utility>
#include <vector>

namespace nb = nanobind;
using namespace nb::literals;
using openusdconnect::client::AcceptResult;
using openusdconnect::client::ConnectionStart;
using openusdconnect::client::FrameDecoder;
using openusdconnect::client::FrameResult;
using openusdconnect::client::ProducerConnectionStart;
using openusdconnect::client::ProducerPhase;
using openusdconnect::client::ProducerRecoveryDisposition;
using openusdconnect::client::ProducerResult;
using openusdconnect::client::ReceiverMessageKind;

namespace
{

using PythonPayload = nb::object;
using PythonProducerSession = openusdconnect::client::OrderedProducerSession<PythonPayload>;
using PythonReceiverInbox = openusdconnect::client::OrderedReceiverSession<PythonPayload>;

class PythonFrameError final : public std::runtime_error
{
public:
	using std::runtime_error::runtime_error;
};

[[noreturn]] void RaiseFrameError(FrameResult result)
{
	switch (result)
	{
	case FrameResult::InvalidMaxFrameSize:
		throw PythonFrameError("max frame size must fit in a non-zero uint32");
	case FrameResult::EmptyPayload:
		throw PythonFrameError("frame payload must not be empty");
	case FrameResult::PayloadTooLarge:
		throw PythonFrameError("frame payload exceeds the configured limit");
	case FrameResult::InvalidHeader:
		throw PythonFrameError("frame payload size is invalid");
	case FrameResult::Success:
		break;
	}
	throw std::logic_error("invalid frame result");
}

[[nodiscard]] nb::list ToPythonBytes(std::vector<std::vector<std::uint8_t>> values)
{
	nb::list result;
	for (const auto& value : values)
	{
		result.append(nb::bytes(value.data(), value.size()));
	}
	return result;
}

[[nodiscard]] nb::list ToPythonBytes(std::vector<PythonPayload> values)
{
	nb::list result;
	for (const auto& value : values)
	{
		result.append(value);
	}
	return result;
}

void ValidateBytes(nb::handle value, const char* empty_message)
{
	if (!PyBytes_Check(value.ptr()))
	{
		throw nb::type_error("payload must be bytes");
	}
	if (PyBytes_Size(value.ptr()) == 0)
	{
		throw std::invalid_argument(empty_message);
	}
}

} // namespace

NB_MODULE(_native_client, module)
{
	module.doc() = "Native OpenUSDConnect client primitives";
	module.attr("DEFAULT_MAX_FRAME_SIZE") = openusdconnect::client::kDefaultMaxFrameSize;

	nb::exception<PythonFrameError>(module, "FrameError", PyExc_ValueError);

	nb::enum_<ReceiverMessageKind>(module, "ReceiverMessageKind")
		.value("EVENT", ReceiverMessageKind::Event)
		.value("LAYER_GRAPH_STATE", ReceiverMessageKind::LayerGraphState)
		.value("RESYNC", ReceiverMessageKind::Resync)
		.value("OTHER", ReceiverMessageKind::Other);

	nb::enum_<AcceptResult>(module, "AcceptResult")
		.value("ACCEPTED", AcceptResult::Accepted)
		.value("STALE_GENERATION", AcceptResult::StaleGeneration)
		.value("QUEUE_FULL", AcceptResult::QueueFull)
		.value("DUPLICATE", AcceptResult::Duplicate)
		.value("SEQUENCE_GAP", AcceptResult::SequenceGap)
		.value("INVALID_SEQUENCE", AcceptResult::InvalidSequence);

	nb::enum_<ProducerPhase>(module, "ProducerPhase")
		.value("DISCONNECTED", ProducerPhase::Disconnected)
		.value("AWAITING_HELLO", ProducerPhase::AwaitingHello)
		.value("READY", ProducerPhase::Ready)
		.value("RECOVERY_REQUIRED", ProducerPhase::RecoveryRequired);

	nb::enum_<ProducerResult>(module, "ProducerResult")
		.value("ACCEPTED", ProducerResult::Accepted)
		.value("STALE_GENERATION", ProducerResult::StaleGeneration)
		.value("INVALID_PHASE", ProducerResult::InvalidPhase)
		.value("RECOVERY_REQUIRED", ProducerResult::RecoveryRequired)
		.value("HIGHWATER_AHEAD", ProducerResult::HighwaterAhead)
		.value("HIGHWATER_REGRESSED", ProducerResult::HighwaterRegressed)
		.value("OUTBOX_FULL", ProducerResult::OutboxFull)
		.value("SEQUENCE_MISMATCH", ProducerResult::SequenceMismatch)
		.value("NO_PENDING_TRANSACTION", ProducerResult::NoPendingTransaction)
		.value("TRANSACTION_MISSING", ProducerResult::TransactionMissing)
		.value("RECOVERY_NOT_RECOVERABLE", ProducerResult::RecoveryNotRecoverable)
		.value("INVALID_ARGUMENT", ProducerResult::InvalidArgument);

	nb::enum_<ProducerRecoveryDisposition>(module, "ProducerRecoveryDisposition")
		.value("NONE", ProducerRecoveryDisposition::None)
		.value("RECOVERABLE_CONFLICT", ProducerRecoveryDisposition::RecoverableConflict)
		.value("INVALID_OPERATION", ProducerRecoveryDisposition::InvalidOperation)
		.value("SESSION_FATAL", ProducerRecoveryDisposition::SessionFatal);

	nb::class_<ConnectionStart>(module, "ConnectionStart")
		.def_ro("generation", &ConnectionStart::Generation)
		.def_ro("sync_from", &ConnectionStart::SyncFrom);

	nb::class_<ProducerConnectionStart>(module, "ProducerConnectionStart")
		.def_ro("generation", &ProducerConnectionStart::Generation);

	nb::class_<FrameDecoder>(module, "FrameDecoder")
		.def(
			"__init__",
			[](FrameDecoder* decoder, std::size_t max_frame_size)
			{
				if (!openusdconnect::client::IsValidMaxFrameSize(max_frame_size))
				{
					throw nb::value_error("max_frame_size must fit in a non-zero uint32");
				}
				new (decoder) FrameDecoder(max_frame_size);
			},
			"max_frame_size"_a = openusdconnect::client::kDefaultMaxFrameSize)
		.def(
			"feed",
			[](FrameDecoder& decoder, const nb::bytes& chunk)
			{
				std::vector<std::vector<std::uint8_t>> frames;
				const FrameResult result = decoder.Feed(
					static_cast<const std::uint8_t*>(chunk.data()), chunk.size(), frames);
				if (result != FrameResult::Success)
				{
					RaiseFrameError(result);
				}
				return ToPythonBytes(std::move(frames));
			},
			"chunk"_a)
		.def("reset", &FrameDecoder::Reset)
		.def_prop_ro("buffered_bytes", &FrameDecoder::BufferedBytes)
		.def_prop_ro("max_frame_size", &FrameDecoder::MaxFrameSize);

	module.def(
		"encode_frame",
		[](const nb::bytes& payload, std::size_t max_frame_size)
		{
			std::vector<std::uint8_t> framed;
			const FrameResult result = openusdconnect::client::EncodeFrame(
				static_cast<const std::uint8_t*>(payload.data()), payload.size(), framed,
				max_frame_size);
			if (result != FrameResult::Success)
			{
				RaiseFrameError(result);
			}
			return nb::bytes(framed.data(), framed.size());
		},
		"payload"_a, "max_frame_size"_a = openusdconnect::client::kDefaultMaxFrameSize);

	nb::class_<PythonReceiverInbox>(module, "ReceiverInbox")
		.def(
			"__init__",
			[](PythonReceiverInbox* inbox, std::int32_t initial_sync_from, std::size_t max_messages,
			   bool require_contiguous)
			{
				if (!PythonReceiverInbox::IsValidConfiguration(initial_sync_from, max_messages))
				{
					throw nb::value_error(
						"initial_sync_from must be positive and max_messages must be non-zero");
				}
				new (inbox)
					PythonReceiverInbox(initial_sync_from, max_messages, require_contiguous);
			},
			"initial_sync_from"_a, "max_messages"_a, "require_contiguous"_a = false)
		.def("begin_connection", &PythonReceiverInbox::BeginConnection)
		.def("disconnect", &PythonReceiverInbox::Disconnect, "generation"_a)
		.def(
			"accept",
			[](PythonReceiverInbox& inbox, std::uint64_t generation, ReceiverMessageKind kind,
			   std::int32_t sequence, nb::handle frame)
			{
				ValidateBytes(frame, "frame must not be empty");
				const AcceptResult result =
					inbox.Accept(generation, kind, sequence, nb::borrow<PythonPayload>(frame));
				if (result == AcceptResult::InvalidSequence)
				{
					throw nb::value_error("sequenced messages require a positive sequence");
				}
				return result;
			},
			"generation"_a, "kind"_a, "sequence"_a, "frame"_a)
		.def(
			"accept_replay_complete",
			[](PythonReceiverInbox& inbox, std::uint64_t generation, std::int32_t head_seq,
			   std::uint64_t epoch)
			{
				const AcceptResult result = inbox.AcceptReplayComplete(generation, head_seq, epoch);
				if (result == AcceptResult::InvalidSequence)
				{
					throw nb::value_error("replay head must not be negative");
				}
				return result;
			},
			"generation"_a, "head_seq"_a, "epoch"_a)
		.def(
			"drain",
			[](PythonReceiverInbox& inbox, std::optional<std::size_t> max_messages)
			{
				if (max_messages.has_value() && *max_messages == 0)
				{
					throw nb::value_error("max_messages must be non-zero when specified");
				}
				return ToPythonBytes(inbox.Drain(max_messages));
			},
			"max_messages"_a = nb::none())
		.def("mark_replay_applied", &PythonReceiverInbox::MarkReplayApplied)
		.def("mark_applied_through", &PythonReceiverInbox::MarkAppliedThrough, "generation"_a,
			 "sequence"_a)
		.def(
			"request_replay_from",
			[](PythonReceiverInbox& inbox, std::int32_t sequence)
			{
				if (!inbox.RequestReplayFrom(sequence))
				{
					throw nb::value_error("replay sequence must be at least one");
				}
			},
			"sequence"_a)
		.def("reset_applied_progress", &PythonReceiverInbox::ResetAppliedProgress)
		.def("clear_overflow", &PythonReceiverInbox::ClearOverflow)
		.def_prop_ro("generation", &PythonReceiverInbox::Generation)
		.def_prop_ro("last_sequence", &PythonReceiverInbox::LastSequence)
		.def_prop_ro("last_applied_sequence", &PythonReceiverInbox::LastAppliedSequence)
		.def_prop_ro("size", &PythonReceiverInbox::Size)
		.def_prop_ro("synchronized", &PythonReceiverInbox::Synchronized)
		.def_prop_ro("overflowed", &PythonReceiverInbox::Overflowed)
		.def_prop_ro("replay_head_sequence", &PythonReceiverInbox::ReplayHeadSequence)
		.def_prop_ro("replay_epoch", &PythonReceiverInbox::ReplayEpoch);

	nb::class_<PythonProducerSession>(module, "ProducerSession")
		.def(
			"__init__",
			[](PythonProducerSession* session, std::size_t capacity)
			{
				if (!PythonProducerSession::IsValidConfiguration(capacity))
				{
					throw nb::value_error("capacity must be non-zero");
				}
				new (session) PythonProducerSession(capacity);
			},
			"capacity"_a)
		.def("begin_connection", &PythonProducerSession::BeginConnection)
		.def("accept_hello", &PythonProducerSession::AcceptHello, "generation"_a,
			 "committed_through"_a)
		.def("disconnect", &PythonProducerSession::Disconnect, "generation"_a)
		.def(
			"append",
			[](PythonProducerSession& session, std::uint64_t generation,
			   std::uint64_t transaction_id, nb::handle payload, std::size_t event_count,
			   std::string layer_key)
			{
				ValidateBytes(payload, "transaction payload must not be empty");
				if (event_count == 0)
				{
					throw nb::value_error("event_count must be non-zero");
				}
				return session.Append(generation, transaction_id,
									  nb::borrow<PythonPayload>(payload), event_count,
									  std::move(layer_key));
			},
			"generation"_a, "transaction_id"_a, "payload"_a, "event_count"_a, "layer_key"_a = "")
		.def(
			"claim_next_unsent",
			[](PythonProducerSession& session, std::uint64_t generation) -> nb::object
			{
				PythonProducerSession::Entry entry;
				const ProducerResult result = session.ClaimNextUnsent(generation, entry);
				if (result == ProducerResult::NoPendingTransaction)
				{
					return nb::none();
				}
				if (result != ProducerResult::Accepted)
				{
					throw std::logic_error("producer session is not ready to replay");
				}
				return nb::make_tuple(entry.TransactionId, entry.Payload, entry.EventCount,
									  entry.LayerKey);
			},
			"generation"_a)
		.def("acknowledge_through", &PythonProducerSession::AcknowledgeThrough, "generation"_a,
			 "transaction_id"_a)
		.def("reject", &PythonProducerSession::Reject, "generation"_a, "transaction_id"_a,
			 "disposition"_a)
		.def(
			"repair_rejected",
			[](PythonProducerSession& session, nb::handle payload, std::size_t event_count,
			   std::string layer_key)
			{
				ValidateBytes(payload, "transaction payload must not be empty");
				if (event_count == 0)
				{
					throw nb::value_error("event_count must be non-zero");
				}
				return session.RepairRejected(nb::borrow<PythonPayload>(payload), event_count,
											  std::move(layer_key));
			},
			"payload"_a, "event_count"_a, "layer_key"_a = "")
		.def("reset_session", &PythonProducerSession::ResetSession)
		.def("contains", &PythonProducerSession::Contains, "transaction_id"_a)
		.def("drain_acknowledged_event_count", &PythonProducerSession::DrainAcknowledgedEventCount)
		.def("entries",
			 [](const PythonProducerSession& session)
			 {
				 nb::list result;
				 for (const auto& entry : session.Entries())
				 {
					 result.append(nb::make_tuple(entry.TransactionId, entry.Payload,
												  entry.EventCount, entry.LayerKey));
				 }
				 return result;
			 })
		.def_prop_ro("phase", &PythonProducerSession::Phase)
		.def_prop_ro("generation", &PythonProducerSession::Generation)
		.def_prop_ro("can_append", &PythonProducerSession::CanAppend)
		.def_prop_ro("empty", &PythonProducerSession::Empty)
		.def_prop_ro("recovery_required", &PythonProducerSession::RecoveryRequired)
		.def_prop_ro("recovery_disposition", &PythonProducerSession::RecoveryDisposition)
		.def_prop_ro("rejected_transaction_id", &PythonProducerSession::RejectedTransactionId)
		.def_prop_ro("pending_transaction_count", &PythonProducerSession::PendingTransactionCount)
		.def_prop_ro("pending_event_count", &PythonProducerSession::PendingEventCount)
		.def_prop_ro("next_transaction_id", &PythonProducerSession::NextTransactionId)
		.def_prop_ro("acknowledged_transaction_count",
					 &PythonProducerSession::AcknowledgedTransactionCount)
		.def_prop_ro("acknowledged_event_count", &PythonProducerSession::AcknowledgedEventCount)
		.def_prop_ro("submitted_transaction_count",
					 &PythonProducerSession::SubmittedTransactionCount)
		.def_prop_ro("last_acknowledged_transaction_id",
					 &PythonProducerSession::LastAcknowledgedTransactionId);
}
