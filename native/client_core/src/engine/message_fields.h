#pragma once

#include "openusdconnect/client/engine/notification.h"
#include "openusdconnect/client/schema/messages_generated.h"

#include <cstdint>
#include <optional>
#include <string>
#include <utility>

// Message decoding and log text shared by the endpoints.
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

[[nodiscard]] inline std::string DescribeRejection(const HandshakeRejected& rejection)
{
	const std::string kind =
		rejection.Authentication
			? "authentication rejected"
			: "connection rejected (code " + std::to_string(static_cast<int>(rejection.Code)) + ")";
	return kind + ": " + rejection.Reason;
}

[[nodiscard]] inline std::string Address(const std::string& host, std::uint16_t port)
{
	return host + ":" + std::to_string(port);
}

} // namespace openusdconnect::client::detail
