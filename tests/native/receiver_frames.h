#pragma once

#include "openusdconnect/client/engine/receiver_endpoint.h"

#include "test_check.h"

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

// Receiver test fixtures shared by the endpoint and driver tests.
namespace receiver_test
{

using namespace openusdconnect::client;
using OpenUSDConnect::HelloRejectionCode;
using OpenUSDConnect::LayerMode;
using OpenUSDConnect::Payload;

using Bytes = std::vector<std::uint8_t>;

[[nodiscard]] inline ReceiverConfig TestConfig()
{
	ReceiverConfig config;
	config.Host = "127.0.0.1";
	config.Port = 7200;
	config.ClientId = "client";
	config.Origin = "origin";
	return config;
}

[[nodiscard]] inline std::string Text(const flatbuffers::String* value)
{
	return value ? value->str() : std::string();
}

[[nodiscard]] inline const OpenUSDConnect::Envelope& Decode(const Bytes& payload)
{
	EnvelopeView view;
	CHECK(DecodeEnvelope(payload.data(), payload.size(), view) == ProtocolResult::Success);
	return *view.Get();
}

[[nodiscard]] inline std::vector<Payload> Kinds(const std::vector<Bytes>& frames)
{
	std::vector<Payload> kinds;
	for (const Bytes& frame : frames)
	{
		kinds.push_back(Decode(frame).payload_type());
	}
	return kinds;
}

[[nodiscard]] inline std::vector<std::int32_t> Sequences(const std::vector<Bytes>& frames)
{
	std::vector<std::int32_t> sequences;
	for (const Bytes& frame : frames)
	{
		if (const auto* event = Decode(frame).payload_as_BroadcastEvent())
		{
			sequences.push_back(event->seq());
		}
	}
	return sequences;
}

// Server-to-receiver frames, length-prefixed as they arrive on the socket.
namespace server
{

struct Hello final
{
	std::string ServerInstance = "server";
	bool ReplayIdentity = true;
	std::optional<std::uint64_t> ReplayEpoch = 0;
	bool LayeredReplay = true;
	LayerMode Mode = LayerMode::Managed;
	std::string Token;
	std::optional<StageMetadata> Metadata;
};

[[nodiscard]] inline Bytes Frame(flatbuffers::FlatBufferBuilder& builder, Payload type,
								 flatbuffers::Offset<void> payload,
								 std::uint16_t schema_version = kSchemaVersion)
{
	OpenUSDConnect::FinishEnvelopeBuffer(
		builder, OpenUSDConnect::CreateEnvelope(builder, type, payload, schema_version));
	Bytes frame;
	CHECK(EncodeFrame(builder.GetBufferPointer(), builder.GetSize(), frame) ==
		  FrameResult::Success);
	return frame;
}

[[nodiscard]] inline flatbuffers::Offset<flatbuffers::String>
OptionalString(flatbuffers::FlatBufferBuilder& builder, std::string_view text)
{
	return text.empty() ? flatbuffers::Offset<flatbuffers::String>() : CreateString(builder, text);
}

[[nodiscard]] inline flatbuffers::Optional<double> Wire(std::optional<double> value)
{
	return value ? flatbuffers::Optional<double>(*value) : flatbuffers::nullopt;
}

[[nodiscard]] inline Bytes HelloOk(const Hello& hello = {})
{
	flatbuffers::FlatBufferBuilder builder(256);
	flatbuffers::Offset<OpenUSDConnect::SetStageMetadata> metadata;
	if (hello.Metadata)
	{
		const StageMetadata& fields = *hello.Metadata;
		const auto up_axis = OptionalString(builder, fields.UpAxis.value_or(""));
		metadata = OpenUSDConnect::CreateSetStageMetadata(
			builder, Wire(fields.TimeCodesPerSecond), Wire(fields.FramesPerSecond),
			Wire(fields.StartTimeCode), Wire(fields.EndTimeCode), Wire(fields.MetersPerUnit),
			up_axis);
	}
	const auto token = OptionalString(builder, hello.Token);
	const auto instance = OptionalString(builder, hello.ServerInstance);
	const auto epoch = hello.ReplayEpoch ? flatbuffers::Optional<std::uint64_t>(*hello.ReplayEpoch)
										 : flatbuffers::nullopt;
	const auto accepted =
		OpenUSDConnect::CreateHelloOk(builder, token, metadata, hello.LayeredReplay, hello.Mode, 0,
									  instance, hello.ReplayIdentity, epoch);
	return Frame(builder, Payload::HelloOk, accepted.Union());
}

[[nodiscard]] inline Bytes AuthRejected(std::string_view reason)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto rejected =
		OpenUSDConnect::CreateAuthRejected(builder, CreateString(builder, reason));
	return Frame(builder, Payload::AuthRejected, rejected.Union());
}

[[nodiscard]] inline Bytes HelloRejected(HelloRejectionCode code, std::string_view reason)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto rejected =
		OpenUSDConnect::CreateHelloRejected(builder, code, CreateString(builder, reason));
	return Frame(builder, Payload::HelloRejected, rejected.Union());
}

[[nodiscard]] inline Bytes Ping()
{
	flatbuffers::FlatBufferBuilder builder(32);
	return Frame(builder, Payload::Ping, OpenUSDConnect::CreatePing(builder).Union());
}

[[nodiscard]] inline Bytes Resync()
{
	flatbuffers::FlatBufferBuilder builder(32);
	return Frame(builder, Payload::Resync, OpenUSDConnect::CreateResync(builder).Union());
}

[[nodiscard]] inline Bytes ReplayComplete(std::int32_t head, std::uint64_t epoch)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto complete = OpenUSDConnect::CreateReplayComplete(builder, head, epoch);
	return Frame(builder, Payload::ReplayComplete, complete.Union());
}

[[nodiscard]] inline Bytes Event(std::int32_t sequence)
{
	flatbuffers::FlatBufferBuilder builder(128);
	const auto prim = OpenUSDConnect::CreateEnsurePrim(
		builder, CreateString(builder, "/World/P" + std::to_string(sequence)));
	const auto event = OpenUSDConnect::CreateEventWrapper(
		builder, OpenUSDConnect::EventPayload::EnsurePrim, prim.Union());
	const auto broadcast = OpenUSDConnect::CreateBroadcastEvent(builder, sequence, event);
	return Frame(builder, Payload::BroadcastEvent, broadcast.Union());
}

[[nodiscard]] inline Bytes LayerGraph(std::int32_t sequence)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto state = OpenUSDConnect::CreateLayerGraphState(builder, sequence);
	return Frame(builder, Payload::LayerGraphState, state.Union());
}

[[nodiscard]] inline Bytes LayerStack()
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto layer = OpenUSDConnect::CreateLogicalLayerState(builder, CreateString(builder, "a"));
	const auto stack = OpenUSDConnect::CreateLayerStackState(
		builder, CreateString(builder, "generation"), 1, builder.CreateVector(&layer, 1));
	return Frame(builder, Payload::LayerStackState, stack.Union());
}

[[nodiscard]] inline Bytes Playback(double time, bool playing, double rate, std::string_view leader)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto state = OpenUSDConnect::CreatePlaybackState(builder, time, playing, rate,
														   CreateString(builder, leader));
	return Frame(builder, Payload::PlaybackState, state.Union());
}

[[nodiscard]] inline Bytes Claimed(std::string_view leader)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto claimed =
		OpenUSDConnect::CreatePlaybackClaimed(builder, CreateString(builder, leader));
	return Frame(builder, Payload::PlaybackClaimed, claimed.Union());
}

[[nodiscard]] inline Bytes ClaimRejected(std::string_view reason, std::string_view leader)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto rejected = OpenUSDConnect::CreatePlaybackRejected(
		builder, CreateString(builder, reason), CreateString(builder, leader));
	return Frame(builder, Payload::PlaybackRejected, rejected.Union());
}

} // namespace server

struct SentHello final
{
	std::string Role;
	std::int32_t ProtocolVersion = 0;
	std::int32_t SyncFrom = 0;
	std::string ClientId;
	std::string Origin;
	std::string Department;
	std::string Token;
	bool LayeredReplay = false;
	LayerMode Mode = LayerMode::Managed;
	// Absent without a claim; empty when the claimed prefix is unknown.
	std::optional<std::string> ReplayServerInstance;
	std::optional<std::uint64_t> ReplayEpoch;

	[[nodiscard]] bool Claims(std::string_view instance, std::uint64_t epoch) const
	{
		return ReplayServerInstance == instance && ReplayEpoch == epoch;
	}

	[[nodiscard]] bool ClaimsUnknownPrefix() const
	{
		return ReplayServerInstance == "" && !ReplayEpoch;
	}
};

[[nodiscard]] inline SentHello DecodeHello(const Bytes& frame)
{
	std::size_t size = 0;
	CHECK(TryReadFrameHeader(frame.data(), kDefaultMaxFrameSize, size));
	CHECK(size + kFrameHeaderSize == frame.size());
	EnvelopeView view;
	CHECK(DecodeEnvelope(frame.data() + kFrameHeaderSize, size, view) == ProtocolResult::Success);
	const OpenUSDConnect::Hello* hello = view.Get()->payload_as_Hello();
	CHECK(hello != nullptr);
	SentHello sent;
	sent.Role = Text(hello->role());
	sent.ProtocolVersion = hello->protocol_version();
	sent.SyncFrom = hello->sync_from();
	sent.ClientId = Text(hello->client_id());
	sent.Origin = Text(hello->origin());
	sent.Department = Text(hello->department());
	sent.Token = Text(hello->token());
	sent.LayeredReplay = hello->layered_replay();
	sent.Mode = hello->layer_mode();
	if (hello->replay_server_instance())
	{
		sent.ReplayServerInstance = hello->replay_server_instance()->str();
	}
	if (hello->replay_epoch().has_value())
	{
		sent.ReplayEpoch = *hello->replay_epoch();
	}
	return sent;
}

} // namespace receiver_test
