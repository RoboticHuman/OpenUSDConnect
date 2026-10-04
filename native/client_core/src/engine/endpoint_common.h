#pragma once

#include "openusdconnect/client/engine/actions.h"
#include "openusdconnect/client/engine/notification.h"
#include "openusdconnect/client/frame_codec.h"
#include "openusdconnect/client/protocol_codec.h"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

// The connection steps both endpoints share, as free functions over their state.
namespace openusdconnect::client::detail
{

[[nodiscard]] inline std::string Text(const flatbuffers::String* value)
{
	return value ? value->str() : std::string();
}

template <typename T>
[[nodiscard]] std::optional<T> Value(flatbuffers::Optional<T> value) noexcept
{
	return value.has_value() ? std::optional<T>(*value) : std::nullopt;
}

template <typename Config>
[[nodiscard]] std::string Address(const Config& config)
{
	return config.Host + ":" + std::to_string(config.Port);
}

[[nodiscard]] inline std::shared_ptr<const std::vector<std::uint8_t>>
Share(std::vector<std::uint8_t> bytes)
{
	return std::make_shared<const std::vector<std::uint8_t>>(std::move(bytes));
}

[[nodiscard]] inline std::shared_ptr<const std::vector<std::uint8_t>>
Share(const flatbuffers::FlatBufferBuilder& builder)
{
	const std::uint8_t* bytes = builder.GetBufferPointer();
	return Share(std::vector<std::uint8_t>(bytes, bytes + builder.GetSize()));
}

// The Hello fields every role sends; the caller adds its own before QueueHello.
template <typename Config>
[[nodiscard]] HelloParameters CommonHello(std::string_view role, const Config& config,
										  std::string_view token)
{
	HelloParameters hello;
	hello.Role = role;
	hello.ClientId = config.ClientId;
	hello.Origin = config.Origin;
	hello.Department = config.Department;
	hello.Token = token;
	hello.LayerMode = config.LayerMode;
	return hello;
}

// False, with an error logged, when the parameters cannot be encoded.
[[nodiscard]] inline bool QueueHello(const HelloParameters& hello, std::vector<Action>& actions)
{
	flatbuffers::FlatBufferBuilder builder(256);
	if (BuildHelloFrame(builder, hello) != ProtocolResult::Success)
	{
		actions.push_back(LogAction{LogLevel::Error, "could not build the Hello frame"});
		return false;
	}
	actions.push_back(SendAction{Share(builder)});
	return true;
}

// Feeds bytes to decoder, then hands each complete frame to handle until it
// returns false. False, with no frame handled, when the bytes do not frame.
template <typename Handle>
[[nodiscard]] bool HandleFrames(FrameDecoder& decoder, const std::uint8_t* data, std::size_t size,
								Handle handle)
{
	std::vector<std::vector<std::uint8_t>> frames;
	if (decoder.Feed(data, size, frames) != FrameResult::Success)
	{
		return false;
	}
	for (std::vector<std::uint8_t>& frame : frames)
	{
		if (!handle(frame))
		{
			break;
		}
	}
	return true;
}

[[nodiscard]] inline std::string DescribeDecodeFailure(ProtocolResult result)
{
	return result == ProtocolResult::SchemaVersionMismatch
			   ? "frame uses an unsupported schema version"
			   : "malformed frame";
}

// Accepted or Rejection is set, or neither for a payload that answers no Hello.
struct HandshakeOutcome final
{
	const OpenUSDConnect::HelloOk* Accepted = nullptr;
	std::optional<HandshakeRejected> Rejection;
};

[[nodiscard]] inline HandshakeOutcome ClassifyHandshake(EnvelopeView envelope)
{
	const HandshakeResponseView response(envelope);
	switch (response.Kind())
	{
	case HandshakeResponseKind::Accepted:
		return {response.Accepted(), std::nullopt};
	case HandshakeResponseKind::AuthenticationRejected:
		return {nullptr, HandshakeRejected{true, OpenUSDConnect::HelloRejectionCode::Unspecified,
										   Text(response.AuthenticationRejection()->reason())}};
	case HandshakeResponseKind::ConfigurationRejected:
	{
		const OpenUSDConnect::HelloRejected& rejected = *response.ConfigurationRejection();
		return {nullptr, HandshakeRejected{false, rejected.code(), Text(rejected.reason())}};
	}
	case HandshakeResponseKind::Unexpected:
		break;
	}
	return {};
}

[[nodiscard]] inline std::string DescribeRejection(const HandshakeRejected& rejection)
{
	const std::string kind =
		rejection.Authentication
			? "authentication rejected"
			: "connection rejected (code " + std::to_string(static_cast<int>(rejection.Code)) + ")";
	return kind + ": " + rejection.Reason;
}

// Only the fields the server authored; nullopt when it authored none.
[[nodiscard]] inline std::optional<StageMetadata>
DecodeStageMetadata(const OpenUSDConnect::SetStageMetadata* table)
{
	if (!table)
	{
		return std::nullopt;
	}
	StageMetadata metadata;
	metadata.TimeCodesPerSecond = Value(table->timeCodesPerSecond());
	metadata.FramesPerSecond = Value(table->framesPerSecond());
	metadata.StartTimeCode = Value(table->startTimeCode());
	metadata.EndTimeCode = Value(table->endTimeCode());
	metadata.MetersPerUnit = Value(table->metersPerUnit());
	if (table->upAxis() && table->upAxis()->size() != 0)
	{
		metadata.UpAxis = table->upAxis()->str();
	}
	const bool authored = metadata.TimeCodesPerSecond || metadata.FramesPerSecond ||
						  metadata.StartTimeCode || metadata.EndTimeCode ||
						  metadata.MetersPerUnit || metadata.UpAxis;
	return authored ? std::optional<StageMetadata>(std::move(metadata)) : std::nullopt;
}

// Notifies the token and the authored stage metadata a HelloOk carries, and
// keeps that metadata.
inline void NotifyHelloFields(const OpenUSDConnect::HelloOk& hello, StageMetadata& metadata,
							  NotificationQueue& notifications, std::vector<Action>& actions)
{
	if (std::string token = Text(hello.token()); !token.empty())
	{
		actions.push_back(LogAction{LogLevel::Info, "token issued by server"});
		notifications.Push(TokenIssued{std::move(token)});
	}
	if (std::optional<StageMetadata> authored = DecodeStageMetadata(hello.stage_metadata()))
	{
		metadata = *authored;
		notifications.Push(std::move(*authored));
	}
}

} // namespace openusdconnect::client::detail
