#include "openusdconnect/client/protocol_codec.h"

#include <cassert>
#include <cstdint>
#include <string_view>

using namespace openusdconnect::client;

int main()
{
	flatbuffers::FlatBufferBuilder hello_builder(256);
	const HelloParameters hello{"emitter",	0,		 "client", "origin",
								"lighting", "token", false,	   OpenUSDConnect::LayerMode::Managed,
								"producer"};
	assert(BuildHelloFrame(hello_builder, hello) == ProtocolResult::Success);

	std::size_t hello_size = 0;
	assert(TryReadFrameHeader(hello_builder.GetBufferPointer(), kDefaultMaxFrameSize, hello_size));
	assert(hello_size + kFrameHeaderSize == hello_builder.GetSize());
	EnvelopeView hello_envelope;
	assert(DecodeEnvelope(hello_builder.GetBufferPointer() + kFrameHeaderSize, hello_size,
						  hello_envelope) == ProtocolResult::Success);
	assert(hello_envelope.PayloadType() == OpenUSDConnect::Payload::Hello);
	assert(std::string_view(hello_envelope.Get()->payload_as_Hello()->client_id()->c_str()) ==
		   "client");
	assert(hello_envelope.Get()->payload_as_Hello()->replay_server_instance() == nullptr);
	assert(!hello_envelope.Get()->payload_as_Hello()->replay_epoch().has_value());

	flatbuffers::FlatBufferBuilder replay_hello_builder(256);
	HelloParameters replay_hello{"receiver", 7, "client", "origin"};
	replay_hello.ReplayPrefix = ReplayPrefixIdentity::Known("server-instance", 3);
	assert(BuildHelloFrame(replay_hello_builder, replay_hello) == ProtocolResult::Success);
	std::size_t replay_hello_size = 0;
	assert(TryReadFrameHeader(replay_hello_builder.GetBufferPointer(), kDefaultMaxFrameSize,
							  replay_hello_size));
	EnvelopeView replay_hello_envelope;
	assert(DecodeEnvelope(replay_hello_builder.GetBufferPointer() + kFrameHeaderSize,
						  replay_hello_size, replay_hello_envelope) == ProtocolResult::Success);
	const OpenUSDConnect::Hello* decoded_replay_hello =
		replay_hello_envelope.Get()->payload_as_Hello();
	assert(std::string_view(decoded_replay_hello->replay_server_instance()->c_str()) ==
		   "server-instance");
	assert(decoded_replay_hello->replay_epoch().has_value());
	assert(*decoded_replay_hello->replay_epoch() == 3);

	flatbuffers::FlatBufferBuilder transaction_builder(256);
	const VisibilityEventView visibility{"/World/Sphere", true};
	flatbuffers::Offset<OpenUSDConnect::EventWrapper> event;
	assert(BuildVisibilityEvent(transaction_builder, visibility, event) == ProtocolResult::Success);
	assert(FinishTransactionFrame(transaction_builder, 42, &event, 1) == ProtocolResult::Success);

	std::size_t transaction_size = 0;
	assert(TryReadFrameHeader(transaction_builder.GetBufferPointer(), kDefaultMaxFrameSize,
							  transaction_size));
	EnvelopeView transaction_envelope;
	assert(DecodeEnvelope(transaction_builder.GetBufferPointer() + kFrameHeaderSize,
						  transaction_size, transaction_envelope) == ProtocolResult::Success);
	const OpenUSDConnect::Txn* transaction = transaction_envelope.Get()->payload_as_Txn();
	assert(transaction->txn_id() == 42);
	assert(transaction->events()->size() == 1);
	assert(transaction->events()->Get(0)->event_type() ==
		   OpenUSDConnect::EventPayload::SetVisibility);

	flatbuffers::FlatBufferBuilder response_builder(128);
	const auto checkpoint =
		OpenUSDConnect::CreateTransactionCheckpoint(response_builder, 3, 42);
	const auto result = OpenUSDConnect::CreateTransactionResult(
		response_builder, 42, OpenUSDConnect::TransactionStatus::Acknowledged, 0,
		OpenUSDConnect::TransactionRejectionCode::None,
		flatbuffers::Offset<flatbuffers::String>(), checkpoint);
	const auto response_envelope =
		OpenUSDConnect::CreateEnvelope(response_builder, OpenUSDConnect::Payload::TransactionResult,
									   result.Union(), kSchemaVersion);
	OpenUSDConnect::FinishEnvelopeBuffer(response_builder, response_envelope);
	EnvelopeView decoded_response;
	assert(DecodeEnvelope(response_builder.GetBufferPointer(), response_builder.GetSize(),
						  decoded_response) == ProtocolResult::Success);
	const ControlMessageView control(decoded_response);
	assert(control.Kind() == ControlMessageKind::TransactionResult);
	assert(control.TransactionResult()->txn_id() == 42);
	assert(control.TransactionResult()->checkpoint() != nullptr);
	assert(control.TransactionResult()->checkpoint()->epoch() == 3);
	assert(control.TransactionResult()->checkpoint()->head_seq() == 42);
	return 0;
}
