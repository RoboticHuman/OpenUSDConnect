#include "openusdconnect/client/protocol_codec.h"

#include "test_check.h"
#include <cstdint>
#include <string_view>

using namespace openusdconnect::client;

int main()
{
	flatbuffers::FlatBufferBuilder hello_builder(256);
	const HelloParameters hello{"emitter",	0,		 "client", "origin",
								"lighting", "token", false,	   OpenUSDConnect::LayerMode::Managed,
								"producer"};
	CHECK(BuildHelloFrame(hello_builder, hello) == ProtocolResult::Success);

	std::size_t hello_size = 0;
	CHECK(TryReadFrameHeader(hello_builder.GetBufferPointer(), kDefaultMaxFrameSize, hello_size));
	CHECK(hello_size + kFrameHeaderSize == hello_builder.GetSize());
	EnvelopeView hello_envelope;
	CHECK(DecodeEnvelope(hello_builder.GetBufferPointer() + kFrameHeaderSize, hello_size,
						 hello_envelope) == ProtocolResult::Success);
	CHECK(hello_envelope.PayloadType() == OpenUSDConnect::Payload::Hello);
	CHECK(std::string_view(hello_envelope.Get()->payload_as_Hello()->client_id()->c_str()) ==
		  "client");
	CHECK(hello_envelope.Get()->payload_as_Hello()->replay_server_instance() == nullptr);
	CHECK(!hello_envelope.Get()->payload_as_Hello()->replay_epoch().has_value());

	flatbuffers::FlatBufferBuilder replay_hello_builder(256);
	HelloParameters replay_hello{"receiver", 7, "client", "origin"};
	replay_hello.ReplayPrefix = ReplayPrefixIdentity::Known("server-instance", 3);
	CHECK(BuildHelloFrame(replay_hello_builder, replay_hello) == ProtocolResult::Success);
	std::size_t replay_hello_size = 0;
	CHECK(TryReadFrameHeader(replay_hello_builder.GetBufferPointer(), kDefaultMaxFrameSize,
							 replay_hello_size));
	EnvelopeView replay_hello_envelope;
	CHECK(DecodeEnvelope(replay_hello_builder.GetBufferPointer() + kFrameHeaderSize,
						 replay_hello_size, replay_hello_envelope) == ProtocolResult::Success);
	const OpenUSDConnect::Hello* decoded_replay_hello =
		replay_hello_envelope.Get()->payload_as_Hello();
	CHECK(std::string_view(decoded_replay_hello->replay_server_instance()->c_str()) ==
		  "server-instance");
	CHECK(decoded_replay_hello->replay_epoch().has_value());
	CHECK(*decoded_replay_hello->replay_epoch() == 3);

	flatbuffers::FlatBufferBuilder transaction_builder(256);
	const VisibilityEventView visibility{"/World/Sphere", true};
	flatbuffers::Offset<OpenUSDConnect::EventWrapper> event;
	CHECK(BuildVisibilityEvent(transaction_builder, visibility, event) == ProtocolResult::Success);
	CHECK(FinishTransactionFrame(transaction_builder, 42, &event, 1) == ProtocolResult::Success);

	std::size_t transaction_size = 0;
	CHECK(TryReadFrameHeader(transaction_builder.GetBufferPointer(), kDefaultMaxFrameSize,
							 transaction_size));
	EnvelopeView transaction_envelope;
	CHECK(DecodeEnvelope(transaction_builder.GetBufferPointer() + kFrameHeaderSize,
						 transaction_size, transaction_envelope) == ProtocolResult::Success);
	const OpenUSDConnect::Txn* transaction = transaction_envelope.Get()->payload_as_Txn();
	CHECK(transaction->txn_id() == 42);
	CHECK(transaction->events()->size() == 1);
	CHECK(transaction->events()->Get(0)->event_type() ==
		  OpenUSDConnect::EventPayload::SetVisibility);

	flatbuffers::FlatBufferBuilder response_builder(128);
	const auto checkpoint = OpenUSDConnect::CreateTransactionCheckpoint(response_builder, 3, 42);
	const auto result = OpenUSDConnect::CreateTransactionResult(
		response_builder, 42, OpenUSDConnect::TransactionStatus::Acknowledged, 0,
		OpenUSDConnect::TransactionRejectionCode::None, flatbuffers::Offset<flatbuffers::String>(),
		checkpoint);
	const auto response_envelope =
		OpenUSDConnect::CreateEnvelope(response_builder, OpenUSDConnect::Payload::TransactionResult,
									   result.Union(), kSchemaVersion);
	OpenUSDConnect::FinishEnvelopeBuffer(response_builder, response_envelope);
	EnvelopeView decoded_response;
	CHECK(DecodeEnvelope(response_builder.GetBufferPointer(), response_builder.GetSize(),
						 decoded_response) == ProtocolResult::Success);
	const ControlMessageView control(decoded_response);
	CHECK(control.Kind() == ControlMessageKind::TransactionResult);
	CHECK(control.TransactionResult()->txn_id() == 42);
	CHECK(control.TransactionResult()->checkpoint() != nullptr);
	CHECK(control.TransactionResult()->checkpoint()->epoch() == 3);
	CHECK(control.TransactionResult()->checkpoint()->head_seq() == 42);
	return 0;
}
