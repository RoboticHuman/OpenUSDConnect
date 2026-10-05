#pragma once

#include "openusdconnect/client/engine/producer_endpoint.h"

#include "frames.h"
#include "test_check.h"

#include <cstddef>
#include <cstdint>
#include <string_view>
#include <vector>

// Producer test fixtures shared by the endpoint and driver tests.
namespace producer_test
{

using namespace endpoint_test;

[[nodiscard]] inline ProducerConfig TestConfig()
{
	ProducerConfig config;
	config.Host = "127.0.0.1";
	config.Port = 7200;
	config.ClientId = "client";
	config.Origin = "origin";
	config.SessionId = "session";
	return config;
}

// A one-event transaction frame, encoded as a host encodes it.
[[nodiscard]] inline Bytes TransactionFrame(std::uint64_t transaction_id, std::string_view prim,
											std::string_view layer_key = {})
{
	flatbuffers::FlatBufferBuilder builder(128);
	flatbuffers::Offset<OpenUSDConnect::EventWrapper> event;
	CHECK(BuildVisibilityEvent(builder, VisibilityEventView{prim, true}, event) ==
		  ProtocolResult::Success);
	CHECK(FinishTransactionFrame(builder, transaction_id, &event, 1, layer_key) ==
		  ProtocolResult::Success);
	const std::uint8_t* bytes = builder.GetBufferPointer();
	return {bytes, bytes + builder.GetSize()};
}

[[nodiscard]] inline Bytes ClaimFrame()
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto claim =
		OpenUSDConnect::CreateClaimPlayback(builder, CreateString(builder, "client"));
	CHECK(FinishEnvelopeFrame(builder, OpenUSDConnect::CreateEnvelope(
										   builder, Payload::ClaimPlayback, claim.Union(),
										   kSchemaVersion)) == ProtocolResult::Success);
	const std::uint8_t* bytes = builder.GetBufferPointer();
	return {bytes, bytes + builder.GetSize()};
}

[[nodiscard]] inline std::vector<Payload> Kinds(const std::vector<Bytes>& frames)
{
	std::vector<Payload> kinds;
	for (const Bytes& frame : frames)
	{
		kinds.push_back(DecodeSent(frame).payload_type());
	}
	return kinds;
}

[[nodiscard]] inline std::vector<std::uint64_t> TransactionIds(const std::vector<Bytes>& frames)
{
	std::vector<std::uint64_t> ids;
	for (const Bytes& frame : frames)
	{
		const OpenUSDConnect::Txn* transaction = DecodeSent(frame).payload_as_Txn();
		CHECK(transaction != nullptr);
		ids.push_back(transaction->txn_id());
	}
	return ids;
}

[[nodiscard]] inline Bytes Concatenate(const std::vector<Bytes>& frames)
{
	Bytes bytes;
	for (const Bytes& frame : frames)
	{
		bytes.insert(bytes.end(), frame.begin(), frame.end());
	}
	return bytes;
}

// Splits what a client sent into its length-prefixed frames.
[[nodiscard]] inline std::vector<Bytes> SplitFrames(const Bytes& stream)
{
	std::vector<Bytes> frames;
	std::size_t offset = 0;
	while (offset < stream.size())
	{
		std::size_t size = 0;
		CHECK(stream.size() - offset >= kFrameHeaderSize);
		CHECK(TryReadFrameHeader(stream.data() + offset, kDefaultMaxFrameSize, size));
		const std::size_t end = offset + kFrameHeaderSize + size;
		CHECK(end <= stream.size());
		frames.emplace_back(stream.begin() + static_cast<std::ptrdiff_t>(offset),
							stream.begin() + static_cast<std::ptrdiff_t>(end));
		offset = end;
	}
	return frames;
}

} // namespace producer_test
