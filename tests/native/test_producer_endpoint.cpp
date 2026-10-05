#include "openusdconnect/client/engine/producer_endpoint.h"

#include "endpoint_host.h"
#include "producer_frames.h"
#include "test_check.h"

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <variant>
#include <vector>

using namespace openusdconnect::client;
using namespace std::chrono_literals;

namespace
{

using namespace producer_test;

// Plays the host around one endpoint.
class Producer final : public Host<ProducerEndpoint>
{
public:
	explicit Producer(const ProducerConfig& config = TestConfig())
		: Host(config)
	{
	}

	// The frames to send since the last call; any other action fails.
	[[nodiscard]] std::vector<SendAction> SendActions()
	{
		std::vector<SendAction> sends;
		for (Action& action : Commands())
		{
			CHECK(std::holds_alternative<SendAction>(action));
			sends.push_back(std::get<SendAction>(std::move(action)));
		}
		return sends;
	}

	[[nodiscard]] std::vector<Bytes> Sends()
	{
		std::vector<Bytes> frames;
		for (const SendAction& send : SendActions())
		{
			frames.push_back(*send.Bytes);
		}
		return frames;
	}

	// Takes a new attempt's actions and returns its deadline, when the endpoint wakes.
	TimePoint Attempt()
	{
		const ConnectAction connect = Next<ConnectAction>();
		CHECK(connect.Host == "127.0.0.1" && connect.Port == 7200);
		CHECK(Endpoint.NextWake() == connect.Deadline);
		for (const Action& action : Commands())
		{
			CHECK(std::holds_alternative<WakeAction>(action));
		}
		return connect.Deadline;
	}

	// Opens the pending attempt's socket and returns the Hello it sent.
	SentHello Open(std::string_view token = {})
	{
		Endpoint.OnConnected(token);
		return DecodeHello(*Single<SendAction>().Bytes);
	}

	SentHello Request(std::string_view token = {})
	{
		CHECK(Endpoint.RequestConnect(Now, Now + 2s));
		static_cast<void>(Attempt());
		return Open(token);
	}

	// Connects and returns what publication replayed.
	std::vector<Bytes> Handshake(const server::Hello& hello = {})
	{
		static_cast<void>(Request());
		Feed(server::HelloOk(hello));
		CHECK(Status().Connected);
		static_cast<void>(Notices());
		return Sends();
	}

	ProducerResult Submit(std::string_view prim, std::string_view layer_key = {},
						  std::size_t events = 1)
	{
		const std::uint64_t id = Endpoint.NextTransactionId();
		return Endpoint.Append(id, TransactionFrame(id, prim, layer_key), events,
							   std::string(layer_key));
	}

	// Expects the endpoint to close the connection, then reports the close.
	void ExpectClose(DisconnectReason reason)
	{
		CHECK(Single<CloseAction>().Reason == reason);
		Disconnect(reason);
	}
};

void TestConfigurationValidation()
{
	const ProducerConfig valid = TestConfig();
	const std::string two_byte = "\xC3\xA9";
	std::string longest_multibyte;
	for (std::size_t index = 0; index < kMaxProducerSessionIdLength; ++index)
	{
		longest_multibyte += two_byte;
	}
	const std::pair<ProducerConfig, bool> rules[] = {
		{valid, true},
		{With(valid, &ProducerConfig::LayerMode, LayerMode::SharedStage), true},
		{With(valid, &ProducerConfig::Origin, ""), true},
		{With(valid, &ProducerConfig::Host, ""), false},
		{With(valid, &ProducerConfig::Port, 0), false},
		{With(valid, &ProducerConfig::ClientId, ""), false},
		{With(valid, &ProducerConfig::SessionId, ""), false},
		{With(valid, &ProducerConfig::SessionId, std::string(128, 's')), true},
		{With(valid, &ProducerConfig::SessionId, std::string(129, 's')), false},
		{With(valid, &ProducerConfig::SessionId, longest_multibyte), true},
		{With(valid, &ProducerConfig::SessionId, longest_multibyte + two_byte), false},
		{With(valid, &ProducerConfig::HandshakeTimeout, 0ms), false},
		{With(valid, &ProducerConfig::MaxPendingTransactions, 0), false},
		{With(valid, &ProducerConfig::ReconnectBaseDelay, 0ms), false},
		{With(valid, &ProducerConfig::ReconnectMaxDelay, 999ms), false},
		{With(valid, &ProducerConfig::LayerMode, static_cast<LayerMode>(2)), false},
		{With(With(valid, &ProducerConfig::LayerMode, LayerMode::SharedStage),
			  &ProducerConfig::Department, "layout"),
		 false},
	};
	for (const auto& [config, expected] : rules)
	{
		CHECK(ProducerEndpoint::IsValidConfiguration(config) == expected);
	}
}

void TestAttemptsAreBoundedAndExclusive()
{
	Producer producer;
	CHECK(!producer.Status().Handshaking && !producer.Endpoint.NextWake());
	CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	CHECK(producer.Attempt() == producer.Now + 2s);
	CHECK(producer.Endpoint.NextWake() == producer.Now + 2s);
	CHECK(producer.Status().Handshaking && !producer.Status().Connected);
	CHECK(!producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	CHECK(producer.Endpoint.Connect(producer.Now, producer.Now + 2s) == ConnectResult::Busy);
	static_cast<void>(producer.Open());
	CHECK(producer.Status().Handshaking);
	CHECK(producer.Endpoint.Connect(producer.Now, producer.Now + 2s) == ConnectResult::Busy);
	producer.Feed(server::HelloOk());
	CHECK(producer.Endpoint.Connect(producer.Now, producer.Now) == ConnectResult::Connected);
	CHECK(!producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	CHECK(!producer.Endpoint.NextWake());

	// The configured handshake timeout caps a longer deadline.
	Producer patient;
	CHECK(patient.Endpoint.Connect(patient.Now, patient.Now + 1h) == ConnectResult::Started);
	CHECK(patient.Attempt() == patient.Now + 10s);

	// No attempt starts without time left.
	Producer hurried;
	CHECK(!hurried.Endpoint.RequestConnect(hurried.Now, hurried.Now));
	CHECK(hurried.Endpoint.Connect(hurried.Now, hurried.Now - 1ms) == ConnectResult::Refused);
	CHECK(hurried.Commands().empty());
}

void TestHelloCarriesTheProducerIdentity()
{
	ProducerConfig config = TestConfig();
	config.Department = "layout";
	Producer producer(config);
	const SentHello hello = producer.Request("token-1");
	CHECK(hello.Role == "emitter");
	CHECK(hello.ProtocolVersion == kProtocolVersion);
	CHECK(hello.SyncFrom == 0);
	CHECK(hello.ClientId == "client");
	CHECK(hello.Origin == "origin");
	CHECK(hello.Department == "layout");
	CHECK(hello.Token == "token-1");
	CHECK(!hello.LayeredReplay);
	CHECK(hello.Mode == LayerMode::Managed);
	CHECK(hello.ProducerSessionId == "session");
	CHECK(!hello.ReplayServerInstance && !hello.ReplayEpoch);

	config = TestConfig();
	config.LayerMode = LayerMode::SharedStage;
	config.Origin.clear();
	Producer shared(config);
	const SentHello shared_hello = shared.Request();
	CHECK(shared_hello.Mode == LayerMode::SharedStage);
	CHECK(shared_hello.Origin.empty() && shared_hello.Token.empty());
}

void TestAcceptedHelloNotifiesThenPublishes()
{
	Producer producer;
	static_cast<void>(producer.Request());
	StageMetadata metadata;
	metadata.MetersPerUnit = 0.01;
	metadata.UpAxis = "Y";
	server::Hello hello;
	hello.Token = "issued";
	hello.Metadata = metadata;
	producer.Feed(server::HelloOk(hello));

	const std::vector<Notification> notices = producer.Notices();
	CHECK(notices.size() == 3);
	CHECK(As<TokenIssued>(notices[0]).Token == "issued");
	CHECK(As<StageMetadata>(notices[1]).MetersPerUnit == 0.01);
	static_cast<void>(As<Connected>(notices[2]));
	const ProducerStatus status = producer.Status();
	CHECK(status.Connected && !status.Handshaking && !status.Rejection && !status.Failure);
	CHECK(status.Metadata.UpAxis == "Y" && !status.Metadata.TimeCodesPerSecond);
	CHECK(status.LayerModeActive == LayerMode::Managed);
	CHECK(status.SessionId == "session");

	// Metadata the server did not author is neither notified nor kept.
	Producer bare;
	hello.Metadata = StageMetadata{};
	hello.Token.clear();
	static_cast<void>(bare.Request());
	bare.Feed(server::HelloOk(hello));
	static_cast<void>(bare.Notice<Connected>());
	CHECK(!bare.Status().Metadata.UpAxis);
}

void TestHandshakeRejectionsHoldUntilAnExplicitConnect()
{
	{
		Producer producer;
		static_cast<void>(producer.Request());
		producer.Feed(server::AuthRejected("invalid token"));
		CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::HandshakeRejected);
		const HandshakeRejected rejected = producer.Notice<HandshakeRejected>();
		CHECK(rejected.Authentication && rejected.Reason == "invalid token");
		producer.Disconnect();
		const ProducerStatus status = producer.Status();
		CHECK(!status.Connected && !status.Handshaking && !status.Stopped && !status.Failure);
		CHECK(status.Rejection && status.Rejection->Authentication);

		// The backoff has passed, yet only an explicit connect retries.
		producer.Now += 1h;
		CHECK(!producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
		CHECK(producer.Endpoint.Connect(producer.Now, producer.Now + 2s) == ConnectResult::Started);
		CHECK(!producer.Status().Rejection);
		static_cast<void>(producer.Attempt());
		static_cast<void>(producer.Open());
		producer.Feed(server::HelloOk());
		CHECK(producer.Status().Connected);
	}
	// An empty reason stays empty; hosts word their own default.
	const std::pair<Bytes, HandshakeRejected> rejections[] = {
		{server::HelloRejected(HelloRejectionCode::Unspecified, ""),
		 {false, HelloRejectionCode::Unspecified, ""}},
		{server::HelloRejected(HelloRejectionCode::LayerModeMismatch, "server uses shared_stage"),
		 {false, HelloRejectionCode::LayerModeMismatch, "server uses shared_stage"}},
	};
	for (const auto& [frame, expected] : rejections)
	{
		Producer producer;
		static_cast<void>(producer.Request());
		producer.Feed(frame);
		producer.ExpectClose(DisconnectReason::HandshakeRejected);
		const std::optional<HandshakeRejected> rejection = producer.Status().Rejection;
		CHECK(rejection && !rejection->Authentication);
		CHECK(rejection->Code == expected.Code && rejection->Reason == expected.Reason);
		CHECK(!producer.Endpoint.RequestConnect(producer.Now + 1h, producer.Now + 2h));
	}
}

void TestLayerModeMismatchIsARejection()
{
	const std::pair<LayerMode, std::string_view> cases[] = {
		{LayerMode::Managed, "server negotiated shared_stage instead of managed"},
		{LayerMode::SharedStage, "server negotiated managed instead of shared_stage"},
	};
	for (const auto& [requested, reason] : cases)
	{
		Producer producer(With(TestConfig(), &ProducerConfig::LayerMode, requested));
		static_cast<void>(producer.Request());
		server::Hello hello;
		hello.Mode = requested == LayerMode::Managed ? LayerMode::SharedStage : LayerMode::Managed;
		hello.Token = "not-issued";
		producer.Feed(server::HelloOk(hello));
		CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::HandshakeRejected);
		const HandshakeRejected rejected = producer.Notice<HandshakeRejected>();
		CHECK(!rejected.Authentication);
		CHECK(rejected.Code == HelloRejectionCode::LayerModeMismatch);
		CHECK(rejected.Reason == reason);
		CHECK(producer.Status().LayerModeActive == LayerMode::Managed);
	}
}

void CheckSessionFailure(Producer& producer, std::uint64_t transaction_id, std::string_view reason)
{
	const std::optional<TransactionFailure> failure = producer.Endpoint.Failure();
	CHECK(failure);
	CHECK(failure->TransactionId == transaction_id);
	CHECK(failure->Code == static_cast<std::uint8_t>(RejectionCode::UnexpectedId));
	CHECK(failure->ExpectedTransactionId == 0);
	CHECK(failure->Reason == reason);
	CHECK(failure->Disposition() == ProducerRecoveryDisposition::SessionFatal);
}

void TestHelloHighwaterAheadRequiresRecovery()
{
	Producer producer;
	static_cast<void>(producer.Request());
	server::Hello hello;
	hello.CommittedThrough = 1;
	hello.Token = "not-issued";
	producer.Feed(server::HelloOk(hello));
	CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::RecoveryRequired);
	CheckSessionFailure(producer, 1, "server producer highwater 1 is ahead of local transaction 0");
	CHECK(producer.Endpoint.Failure()->Describe() ==
		  "transaction 1 rejected (unexpected_id): server producer highwater 1 is ahead of local "
		  "transaction 0");
	CHECK(producer.Notices().empty());
	producer.Disconnect();
	const ProducerStatus status = producer.Status();
	CHECK(!status.Connected && !status.Rejection && status.Failure);
	CHECK(!producer.Endpoint.RequestConnect(producer.Now + 1h, producer.Now + 2h));
	CHECK(producer.Endpoint.Connect(producer.Now + 1h, producer.Now + 2h) ==
		  ConnectResult::Refused);
	const std::optional<RecoveryArtifact> artifact = producer.Endpoint.Artifact();
	CHECK(artifact && artifact->SessionId == "session" && artifact->Transactions.empty());

	// A session ahead of its local outbox keeps the outbox as evidence.
	Producer behind;
	static_cast<void>(behind.Handshake());
	CHECK(behind.Submit("/A") == ProducerResult::Accepted);
	CHECK(behind.Submit("/B") == ProducerResult::Accepted);
	static_cast<void>(behind.Sends());
	behind.Disconnect();
	static_cast<void>(behind.Request());
	hello.CommittedThrough = 3;
	behind.Feed(server::HelloOk(hello));
	behind.ExpectClose(DisconnectReason::RecoveryRequired);
	CheckSessionFailure(behind, 3, "server producer highwater 3 is ahead of local transaction 2");
	CHECK(behind.Endpoint.Artifact()->Transactions.size() == 2);
}

void TestHelloHighwaterRegressionRequiresRecovery()
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	CHECK(producer.Submit("/A") == ProducerResult::Accepted);
	CHECK(producer.Submit("/B") == ProducerResult::Accepted);
	static_cast<void>(producer.Sends());
	producer.Feed(server::Acknowledged(2));
	producer.Disconnect();
	static_cast<void>(producer.Request());
	server::Hello hello;
	hello.CommittedThrough = 1;
	producer.Feed(server::HelloOk(hello));
	producer.ExpectClose(DisconnectReason::RecoveryRequired);
	CheckSessionFailure(producer, 1, "server producer highwater regressed from 2 to 1");
	CHECK(producer.Endpoint.Artifact()->Transactions.empty());
}

void TestHandshakeProtocolErrorsAreFailedAttempts()
{
	flatbuffers::FlatBufferBuilder old_schema(32);
	const Bytes cases[] = {
		server::Ping(),
		Bytes{0, 0, 0, 2, 0xFF, 0xFF},
		server::Frame(old_schema, Payload::Ping, OpenUSDConnect::CreatePing(old_schema).Union(),
					  kSchemaVersion - 1),
		Bytes{0, 0, 0, 0},
	};
	for (const Bytes& bytes : cases)
	{
		Producer producer;
		static_cast<void>(producer.Request());
		producer.Feed(bytes);
		CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::ProtocolError);
		producer.Disconnect();
		const ProducerStatus status = producer.Status();
		CHECK(!status.Connected && !status.Rejection && !status.Failure);
		CHECK(producer.Notices().empty());
		// A failed request backs off before the next one.
		CHECK(!producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
		CHECK(producer.Endpoint.RequestConnect(producer.Now + 1s, producer.Now + 3s));
	}
}

void TestHandshakeDeadline()
{
	{
		Producer producer;
		CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
		static_cast<void>(producer.Attempt());
		producer.Advance(1999ms);
		CHECK(producer.Commands().empty());
		producer.Advance(1ms);
		CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::HandshakeTimeout);
		CHECK(!producer.Status().Handshaking && !producer.Endpoint.NextWake());
		// The host's connect finishing late opens nothing.
		producer.Endpoint.OnConnected({});
		CHECK(producer.Commands().empty());
		producer.Disconnect(DisconnectReason::ConnectFailed);
		CHECK(!producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	}
	{
		Producer producer;
		static_cast<void>(producer.Request());
		producer.Advance(2s);
		CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::HandshakeTimeout);
		producer.Feed(server::HelloOk());
		CHECK(!producer.Status().Connected && producer.Notices().empty());
		producer.Disconnect();
		producer.Now += 1s;
		static_cast<void>(producer.Handshake());
		CHECK(producer.Submit("/A") == ProducerResult::Accepted);
	}
}

void TestRequestBackoffDoublesAndResetsOnPublication()
{
	Producer producer;
	for (const std::chrono::milliseconds delay : {1s, 2s, 4s, 8s, 8s})
	{
		CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
		static_cast<void>(producer.Attempt());
		producer.Disconnect(DisconnectReason::ConnectFailed);
		CHECK(!producer.Endpoint.RequestConnect(producer.Now + delay - 1ms, producer.Now + 1h));
		producer.Now += delay;
	}
	static_cast<void>(producer.Handshake());
	producer.Disconnect();
	// A lost connection is not a failed attempt, and publication reset the delay.
	CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	static_cast<void>(producer.Attempt());
	producer.Disconnect(DisconnectReason::ConnectFailed);
	CHECK(!producer.Endpoint.RequestConnect(producer.Now + 999ms, producer.Now + 1h));
	CHECK(producer.Endpoint.RequestConnect(producer.Now + 1s, producer.Now + 1h));
}

void TestExplicitConnectNeitherWaitsForNorExtendsTheBackoff()
{
	Producer producer;
	CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	static_cast<void>(producer.Attempt());
	producer.Disconnect(DisconnectReason::ConnectFailed);
	CHECK(producer.Endpoint.Connect(producer.Now, producer.Now + 2s) == ConnectResult::Started);
	static_cast<void>(producer.Attempt());
	producer.Disconnect(DisconnectReason::ConnectFailed);
	CHECK(producer.Endpoint.RequestConnect(producer.Now + 1s, producer.Now + 1h));
}

void TestCancelAndDisconnectResetTheBackoff()
{
	for (const bool cancel : {true, false})
	{
		Producer producer;
		CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
		static_cast<void>(producer.Attempt());
		producer.Disconnect(DisconnectReason::ConnectFailed);
		CHECK(!producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
		if (cancel)
		{
			CHECK(producer.Endpoint.CancelConnect());
		}
		else
		{
			producer.Endpoint.Disconnect();
		}
		CHECK(producer.Commands().empty());
		CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	}
}

// A cancelled handshake must not end a later attempt's session generation or
// accept its own late HelloOk, either of which quarantines a healthy session.
void TestCancelDuringHandshakeKeepsTheSessionHealthy(bool disconnect)
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	CHECK(producer.Submit("/Unsent") == ProducerResult::Accepted);
	const Bytes unsent = producer.Sends().at(0);
	producer.Disconnect();
	CHECK(producer.Notice<Disconnected>().Reason == DisconnectReason::PeerClosed);

	static_cast<void>(producer.Request());
	bool finished = false;
	if (disconnect)
	{
		producer.Endpoint.Disconnect();
	}
	else
	{
		finished = producer.Endpoint.CancelConnect();
	}
	// The HelloOk was already in flight when the host cancelled.
	producer.Feed(server::HelloOk());
	const ProducerStatus status = producer.Status();
	CHECK(!status.Connected && !status.Handshaking && !status.Failure);
	CHECK(status.Closing && !finished);
	CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::Cancelled);
	CHECK(producer.Notices().empty());
	CHECK(!producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	CHECK(producer.Endpoint.Connect(producer.Now, producer.Now + 2s) == ConnectResult::Busy);
	producer.Disconnect(DisconnectReason::Cancelled);
	CHECK(!producer.Status().Closing);
	CHECK(producer.Endpoint.CancelConnect());

	CHECK(producer.Handshake() == std::vector<Bytes>{unsent});
	CHECK(producer.Submit("/After") == ProducerResult::Accepted);
	CHECK(TransactionIds(producer.Sends()) == std::vector<std::uint64_t>{2});
	CHECK(!producer.Status().Failure);
}

void TestCancelBeforeTheSocketOpens()
{
	Producer producer;
	CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	static_cast<void>(producer.Attempt());
	CHECK(!producer.Endpoint.CancelConnect());
	CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::Cancelled);
	// The host's connect finished before it applied the close.
	producer.Endpoint.OnConnected({});
	CHECK(producer.Commands().empty());
	producer.Disconnect(DisconnectReason::Cancelled);
	static_cast<void>(producer.Handshake());
	CHECK(producer.Submit("/A") == ProducerResult::Accepted);

	// Cancelling leaves a published connection intact.
	CHECK(producer.Endpoint.CancelConnect());
	CHECK(producer.Status().Connected);
	CHECK(TransactionIds(producer.Sends()) == std::vector<std::uint64_t>{1});
}

// The host may report a socket's end before it applies the close queued for
// it. Applying that close later would end the next attempt in its place.
void TestAReportedEndVoidsActionsQueuedForTheConnection()
{
	Producer producer;
	CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	static_cast<void>(producer.Attempt());
	CHECK(!producer.Endpoint.CancelConnect());
	producer.Disconnect(DisconnectReason::ConnectFailed);
	CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	static_cast<void>(producer.Attempt());
	static_cast<void>(producer.Open());
	producer.Feed(server::HelloOk());
	CHECK(producer.Status().Connected);

	CHECK(producer.Submit("/A") == ProducerResult::Accepted);
	producer.Endpoint.Disconnect();
	producer.Disconnect();
	CHECK(producer.Commands().empty());
	CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	static_cast<void>(producer.Attempt());
}

// An attempt the host has not taken ends at once, so its close cannot share
// a batch with its connect and outlive it.
void TestAnUntakenAttemptIsWithdrawn()
{
	for (int end = 0; end < 4; ++end)
	{
		Producer producer;
		CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
		switch (end)
		{
		case 0:
			CHECK(producer.Endpoint.CancelConnect());
			break;
		case 1:
			producer.Endpoint.Disconnect();
			break;
		case 2:
			producer.Advance(2s);
			break;
		default:
			producer.Endpoint.Stop();
			break;
		}
		CHECK(producer.Commands().empty());
		const ProducerStatus status = producer.Status();
		CHECK(!status.Handshaking && !status.Closing && !producer.Endpoint.NextWake());
		CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s) == (end != 3));
	}
}

void TestPublicationReplaysUnsentFramesInOrderWithoutCopies()
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	CHECK(producer.Submit("/A", "", 1) == ProducerResult::Accepted);
	CHECK(producer.Submit("/B", "", 2) == ProducerResult::Accepted);
	CHECK(producer.Submit("/C", "", 3) == ProducerResult::Accepted);
	const std::vector<SendAction> first = producer.SendActions();
	CHECK(first.size() == 3);
	producer.Feed(server::Acknowledged(1));
	producer.Disconnect();
	CHECK(producer.Notice<Disconnected>().Reason == DisconnectReason::PeerClosed);
	ProducerStatus status = producer.Status();
	CHECK(status.PendingTransactions == 2 && status.PendingEvents == 5);

	static_cast<void>(producer.Request());
	server::Hello hello;
	hello.CommittedThrough = 2;
	producer.Feed(server::HelloOk(hello));
	const std::vector<SendAction> replayed = producer.SendActions();
	CHECK(replayed.size() == 1);
	CHECK(replayed[0].Bytes == first[2].Bytes);
	status = producer.Status();
	CHECK(status.PendingTransactions == 1 && status.PendingEvents == 3);
	CHECK(status.AcknowledgedTransactions == 2 && status.AcknowledgedEvents == 3);

	CHECK(producer.Submit("/D") == ProducerResult::Accepted);
	producer.Feed(server::Acknowledged(4));
	CHECK(producer.Endpoint.OutboxEmpty());
	CHECK(producer.Endpoint.DrainAcknowledgedEventCount() == 7);
	CHECK(producer.Endpoint.DrainAcknowledgedEventCount() == 0);
	CHECK(producer.Status().NextTransactionId == 5);
}

void TestReplayOrderSurvivesSeveralLostConnections()
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	for (const std::string_view prim : {"/A", "/B", "/C"})
	{
		CHECK(producer.Submit(prim) == ProducerResult::Accepted);
	}
	const std::vector<Bytes> sent = producer.Sends();
	for (int attempt = 0; attempt < 2; ++attempt)
	{
		producer.Disconnect();
		CHECK(producer.Handshake() == sent);
	}
}

void TestAppendRequiresAPublishedHealthyConnection()
{
	ProducerConfig config = TestConfig();
	config.MaxPendingTransactions = 2;
	Producer producer(config);
	CHECK(producer.Submit("/A") == ProducerResult::InvalidPhase);
	CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	static_cast<void>(producer.Attempt());
	CHECK(producer.Submit("/A") == ProducerResult::InvalidPhase);
	static_cast<void>(producer.Open());
	CHECK(producer.Submit("/A") == ProducerResult::InvalidPhase);
	CHECK(producer.Commands().empty());
	CHECK(producer.Endpoint.NextTransactionId() == 1 && producer.Endpoint.OutboxEmpty());

	producer.Feed(server::HelloOk());
	static_cast<void>(producer.Notices());
	const Bytes frame = TransactionFrame(1, "/A");
	CHECK(producer.Endpoint.Append(2, TransactionFrame(2, "/A"), 1, "") ==
		  ProducerResult::SequenceMismatch);
	CHECK(producer.Endpoint.Append(1, frame, 0, "") == ProducerResult::InvalidArgument);
	CHECK(producer.Endpoint.Append(1, Bytes(frame.begin() + kFrameHeaderSize, frame.end()), 1,
								   "") == ProducerResult::InvalidArgument);
	CHECK(producer.Endpoint.Append(1, Bytes(frame.begin(), frame.end() - 1), 1, "") ==
		  ProducerResult::InvalidArgument);
	CHECK(producer.Endpoint.Append(1, {}, 1, "") == ProducerResult::InvalidArgument);
	CHECK(producer.Commands().empty());

	CHECK(producer.Endpoint.Append(1, frame, 1, "") == ProducerResult::Accepted);
	CHECK(producer.Sends() == std::vector<Bytes>{frame});
	CHECK(producer.Submit("/B") == ProducerResult::Accepted);
	CHECK(producer.Submit("/C") == ProducerResult::OutboxFull);
	CHECK(TransactionIds(producer.Sends()) == std::vector<std::uint64_t>{2});
	producer.Feed(server::Acknowledged(1));
	CHECK(producer.Submit("/C") == ProducerResult::Accepted);
	static_cast<void>(producer.Sends());

	producer.Disconnect();
	CHECK(producer.Submit("/D") == ProducerResult::InvalidPhase);
	server::Hello hello;
	hello.CommittedThrough = 1;
	static_cast<void>(producer.Handshake(hello));
	producer.Feed(server::Rejected(2, TransactionRejectionCode::InvalidTransaction, "bad"));
	producer.ExpectClose(DisconnectReason::RecoveryRequired);
	CHECK(producer.Submit("/D") == ProducerResult::RecoveryRequired);
	CHECK(producer.Status().PendingTransactions == 2);
}

void TestAcknowledgedCheckpointNeedsAnEmptyOutbox()
{
	Producer producer;
	server::Hello hello;
	hello.ServerInstance = "server-a";
	static_cast<void>(producer.Handshake(hello));
	CHECK(!producer.Endpoint.AcknowledgedCheckpoint());
	CHECK(producer.Submit("/A") == ProducerResult::Accepted);
	CHECK(producer.Submit("/B") == ProducerResult::Accepted);
	producer.Feed(server::Acknowledged(1, server::Checkpoint{2, 8}));
	CHECK(!producer.Endpoint.AcknowledgedCheckpoint());
	producer.Feed(server::Acknowledged(2, server::Checkpoint{3, 9}));
	std::optional<MirrorCheckpoint> checkpoint = producer.Endpoint.AcknowledgedCheckpoint();
	CHECK(checkpoint && checkpoint->ServerInstance == "server-a");
	CHECK(checkpoint->Epoch == 3 && checkpoint->HeadSequence == 9);

	CHECK(producer.Submit("/C") == ProducerResult::Accepted);
	CHECK(!producer.Endpoint.AcknowledgedCheckpoint());
	producer.Feed(server::Acknowledged(3));
	CHECK(producer.Endpoint.OutboxEmpty() && !producer.Endpoint.AcknowledgedCheckpoint());

	CHECK(producer.Submit("/D") == ProducerResult::Accepted);
	producer.Feed(server::Acknowledged(4, server::Checkpoint{4, 10}));
	CHECK(producer.Endpoint.AcknowledgedCheckpoint());
	CHECK((TransactionIds(producer.Sends()) == std::vector<std::uint64_t>{1, 2, 3, 4}));
	// A Hello acknowledges no mirror position, even with nothing pending.
	producer.Disconnect();
	CHECK(producer.Endpoint.AcknowledgedCheckpoint());
	static_cast<void>(producer.Request());
	hello.ServerInstance = "server-b";
	hello.CommittedThrough = 4;
	producer.Feed(server::HelloOk(hello));
	CHECK(producer.Status().Connected && !producer.Endpoint.AcknowledgedCheckpoint());

	// Without a server instance, a checkpoint cannot name its sequence domain.
	Producer anonymous;
	hello = {};
	hello.ServerInstance.clear();
	static_cast<void>(anonymous.Handshake(hello));
	CHECK(anonymous.Submit("/A") == ProducerResult::Accepted);
	anonymous.Feed(server::Acknowledged(1, server::Checkpoint{1, 1}));
	CHECK(anonymous.Endpoint.OutboxEmpty() && !anonymous.Endpoint.AcknowledgedCheckpoint());
}

void TestAcknowledgementHighwaterFailures()
{
	{
		Producer producer;
		static_cast<void>(producer.Handshake());
		CHECK(producer.Submit("/A") == ProducerResult::Accepted);
		static_cast<void>(producer.Sends());
		producer.Feed(server::Acknowledged(9));
		CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::RecoveryRequired);
		CheckSessionFailure(producer, 9,
							"server producer highwater 9 is ahead of local transaction 1");
		CHECK(producer.Notice<Disconnected>().Reason == DisconnectReason::RecoveryRequired);
	}
	{
		Producer producer;
		static_cast<void>(producer.Handshake());
		CHECK(producer.Submit("/A") == ProducerResult::Accepted);
		CHECK(producer.Submit("/B") == ProducerResult::Accepted);
		CHECK(producer.Submit("/C") == ProducerResult::Accepted);
		producer.Feed(server::Acknowledged(2));
		producer.Feed(server::Acknowledged(1));
		CheckSessionFailure(producer, 1, "server producer highwater regressed from 2 to 1");
		CHECK(producer.Endpoint.Artifact()->Transactions.size() == 1);
	}
}

struct DispositionCase final
{
	TransactionRejectionCode Code;
	ProducerRecoveryDisposition Disposition;
	std::string_view Description;
};

void TestRejectionQuarantinesTheOutbox(const DispositionCase& expected)
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	CHECK(producer.Submit("/A", "layer-a", 1) == ProducerResult::Accepted);
	CHECK(producer.Submit("/B", "layer-b", 2) == ProducerResult::Accepted);
	const std::vector<Bytes> sent = producer.Sends();
	// Frames after a rejection in the same read are not handled.
	producer.Feed(Concatenate(
		{server::Rejected(1, expected.Code, "layer was remapped", 1), server::Acknowledged(1)}));
	CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::RecoveryRequired);
	CHECK(producer.Notice<Disconnected>().Reason == DisconnectReason::RecoveryRequired);

	const std::optional<TransactionFailure> failure = producer.Endpoint.Failure();
	CHECK(failure && failure->TransactionId == 1 && failure->ExpectedTransactionId == 1);
	CHECK(failure->Code == static_cast<std::uint8_t>(expected.Code));
	CHECK(failure->Reason == "layer was remapped");
	CHECK(failure->Disposition() == expected.Disposition);
	CHECK(failure->Describe() == expected.Description);

	CHECK(producer.Submit("/C") == ProducerResult::RecoveryRequired);
	CHECK(!producer.Endpoint.QueueControl(ClaimFrame()));
	producer.Disconnect();
	const ProducerStatus status = producer.Status();
	CHECK(!status.Connected && status.Failure && status.PendingTransactions == 2);
	CHECK(!producer.Endpoint.AcknowledgedCheckpoint());
	CHECK(!producer.Endpoint.RequestConnect(producer.Now + 1h, producer.Now + 2h));
	CHECK(producer.Endpoint.Connect(producer.Now, producer.Now + 2s) == ConnectResult::Refused);

	const std::optional<RecoveryArtifact> artifact = producer.Endpoint.Artifact();
	CHECK(artifact && artifact->SessionId == "session");
	CHECK(artifact->Failure.Describe() == expected.Description);
	CHECK(artifact->Transactions.size() == 2);
	const std::pair<std::string_view, std::size_t> transactions[] = {{"layer-a", 1},
																	 {"layer-b", 2}};
	for (std::size_t index = 0; index < 2; ++index)
	{
		const ProducerSessionEntry& entry = artifact->Transactions[index];
		CHECK(entry.TransactionId == index + 1);
		CHECK(*entry.Payload == sent[index]);
		CHECK(entry.LayerKey == transactions[index].first);
		CHECK(entry.EventCount == transactions[index].second);
	}
}

void TestRejectionWithoutReasonOrKnownCode()
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	CHECK(producer.Submit("/A") == ProducerResult::Accepted);
	producer.Feed(server::Rejected(1, static_cast<TransactionRejectionCode>(9), ""));
	const std::optional<TransactionFailure> failure = producer.Endpoint.Failure();
	CHECK(failure && failure->Code == 9);
	CHECK(failure->Disposition() == ProducerRecoveryDisposition::SessionFatal);
	CHECK(failure->Describe() == "transaction 1 rejected (unknown_9): no reason supplied");
}

void TestRejectionOfAnUnknownTransaction()
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	CHECK(producer.Submit("/A") == ProducerResult::Accepted);
	CHECK(TransactionIds(producer.Sends()) == std::vector<std::uint64_t>{1});
	producer.Feed(server::Rejected(7, TransactionRejectionCode::StaleLayerGraph, "stale", 1));
	producer.ExpectClose(DisconnectReason::RecoveryRequired);
	const std::optional<TransactionFailure> failure = producer.Endpoint.Failure();
	CHECK(failure && failure->TransactionId == 7 && failure->ExpectedTransactionId == 0);
	CHECK(failure->Code == static_cast<std::uint8_t>(RejectionCode::UnexpectedId));
	CHECK(failure->Reason == "server rejected unknown transaction 7");
	CHECK(failure->Disposition() == ProducerRecoveryDisposition::SessionFatal);
	CHECK(producer.Endpoint.RepairRejected(TransactionFrame(7, "/A"), 1, "") ==
		  ProducerResult::RecoveryNotRecoverable);
}

void TestRateLimitClosesAndOpensARetryWindow()
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	CHECK(producer.Submit("/A") == ProducerResult::Accepted);
	const std::vector<Bytes> sent = producer.Sends();
	producer.Feed(server::RateLimited(1.5F));
	CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::RateLimited);
	CHECK(producer.Notice<Disconnected>().Reason == DisconnectReason::RateLimited);
	// The window starts once the close is reported.
	producer.Now += 100ms;
	producer.Disconnect();
	CHECK(producer.Status().RetryAfter == producer.Now + 1500ms);
	CHECK(!producer.Endpoint.RequestConnect(producer.Now + 1499ms, producer.Now + 1h));
	CHECK(producer.Endpoint.Connect(producer.Now + 1499ms, producer.Now + 1h) ==
		  ConnectResult::Refused);
	producer.Now += 1500ms;
	CHECK(producer.Handshake() == sent);
	CHECK(!producer.Status().Failure);

	const std::pair<float, std::chrono::steady_clock::duration> hostile[] = {
		{-1.0F, std::chrono::steady_clock::duration::zero()},
		{std::numeric_limits<float>::quiet_NaN(), std::chrono::steady_clock::duration::zero()},
		{std::numeric_limits<float>::infinity(), 1h},
		{1e30F, 1h},
	};
	for (const auto& [seconds, window] : hostile)
	{
		Producer limited;
		static_cast<void>(limited.Handshake());
		limited.Feed(server::RateLimited(seconds));
		limited.ExpectClose(DisconnectReason::RateLimited);
		CHECK(limited.Status().RetryAfter == limited.Now + window);
		CHECK(limited.Endpoint.RequestConnect(limited.Now + window, limited.Now + window + 1s));
	}
}

void TestRepairReplacesTheRejectedTransaction()
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	CHECK(producer.Endpoint.RepairRejected(TransactionFrame(1, "/A"), 1, "") ==
		  ProducerResult::InvalidPhase);
	CHECK(producer.Submit("/Stale", "old-layer") == ProducerResult::Accepted);
	CHECK(producer.Submit("/Later", "stable-layer") == ProducerResult::Accepted);
	const std::vector<Bytes> sent = producer.Sends();
	producer.Feed(server::Rejected(1, TransactionRejectionCode::StaleLayerGraph, "remapped"));
	producer.ExpectClose(DisconnectReason::RecoveryRequired);

	const Bytes repaired = TransactionFrame(1, "/Repaired", "new-layer");
	CHECK(producer.Endpoint.RepairRejected(Bytes{0, 0, 0, 1}, 1, "") ==
		  ProducerResult::InvalidArgument);
	CHECK(producer.Endpoint.RepairRejected(repaired, 0, "new-layer") ==
		  ProducerResult::InvalidArgument);
	CHECK(producer.Endpoint.Failure());
	CHECK(producer.Endpoint.RepairRejected(repaired, 2, "new-layer") == ProducerResult::Accepted);
	const ProducerStatus status = producer.Status();
	CHECK(!status.Failure && !producer.Endpoint.Artifact());
	CHECK(status.PendingTransactions == 2 && status.PendingEvents == 3);
	CHECK(status.NextTransactionId == 3);

	CHECK(producer.Handshake() == (std::vector<Bytes>{repaired, sent[1]}));
	producer.Feed(server::Acknowledged(2));
	CHECK(producer.Endpoint.OutboxEmpty());

	Producer invalid;
	static_cast<void>(invalid.Handshake());
	CHECK(invalid.Submit("/A") == ProducerResult::Accepted);
	invalid.Feed(server::Rejected(1, TransactionRejectionCode::InvalidTransaction, "bad"));
	CHECK(invalid.Endpoint.RepairRejected(TransactionFrame(1, "/A"), 1, "") ==
		  ProducerResult::RecoveryNotRecoverable);
	CHECK(invalid.Endpoint.Failure());
}

void TestAbandonContinuesAsAFreshSession()
{
	Producer producer;
	CHECK(!producer.Endpoint.AbandonRejectedSession("replacement"));
	static_cast<void>(producer.Handshake());
	CHECK(producer.Submit("/Rejected") == ProducerResult::Accepted);
	CHECK(producer.Submit("/Suffix") == ProducerResult::Accepted);
	static_cast<void>(producer.Sends());
	producer.Feed(server::Rejected(1, TransactionRejectionCode::InvalidTransaction, "injected"));
	producer.ExpectClose(DisconnectReason::RecoveryRequired);

	CHECK(!producer.Endpoint.AbandonRejectedSession(""));
	CHECK(!producer.Endpoint.AbandonRejectedSession(std::string(129, 's')));
	CHECK(!producer.Endpoint.AbandonRejectedSession("session"));
	CHECK(producer.Endpoint.Failure());

	const std::optional<RecoveryArtifact> artifact =
		producer.Endpoint.AbandonRejectedSession("replacement");
	CHECK(artifact && artifact->SessionId == "session");
	CHECK(artifact->Failure.Code ==
		  static_cast<std::uint8_t>(TransactionRejectionCode::InvalidTransaction));
	CHECK(artifact->Transactions.size() == 2);
	const ProducerStatus status = producer.Status();
	CHECK(status.SessionId == "replacement" && !status.Failure);
	CHECK(status.PendingTransactions == 0 && status.NextTransactionId == 1);
	CHECK(!producer.Endpoint.Artifact());
	CHECK(!producer.Endpoint.AbandonRejectedSession("another"));

	CHECK(producer.Request().ProducerSessionId == "replacement");
	producer.Feed(server::HelloOk());
	CHECK(producer.Status().Connected && producer.Sends().empty());
	CHECK(producer.Submit("/Rebuilt") == ProducerResult::Accepted);
	CHECK(TransactionIds(producer.Sends()) == std::vector<std::uint64_t>{1});
}

void TestDisconnectSaysQuitAndKeepsTheOutbox()
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	CHECK(producer.Submit("/A") == ProducerResult::Accepted);
	const std::vector<Bytes> sent = producer.Sends();
	producer.Endpoint.Disconnect();
	CHECK(DecodeSent(*producer.Next<SendAction>().Bytes).payload_type() == Payload::Quit);
	CHECK(producer.Next<CloseAction>().Reason == DisconnectReason::Cancelled);
	CHECK(producer.Notice<Disconnected>().Reason == DisconnectReason::Cancelled);
	CHECK(!producer.Status().Connected);
	CHECK(producer.Submit("/B") == ProducerResult::InvalidPhase);
	CHECK(!producer.Endpoint.QueueControl(ClaimFrame()));
	CHECK(!producer.Endpoint.CancelConnect());
	producer.Disconnect(DisconnectReason::Cancelled);

	producer.Endpoint.Disconnect();
	CHECK(producer.Commands().empty());
	CHECK(producer.Handshake() == sent);

	// An attempt in flight is closed without a Quit.
	producer.Disconnect();
	CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
	static_cast<void>(producer.Attempt());
	producer.Endpoint.Disconnect();
	CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::Cancelled);
}

void TestStopIsFinal()
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	producer.Endpoint.Stop();
	CHECK(DecodeSent(*producer.Next<SendAction>().Bytes).payload_type() == Payload::Quit);
	CHECK(producer.Next<CloseAction>().Reason == DisconnectReason::Stopped);
	CHECK(producer.Notice<Disconnected>().Reason == DisconnectReason::Stopped);
	producer.Disconnect(DisconnectReason::Stopped);
	CHECK(producer.Status().Stopped && !producer.Status().Connected);
	CHECK(!producer.Endpoint.RequestConnect(producer.Now + 1h, producer.Now + 2h));
	CHECK(producer.Endpoint.Connect(producer.Now, producer.Now + 2s) == ConnectResult::Refused);
	CHECK(producer.Endpoint.CancelConnect());
	producer.Endpoint.OnConnected({});
	CHECK(producer.Single<CloseAction>().Reason == DisconnectReason::Stopped);
	producer.Endpoint.Stop();
	producer.Endpoint.Disconnect();
	CHECK(producer.Commands().empty());

	Producer attempting;
	CHECK(attempting.Endpoint.RequestConnect(attempting.Now, attempting.Now + 2s));
	static_cast<void>(attempting.Attempt());
	attempting.Endpoint.Stop();
	CHECK(attempting.Single<CloseAction>().Reason == DisconnectReason::Stopped);
	CHECK(attempting.Status().Stopped && !attempting.Endpoint.NextWake());
}

void TestControlFramesFollowTheConnection()
{
	Producer producer;
	CHECK(!producer.Endpoint.QueueControl(ClaimFrame()));
	static_cast<void>(producer.Handshake());
	CHECK(producer.Submit("/A") == ProducerResult::Accepted);
	CHECK(producer.Endpoint.QueueControl(ClaimFrame()));
	CHECK(producer.Submit("/B") == ProducerResult::Accepted);
	CHECK(!producer.Endpoint.QueueControl(Bytes{1, 2, 3}));
	CHECK((Kinds(producer.Sends()) ==
		   std::vector<Payload>{Payload::Txn, Payload::ClaimPlayback, Payload::Txn}));
	producer.Disconnect();
	CHECK(TransactionIds(producer.Handshake()) == (std::vector<std::uint64_t>{1, 2}));
}

void TestMessagesWithoutAProducerActionAreIgnored()
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	flatbuffers::FlatBufferBuilder newer(32);
	const Bytes frames[] = {
		server::Ping(),
		server::Claimed("client"),
		server::ClaimRejected("already led", "other"),
		server::Playback(1.0, true, 1.0, "other"),
		server::Event(1),
		server::Resync(),
		server::HelloOk(),
		server::Frame(newer, static_cast<Payload>(200), OpenUSDConnect::CreatePing(newer).Union()),
	};
	for (const Bytes& frame : frames)
	{
		producer.Feed(frame);
	}
	CHECK(producer.Commands().empty() && producer.Notices().empty());
	CHECK(producer.Status().Connected);
}

// A result read with the HelloOk applies to the connection the HelloOk published.
void TestFramesAfterTheHelloOkInOneReadFollowPublication()
{
	Producer producer;
	static_cast<void>(producer.Handshake());
	CHECK(producer.Submit("/A") == ProducerResult::Accepted);
	CHECK(producer.Submit("/B") == ProducerResult::Accepted);
	static_cast<void>(producer.Sends());
	producer.Disconnect();

	static_cast<void>(producer.Request());
	server::Hello hello;
	hello.CommittedThrough = 1;
	producer.Feed(Concatenate({server::HelloOk(hello), server::Acknowledged(2)}));
	CHECK(producer.Status().Connected && producer.Endpoint.OutboxEmpty());
	CHECK(TransactionIds(producer.Sends()) == std::vector<std::uint64_t>{2});
}

void TestStaleHostReportsAreIgnored()
{
	Producer producer;
	producer.Feed(server::HelloOk());
	producer.Endpoint.OnConnected({});
	producer.Disconnect(DisconnectReason::ConnectFailed);
	producer.Advance(1h);
	CHECK(producer.Commands().empty() && producer.Notices().empty());
	CHECK(producer.Endpoint.RequestConnect(producer.Now, producer.Now + 2s));
}

// The host thread appends while the loop thread reads acknowledgements.
void TestConcurrentAppendWhileTheLoopReads()
{
	constexpr std::uint64_t kTransactions = 2'000;
	ProducerConfig config = TestConfig();
	config.MaxPendingTransactions = 64;
	Producer producer(config);
	static_cast<void>(producer.Handshake());
	ProducerEndpoint& endpoint = producer.Endpoint;

	std::thread host(
		[&endpoint]
		{
			for (std::uint64_t id = 1; id <= kTransactions;)
			{
				const ProducerResult result =
					endpoint.Append(id, TransactionFrame(id, "/P"), 1, "");
				CHECK(result == ProducerResult::Accepted || result == ProducerResult::OutboxFull);
				if (result == ProducerResult::Accepted)
				{
					++id;
				}
				else
				{
					std::this_thread::yield();
				}
			}
		});
	std::uint64_t next = 1;
	while (next <= kTransactions)
	{
		for (Action& action : endpoint.TakeActions())
		{
			const SendAction* send = std::get_if<SendAction>(&action);
			CHECK(send != nullptr);
			CHECK(TransactionIds({*send->Bytes}) == std::vector<std::uint64_t>{next});
			const Bytes ack = server::Acknowledged(next++);
			endpoint.OnBytes(ack.data(), ack.size());
		}
		CHECK(endpoint.Status().Connected);
		std::this_thread::yield();
	}
	host.join();
	CHECK(endpoint.OutboxEmpty());
	CHECK(endpoint.Status().AcknowledgedTransactions == kTransactions);
}

} // namespace

int main()
{
	TestConfigurationValidation();
	TestAttemptsAreBoundedAndExclusive();
	TestHelloCarriesTheProducerIdentity();
	TestAcceptedHelloNotifiesThenPublishes();
	TestHandshakeRejectionsHoldUntilAnExplicitConnect();
	TestLayerModeMismatchIsARejection();
	TestHelloHighwaterAheadRequiresRecovery();
	TestHelloHighwaterRegressionRequiresRecovery();
	TestHandshakeProtocolErrorsAreFailedAttempts();
	TestHandshakeDeadline();
	TestRequestBackoffDoublesAndResetsOnPublication();
	TestExplicitConnectNeitherWaitsForNorExtendsTheBackoff();
	TestCancelAndDisconnectResetTheBackoff();
	TestCancelDuringHandshakeKeepsTheSessionHealthy(false);
	TestCancelDuringHandshakeKeepsTheSessionHealthy(true);
	TestCancelBeforeTheSocketOpens();
	TestAReportedEndVoidsActionsQueuedForTheConnection();
	TestAnUntakenAttemptIsWithdrawn();
	TestPublicationReplaysUnsentFramesInOrderWithoutCopies();
	TestReplayOrderSurvivesSeveralLostConnections();
	TestAppendRequiresAPublishedHealthyConnection();
	TestAcknowledgedCheckpointNeedsAnEmptyOutbox();
	TestAcknowledgementHighwaterFailures();
	const DispositionCase dispositions[] = {
		{TransactionRejectionCode::StaleLayerGraph,
		 ProducerRecoveryDisposition::RecoverableConflict,
		 "transaction 1 rejected (stale_layer_graph, expected transaction 1): layer was remapped"},
		{TransactionRejectionCode::InvalidTransaction,
		 ProducerRecoveryDisposition::InvalidOperation,
		 "transaction 1 rejected (invalid_transaction, expected transaction 1): layer was "
		 "remapped"},
		{TransactionRejectionCode::InvalidIdentity, ProducerRecoveryDisposition::SessionFatal,
		 "transaction 1 rejected (invalid_identity, expected transaction 1): layer was remapped"},
		{TransactionRejectionCode::UnexpectedId, ProducerRecoveryDisposition::SessionFatal,
		 "transaction 1 rejected (unexpected_id, expected transaction 1): layer was remapped"},
	};
	for (const DispositionCase& expected : dispositions)
	{
		TestRejectionQuarantinesTheOutbox(expected);
	}
	TestRejectionWithoutReasonOrKnownCode();
	TestRejectionOfAnUnknownTransaction();
	TestRateLimitClosesAndOpensARetryWindow();
	TestRepairReplacesTheRejectedTransaction();
	TestAbandonContinuesAsAFreshSession();
	TestDisconnectSaysQuitAndKeepsTheOutbox();
	TestStopIsFinal();
	TestControlFramesFollowTheConnection();
	TestMessagesWithoutAProducerActionAreIgnored();
	TestFramesAfterTheHelloOkInOneReadFollowPublication();
	TestStaleHostReportsAreIgnored();
	TestConcurrentAppendWhileTheLoopReads();
	return 0;
}
