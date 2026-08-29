#pragma once

#include "openusdconnect/client/frame_codec.h"
#include "openusdconnect/client/schema/messages_generated.h"

#include <cstddef>
#include <cstdint>
#include <string_view>

namespace openusdconnect::client
{

inline constexpr std::uint16_t kSchemaVersion = 10;
inline constexpr std::int32_t kProtocolVersion = 12;

enum class ProtocolResult : std::uint8_t
{
	Success,
	InvalidArgument,
	InvalidBuffer,
	SchemaVersionMismatch,
	UnexpectedPayload,
	EmptyTransaction,
	InvalidMaxFrameSize,
	PayloadTooLarge,
};

class EnvelopeView final
{
public:
	EnvelopeView() = default;

	[[nodiscard]] const OpenUSDConnect::Envelope* Get() const noexcept
	{
		return Envelope;
	}

	[[nodiscard]] OpenUSDConnect::Payload PayloadType() const noexcept
	{
		return Envelope ? Envelope->payload_type() : OpenUSDConnect::Payload::NONE;
	}

	explicit operator bool() const noexcept
	{
		return Envelope != nullptr;
	}

private:
	explicit EnvelopeView(const OpenUSDConnect::Envelope* envelope) noexcept
		: Envelope(envelope)
	{
	}

	const OpenUSDConnect::Envelope* Envelope = nullptr;

	friend ProtocolResult DecodeEnvelope(const std::uint8_t*, std::size_t, EnvelopeView&) noexcept;
};

[[nodiscard]] inline ProtocolResult DecodeEnvelope(const std::uint8_t* data, std::size_t size,
												   EnvelopeView& view) noexcept
{
	view = EnvelopeView();
	if (!data || size < sizeof(flatbuffers::uoffset_t))
	{
		return ProtocolResult::InvalidBuffer;
	}

	flatbuffers::Verifier verifier(data, size);
	if (!OpenUSDConnect::VerifyEnvelopeBuffer(verifier))
	{
		return ProtocolResult::InvalidBuffer;
	}

	const OpenUSDConnect::Envelope* envelope = OpenUSDConnect::GetEnvelope(data);
	if (envelope->schema_version() != kSchemaVersion)
	{
		return ProtocolResult::SchemaVersionMismatch;
	}

	view = EnvelopeView(envelope);
	return ProtocolResult::Success;
}

enum class HandshakeResponseKind : std::uint8_t
{
	Accepted,
	AuthenticationRejected,
	ConfigurationRejected,
	Unexpected,
};

class HandshakeResponseView final
{
public:
	explicit HandshakeResponseView(EnvelopeView envelope) noexcept
		: Envelope(envelope.Get())
	{
	}

	[[nodiscard]] HandshakeResponseKind Kind() const noexcept
	{
		switch (Envelope->payload_type())
		{
		case OpenUSDConnect::Payload::HelloOk:
			return HandshakeResponseKind::Accepted;
		case OpenUSDConnect::Payload::AuthRejected:
			return HandshakeResponseKind::AuthenticationRejected;
		case OpenUSDConnect::Payload::HelloRejected:
			return HandshakeResponseKind::ConfigurationRejected;
		default:
			return HandshakeResponseKind::Unexpected;
		}
	}

	[[nodiscard]] const OpenUSDConnect::HelloOk* Accepted() const noexcept
	{
		return Envelope->payload_as_HelloOk();
	}

	[[nodiscard]] const OpenUSDConnect::AuthRejected* AuthenticationRejection() const noexcept
	{
		return Envelope->payload_as_AuthRejected();
	}

	[[nodiscard]] const OpenUSDConnect::HelloRejected* ConfigurationRejection() const noexcept
	{
		return Envelope->payload_as_HelloRejected();
	}

private:
	const OpenUSDConnect::Envelope* Envelope;
};

enum class ControlMessageKind : std::uint8_t
{
	TransactionResult,
	RateLimited,
	Ping,
	Resync,
	ReplayComplete,
	BroadcastEvent,
	Other,
};

class ControlMessageView final
{
public:
	explicit ControlMessageView(EnvelopeView envelope) noexcept
		: Envelope(envelope.Get())
	{
	}

	[[nodiscard]] ControlMessageKind Kind() const noexcept
	{
		switch (Envelope->payload_type())
		{
		case OpenUSDConnect::Payload::TransactionResult:
			return ControlMessageKind::TransactionResult;
		case OpenUSDConnect::Payload::RateLimited:
			return ControlMessageKind::RateLimited;
		case OpenUSDConnect::Payload::Ping:
			return ControlMessageKind::Ping;
		case OpenUSDConnect::Payload::Resync:
			return ControlMessageKind::Resync;
		case OpenUSDConnect::Payload::ReplayComplete:
			return ControlMessageKind::ReplayComplete;
		case OpenUSDConnect::Payload::BroadcastEvent:
			return ControlMessageKind::BroadcastEvent;
		default:
			return ControlMessageKind::Other;
		}
	}

	[[nodiscard]] const OpenUSDConnect::TransactionResult* TransactionResult() const noexcept
	{
		return Envelope->payload_as_TransactionResult();
	}

	[[nodiscard]] const OpenUSDConnect::RateLimited* RateLimit() const noexcept
	{
		return Envelope->payload_as_RateLimited();
	}

	[[nodiscard]] const OpenUSDConnect::ReplayComplete* ReplayComplete() const noexcept
	{
		return Envelope->payload_as_ReplayComplete();
	}

	[[nodiscard]] const OpenUSDConnect::BroadcastEvent* BroadcastEvent() const noexcept
	{
		return Envelope->payload_as_BroadcastEvent();
	}

private:
	const OpenUSDConnect::Envelope* Envelope;
};

struct HelloParameters final
{
	std::string_view Role;
	std::int32_t SyncFrom = 0;
	std::string_view ClientId;
	std::string_view Origin;
	std::string_view Department;
	std::string_view Token;
	bool LayeredReplay = false;
	OpenUSDConnect::LayerMode LayerMode = OpenUSDConnect::LayerMode::Managed;
	std::string_view ProducerSessionId;
};

[[nodiscard]] inline bool IsValidHelloParameters(const HelloParameters& parameters) noexcept
{
	return (parameters.Role == "receiver" || parameters.Role == "emitter") &&
		   parameters.SyncFrom >= 0 && !parameters.ClientId.empty() && !parameters.Origin.empty();
}

[[nodiscard]] inline flatbuffers::Offset<flatbuffers::String>
CreateString(flatbuffers::FlatBufferBuilder& builder, std::string_view value)
{
	return builder.CreateString(value.data(), value.size());
}

[[nodiscard]] inline ProtocolResult
FinishEnvelopeFrame(flatbuffers::FlatBufferBuilder& builder,
					flatbuffers::Offset<OpenUSDConnect::Envelope> envelope,
					std::size_t max_frame_size = kDefaultMaxFrameSize) noexcept
{
	if (!IsValidMaxFrameSize(max_frame_size))
	{
		return ProtocolResult::InvalidMaxFrameSize;
	}

	OpenUSDConnect::FinishSizePrefixedEnvelopeBuffer(builder, envelope);
	const std::size_t payload_size = builder.GetSize() - kFrameHeaderSize;
	const FrameResult result =
		WriteFrameHeader(payload_size, builder.GetBufferPointer(), max_frame_size);
	return result == FrameResult::Success ? ProtocolResult::Success
										  : ProtocolResult::PayloadTooLarge;
}

[[nodiscard]] inline ProtocolResult
BuildHelloFrame(flatbuffers::FlatBufferBuilder& builder, const HelloParameters& parameters,
				std::size_t max_frame_size = kDefaultMaxFrameSize)
{
	if (!IsValidHelloParameters(parameters))
	{
		return ProtocolResult::InvalidArgument;
	}

	const auto hello = OpenUSDConnect::CreateHello(
		builder, CreateString(builder, parameters.Role), kProtocolVersion, parameters.SyncFrom,
		CreateString(builder, parameters.ClientId), CreateString(builder, parameters.Origin),
		CreateString(builder, parameters.Department), CreateString(builder, parameters.Token),
		parameters.LayeredReplay, parameters.LayerMode,
		CreateString(builder, parameters.ProducerSessionId));
	const auto envelope = OpenUSDConnect::CreateEnvelope(builder, OpenUSDConnect::Payload::Hello,
														 hello.Union(), kSchemaVersion);
	return FinishEnvelopeFrame(builder, envelope, max_frame_size);
}

struct XformTrsEventView final
{
	std::string_view PrimPath;
	const float* Translation = nullptr;
	const float* Rotation = nullptr;
	const float* Scale = nullptr;
	std::uint8_t Fields = 7;
	flatbuffers::Optional<double> Time = flatbuffers::nullopt;
};

[[nodiscard]] inline ProtocolResult
BuildXformTrsEvent(flatbuffers::FlatBufferBuilder& builder, const XformTrsEventView& event,
				   flatbuffers::Offset<flatbuffers::String> prim,
				   flatbuffers::Offset<OpenUSDConnect::EventWrapper>& wrapper)
{
	if (prim.IsNull() || (event.Fields & 1U && !event.Translation) ||
		(event.Fields & 2U && !event.Rotation) || (event.Fields & 4U && !event.Scale))
	{
		return ProtocolResult::InvalidArgument;
	}

	const auto translation = event.Fields & 1U ? builder.CreateVector(event.Translation, 3) : 0;
	const auto rotation = event.Fields & 2U ? builder.CreateVector(event.Rotation, 4) : 0;
	const auto scale = event.Fields & 4U ? builder.CreateVector(event.Scale, 3) : 0;
	const auto value = OpenUSDConnect::CreateSetXformTrs(builder, prim, event.Fields, translation,
														 rotation, scale, event.Time);
	wrapper = OpenUSDConnect::CreateEventWrapper(builder, OpenUSDConnect::EventPayload::SetXformTrs,
												 value.Union());
	return ProtocolResult::Success;
}

[[nodiscard]] inline ProtocolResult
BuildEnsureXformOpsEvent(flatbuffers::FlatBufferBuilder& builder,
						 flatbuffers::Offset<flatbuffers::String> prim,
						 flatbuffers::Offset<OpenUSDConnect::EventWrapper>& wrapper)
{
	if (prim.IsNull())
	{
		return ProtocolResult::InvalidArgument;
	}

	const auto event = OpenUSDConnect::CreateEnsureXformOps(builder, prim);
	wrapper = OpenUSDConnect::CreateEventWrapper(
		builder, OpenUSDConnect::EventPayload::EnsureXformOps, event.Union());
	return ProtocolResult::Success;
}

[[nodiscard]] inline ProtocolResult
BuildXformTrsEvent(flatbuffers::FlatBufferBuilder& builder, const XformTrsEventView& event,
				   flatbuffers::Offset<OpenUSDConnect::EventWrapper>& wrapper)
{
	if (event.PrimPath.empty())
	{
		return ProtocolResult::InvalidArgument;
	}
	return BuildXformTrsEvent(builder, event, CreateString(builder, event.PrimPath), wrapper);
}

[[nodiscard]] inline ProtocolResult
BuildEnsureXformOpsEvent(flatbuffers::FlatBufferBuilder& builder, std::string_view prim_path,
						 flatbuffers::Offset<OpenUSDConnect::EventWrapper>& wrapper)
{
	if (prim_path.empty())
	{
		return ProtocolResult::InvalidArgument;
	}

	return BuildEnsureXformOpsEvent(builder, CreateString(builder, prim_path), wrapper);
}

struct VisibilityEventView final
{
	std::string_view PrimPath;
	bool Visible = true;
	flatbuffers::Optional<double> Time = flatbuffers::nullopt;
};

[[nodiscard]] inline ProtocolResult
BuildVisibilityEvent(flatbuffers::FlatBufferBuilder& builder, const VisibilityEventView& event,
					 flatbuffers::Offset<OpenUSDConnect::EventWrapper>& wrapper)
{
	if (event.PrimPath.empty())
	{
		return ProtocolResult::InvalidArgument;
	}

	const auto value = OpenUSDConnect::CreateSetVisibility(
		builder, CreateString(builder, event.PrimPath), event.Visible, event.Time);
	wrapper = OpenUSDConnect::CreateEventWrapper(
		builder, OpenUSDConnect::EventPayload::SetVisibility, value.Union());
	return ProtocolResult::Success;
}

struct ConnectableInputValueView final
{
	std::string_view Name;
	std::string_view TypeName;
	OpenUSDConnect::ConnectableInputValueType ValueType =
		OpenUSDConnect::ConnectableInputValueType::None;
	float ScalarFloat = 0.0F;
	std::int32_t ScalarInt = 0;
	bool ScalarBool = false;
	std::string_view ScalarString;
	const float* FloatArray = nullptr;
	std::size_t FloatCount = 0;
	const std::int32_t* IntArray = nullptr;
	std::size_t IntCount = 0;
	flatbuffers::Offset<flatbuffers::Vector<flatbuffers::Offset<flatbuffers::String>>> StringArray;
};

[[nodiscard]] inline ProtocolResult
BuildConnectableInputValue(flatbuffers::FlatBufferBuilder& builder,
						   const ConnectableInputValueView& value,
						   flatbuffers::Offset<OpenUSDConnect::ConnectableInputValue>& offset)
{
	if (value.Name.empty() || value.TypeName.empty() ||
		(value.FloatCount != 0 && !value.FloatArray) || (value.IntCount != 0 && !value.IntArray))
	{
		return ProtocolResult::InvalidArgument;
	}

	flatbuffers::Offset<flatbuffers::String> scalar_string;
	flatbuffers::Offset<flatbuffers::Vector<float>> float_array;
	flatbuffers::Offset<flatbuffers::Vector<std::int32_t>> int_array;
	if (value.ValueType == OpenUSDConnect::ConnectableInputValueType::ScalarString)
	{
		scalar_string = CreateString(builder, value.ScalarString);
	}
	else if (value.ValueType == OpenUSDConnect::ConnectableInputValueType::FloatArray)
	{
		float_array = builder.CreateVector(value.FloatArray, value.FloatCount);
	}
	else if (value.ValueType == OpenUSDConnect::ConnectableInputValueType::IntArray)
	{
		int_array = builder.CreateVector(value.IntArray, value.IntCount);
	}

	offset = OpenUSDConnect::CreateConnectableInputValue(
		builder, CreateString(builder, value.Name), CreateString(builder, value.TypeName),
		value.ValueType, value.ScalarFloat, value.ScalarInt, value.ScalarBool, scalar_string,
		float_array, int_array, value.StringArray);
	return ProtocolResult::Success;
}

[[nodiscard]] inline ProtocolResult BuildConnectableInputEvent(
	flatbuffers::FlatBufferBuilder& builder, std::string_view prim_path, std::string_view info_id,
	const flatbuffers::Offset<OpenUSDConnect::ConnectableInputValue>* inputs,
	std::size_t input_count, flatbuffers::Offset<OpenUSDConnect::EventWrapper>& wrapper,
	flatbuffers::Optional<double> time = flatbuffers::nullopt)
{
	if (prim_path.empty() || input_count == 0 || !inputs)
	{
		return ProtocolResult::InvalidArgument;
	}

	const auto event = OpenUSDConnect::CreateSetConnectableInput(
		builder, CreateString(builder, prim_path), CreateString(builder, info_id),
		builder.CreateVector(inputs, input_count), time);
	wrapper = OpenUSDConnect::CreateEventWrapper(
		builder, OpenUSDConnect::EventPayload::SetConnectableInput, event.Union());
	return ProtocolResult::Success;
}

[[nodiscard]] inline ProtocolResult BuildTransactionEnvelope(
	flatbuffers::FlatBufferBuilder& builder, std::uint64_t transaction_id,
	flatbuffers::Offset<flatbuffers::Vector<flatbuffers::Offset<OpenUSDConnect::EventWrapper>>>
		events,
	std::string_view layer_key, flatbuffers::Offset<OpenUSDConnect::Envelope>& envelope)
{
	if (events.IsNull())
	{
		return ProtocolResult::EmptyTransaction;
	}

	const auto transaction = OpenUSDConnect::CreateTxn(
		builder, events, CreateString(builder, layer_key), transaction_id);
	envelope = OpenUSDConnect::CreateEnvelope(builder, OpenUSDConnect::Payload::Txn,
											  transaction.Union(), kSchemaVersion);
	return ProtocolResult::Success;
}

[[nodiscard]] inline ProtocolResult
FinishTransactionFrame(flatbuffers::FlatBufferBuilder& builder, std::uint64_t transaction_id,
					   const flatbuffers::Offset<OpenUSDConnect::EventWrapper>* events,
					   std::size_t event_count, std::string_view layer_key = {},
					   std::size_t max_frame_size = kDefaultMaxFrameSize)
{
	if (event_count == 0 || !events)
	{
		return ProtocolResult::EmptyTransaction;
	}

	flatbuffers::Offset<OpenUSDConnect::Envelope> envelope;
	const ProtocolResult result = BuildTransactionEnvelope(
		builder, transaction_id, builder.CreateVector(events, event_count), layer_key, envelope);
	return result == ProtocolResult::Success
			   ? FinishEnvelopeFrame(builder, envelope, max_frame_size)
			   : result;
}

} // namespace openusdconnect::client
