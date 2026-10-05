#include "openusdconnect/client/engine/receiver_endpoint.h"

#include "endpoint_host.h"
#include "receiver_frames.h"
#include "test_check.h"

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
#include <vector>

using namespace openusdconnect::client;
using namespace std::chrono_literals;
using OpenUSDConnect::HelloRejectionCode;
using OpenUSDConnect::LayerMode;
using OpenUSDConnect::Payload;

namespace
{

using namespace receiver_test;

// Whether the server resumes this Hello rather than sending Resync.
[[nodiscard]] bool ServerResumes(const SentHello& hello, std::string_view instance,
								 std::uint64_t epoch, std::int32_t head)
{
	const bool claim_holds = !hello.ReplayServerInstance || hello.Claims(instance, epoch);
	return hello.SyncFrom <= head + 1 && (hello.SyncFrom == 1 || claim_holds);
}

// Plays both the host and the stage-owning consumer around one endpoint.
class Receiver final : public Host<ReceiverEndpoint>
{
public:
	explicit Receiver(const ReceiverConfig& config = TestConfig())
		: Host(config)
		, Applied(config.SyncFrom - 1)
	{
	}

	// Completes the pending connect attempt and returns the Hello it sent.
	SentHello Connect(std::string_view token = {})
	{
		static_cast<void>(Single<ConnectAction>());
		Endpoint.OnConnected(token);
		return DecodeHello(*Single<SendAction>().Bytes);
	}

	SentHello Start(std::string_view token = {})
	{
		CHECK(Endpoint.Start(Now));
		return Connect(token);
	}

	SentHello Handshake(const server::Hello& hello = {})
	{
		const SentHello sent = Start();
		Feed(server::HelloOk(hello));
		CHECK(Status().Connected);
		return sent;
	}

	// Reports the disconnect, waits out the backoff, and returns the next Hello.
	SentHello Reconnect()
	{
		Disconnect();
		Now = Single<WakeAction>().Time;
		Endpoint.OnTick(Now);
		return Connect();
	}

	// Reports the disconnect of an overflowed connection, applies the queue, and
	// returns the Hello sent at the next wake.
	SentHello ReconnectAfterDrain()
	{
		Disconnect();
		const TimePoint poll = Single<WakeAction>().Time;
		static_cast<void>(Apply());
		Now = poll;
		Endpoint.OnTick(Now);
		return Connect();
	}

	// Drains and applies every queued frame as a successful consumer batch does.
	std::vector<Bytes> Apply()
	{
		const std::uint64_t generation = Endpoint.Generation();
		std::vector<Bytes> frames = Endpoint.DrainFrames();
		bool reset = false;
		for (const Bytes& frame : frames)
		{
			const OpenUSDConnect::Envelope& envelope = Decode(frame);
			if (envelope.payload_type() == Payload::Resync)
			{
				reset = true;
				Applied = 0;
			}
			else if (envelope.payload_type() == Payload::BroadcastEvent)
			{
				Applied = envelope.payload_as_BroadcastEvent()->seq();
			}
		}
		if (reset)
		{
			Endpoint.ResetAppliedProgress();
		}
		if (!frames.empty())
		{
			static_cast<void>(Endpoint.MarkAppliedThrough(generation, Applied));
		}
		static_cast<void>(Endpoint.MarkReplayApplied());
		return frames;
	}

	// The consumer's applied cursor.
	std::int32_t Applied;
};

void TestReconnectPolicy()
{
	const TimePoint now{};
	ReconnectPolicy policy(true, 1s, 4s);
	CHECK(policy.Enabled());
	CHECK(policy.NextAttempt(now) == now + 1s);
	CHECK(policy.NextAttempt(now) == now + 2s);
	CHECK(policy.NextAttempt(now) == now + 4s);
	CHECK(policy.NextAttempt(now) == now + 4s);
	policy.Reset();
	CHECK(policy.NextAttempt(now) == now + 1s);
	CHECK(policy.NextAttempt(now) == now + 2s);
	CHECK(policy.DrainDeadline(now) == now + 4s);
	CHECK(policy.NextAttempt(now) == now + 1s);
	CHECK(!ReconnectPolicy(false, 1s, 1s).Enabled());
	CHECK(ReconnectPolicy::IsValidConfiguration(1s, 1s));
	CHECK(!ReconnectPolicy::IsValidConfiguration(0ms, 1s));
	CHECK(!ReconnectPolicy::IsValidConfiguration(2s, 1s));
}

void TestConfigurationValidation()
{
	const ReceiverConfig valid = TestConfig();
	const ReceiverConfig shared_stage =
		With(With(valid, &ReceiverConfig::LayerMode, LayerMode::SharedStage),
			 &ReceiverConfig::LayeredReplay, false);
	const std::pair<ReceiverConfig, bool> rules[] = {
		{valid, true},
		{shared_stage, true},
		{With(valid, &ReceiverConfig::Host, ""), false},
		{With(valid, &ReceiverConfig::Port, 0), false},
		{With(With(valid, &ReceiverConfig::ClientId, ""), &ReceiverConfig::Origin, ""), true},
		{With(valid, &ReceiverConfig::SyncFrom, 0), false},
		{With(valid, &ReceiverConfig::MaxQueue, 0), false},
		{With(valid, &ReceiverConfig::SocketTimeout, 0ms), false},
		{With(valid, &ReceiverConfig::MaxConsecutiveTimeouts, 0), false},
		{With(valid, &ReceiverConfig::ReconnectBaseDelay, 0ms), false},
		{With(valid, &ReceiverConfig::ReconnectMaxDelay, 999ms), false},
		{With(valid, &ReceiverConfig::LayerMode, static_cast<LayerMode>(2)), false},
		{With(valid, &ReceiverConfig::LayerMode, LayerMode::SharedStage), false},
		{With(shared_stage, &ReceiverConfig::Department, "layout"), false},
	};
	for (const auto& [config, expected] : rules)
	{
		CHECK(ReceiverEndpoint::IsValidConfiguration(config) == expected);
	}
}

void TestStartRequestsOneConnection()
{
	Receiver receiver;
	receiver.Now += 5s;
	CHECK(receiver.Endpoint.Start(receiver.Now));
	const ConnectAction connect = receiver.Single<ConnectAction>();
	CHECK(connect.Host == "127.0.0.1");
	CHECK(connect.Port == 7200);
	CHECK(connect.Deadline == receiver.Now + 30s);
	CHECK(!receiver.Endpoint.Start(receiver.Now));
	CHECK(!receiver.Endpoint.NextWake());
}

void TestFirstHelloCarriesConfigurationWithoutClaim()
{
	ReceiverConfig config = TestConfig();
	config.Department = "layout";
	config.SyncFrom = 5;
	Receiver receiver(config);
	const SentHello hello = receiver.Start("token-1");
	CHECK(hello.Role == "receiver");
	CHECK(hello.ProtocolVersion == kProtocolVersion);
	CHECK(hello.SyncFrom == 5);
	CHECK(hello.ClientId == "client");
	CHECK(hello.Origin == "origin");
	CHECK(hello.Department == "layout");
	CHECK(hello.Token == "token-1");
	CHECK(hello.LayeredReplay);
	CHECK(hello.Mode == LayerMode::Managed);
	CHECK(!hello.ReplayServerInstance && !hello.ReplayEpoch);
}

void TestAnonymousReceiverSendsEmptyIdentity()
{
	ReceiverConfig config = TestConfig();
	config.ClientId.clear();
	config.Origin.clear();
	Receiver receiver(config);
	const SentHello hello = receiver.Start();
	CHECK(hello.ClientId.empty() && hello.Origin.empty());
}

void TestAcceptedHelloNotifiesInOrder()
{
	Receiver receiver;
	static_cast<void>(receiver.Start());
	CHECK(!receiver.Status().Connected);
	StageMetadata metadata;
	metadata.TimeCodesPerSecond = 24.0;
	metadata.UpAxis = "Z";
	server::Hello hello;
	hello.Token = "issued";
	hello.Metadata = metadata;
	receiver.Feed(server::HelloOk(hello));

	const std::vector<Notification> notices = receiver.Notices();
	CHECK(notices.size() == 3);
	CHECK(As<TokenIssued>(notices[0]).Token == "issued");
	const StageMetadata& received = As<StageMetadata>(notices[1]);
	CHECK(received.TimeCodesPerSecond == 24.0);
	CHECK(received.UpAxis == "Z");
	CHECK(!received.FramesPerSecond && !received.StartTimeCode && !received.EndTimeCode &&
		  !received.MetersPerUnit);
	static_cast<void>(As<Connected>(notices[2]));

	const ReceiverStatus status = receiver.Status();
	CHECK(status.Connected && !status.Synchronized && status.LayeredReplayActive);
	CHECK(status.Metadata.TimeCodesPerSecond == 24.0);
	CHECK(status.QueuedFrames == 0);
}

void TestEmptyStageMetadataIsNotNotified()
{
	Receiver receiver;
	server::Hello hello;
	hello.Metadata = StageMetadata{};
	static_cast<void>(receiver.Handshake(hello));
	static_cast<void>(receiver.Notice<Connected>());
}

void TestHandshakeRejectionsStop()
{
	const std::pair<Bytes, HandshakeRejected> rejections[] = {
		{server::AuthRejected("invalid token"),
		 {true, HelloRejectionCode::Unspecified, "invalid token"}},
		{server::HelloRejected(HelloRejectionCode::LayeredReplayRequired, "replay is required"),
		 {false, HelloRejectionCode::LayeredReplayRequired, "replay is required"}},
	};
	const auto same = [](const HandshakeRejected& left, const HandshakeRejected& right)
	{
		return left.Authentication == right.Authentication && left.Code == right.Code &&
			   left.Reason == right.Reason;
	};
	for (const auto& [frame, expected] : rejections)
	{
		Receiver receiver;
		static_cast<void>(receiver.Start());
		receiver.Feed(frame);
		CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::HandshakeRejected);
		CHECK(same(receiver.Notice<HandshakeRejected>(), expected));
		receiver.Disconnect();
		CHECK(receiver.Commands().empty() && !receiver.Endpoint.NextWake());
		const ReceiverStatus status = receiver.Status();
		CHECK(status.Stopped && status.Rejection && same(*status.Rejection, expected));
		CHECK(receiver.Notices().empty());
	}
}

void TestNegotiationRejections()
{
	{
		Receiver receiver;
		static_cast<void>(receiver.Start());
		server::Hello hello;
		hello.Mode = LayerMode::SharedStage;
		hello.Token = "not-issued";
		receiver.Feed(server::HelloOk(hello));
		CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::HandshakeRejected);
		const HandshakeRejected rejected = receiver.Notice<HandshakeRejected>();
		CHECK(!rejected.Authentication);
		CHECK(rejected.Code == HelloRejectionCode::LayerModeMismatch);
		CHECK(rejected.Reason == "server did not negotiate requested layer mode");
		CHECK(receiver.Status().LayerModeActive == LayerMode::SharedStage);
	}
	{
		Receiver receiver;
		static_cast<void>(receiver.Start());
		server::Hello hello;
		hello.LayeredReplay = false;
		receiver.Feed(server::HelloOk(hello));
		CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::HandshakeRejected);
		const ReceiverStatus status = receiver.Status();
		CHECK(status.Rejection->Code == HelloRejectionCode::LayeredReplayRequired);
		CHECK(status.Rejection->Reason == "server did not negotiate requested layered replay");
		CHECK(!status.LayeredReplayActive);
	}
	{
		ReceiverConfig config = TestConfig();
		config.LayeredReplay = false;
		Receiver receiver(config);
		CHECK(!receiver.Start().LayeredReplay);
		server::Hello hello;
		hello.LayeredReplay = false;
		receiver.Feed(server::HelloOk(hello));
		CHECK(receiver.Status().Connected);
		CHECK(!receiver.Status().LayeredReplayActive);
	}
	{
		ReceiverConfig config = TestConfig();
		config.LayerMode = LayerMode::SharedStage;
		config.LayeredReplay = false;
		Receiver receiver(config);
		CHECK(receiver.Start().Mode == LayerMode::SharedStage);
		server::Hello hello;
		hello.LayeredReplay = false;
		hello.Mode = LayerMode::SharedStage;
		receiver.Feed(server::HelloOk(hello));
		CHECK(receiver.Status().Connected);
		CHECK(receiver.Status().LayerModeActive == LayerMode::SharedStage);
		receiver.Feed(server::LayerGraph(1));
		CHECK(receiver.Status().LastSequence == 1);
		CHECK(Kinds(receiver.Endpoint.DrainFrames()) ==
			  std::vector<Payload>{Payload::LayerGraphState});
	}
}

void TestControlMessages()
{
	Receiver receiver;
	static_cast<void>(receiver.Handshake());
	static_cast<void>(receiver.Notices());
	receiver.Feed(server::Ping());
	receiver.Feed(server::Playback(12.5, true, 2.0, "leader"));
	receiver.Feed(server::Claimed("me"));
	receiver.Feed(server::ClaimRejected("already led", "other"));
	receiver.Feed(server::LayerStack());
	CHECK(receiver.Commands().empty());

	const std::vector<Notification> notices = receiver.Notices();
	CHECK(notices.size() == 3);
	const PlaybackState& state = As<PlaybackState>(notices[0]);
	CHECK(state.Time == 12.5 && state.Playing && state.Rate == 2.0);
	CHECK(state.LeaderClientId == "leader");
	CHECK(As<PlaybackClaimed>(notices[1]).LeaderClientId == "me");
	const PlaybackRejected& rejected = As<PlaybackRejected>(notices[2]);
	CHECK(rejected.Reason == "already led" && rejected.CurrentLeaderClientId == "other");
	CHECK(Kinds(receiver.Endpoint.DrainFrames()) == std::vector<Payload>{Payload::LayerStackState});
}

void TestReplayCompleteWaitsForDrainedFrames()
{
	Receiver receiver;
	static_cast<void>(receiver.Handshake());
	for (std::int32_t sequence = 1; sequence <= 3; ++sequence)
	{
		receiver.Feed(server::Event(sequence));
	}
	receiver.Feed(server::ReplayComplete(3, 7));
	CHECK(!receiver.Status().Synchronized);
	const std::uint64_t generation = receiver.Endpoint.Generation();
	CHECK((Sequences(receiver.Endpoint.DrainFrames(2)) == std::vector<std::int32_t>{1, 2}));
	CHECK(receiver.Status().QueuedFrames == 1);
	CHECK(!receiver.Endpoint.MarkReplayApplied());
	CHECK(Sequences(receiver.Endpoint.DrainFrames(2)) == std::vector<std::int32_t>{3});
	CHECK(receiver.Endpoint.MarkAppliedThrough(generation, 3));
	CHECK(receiver.Endpoint.MarkReplayApplied());
	const ReceiverStatus status = receiver.Status();
	CHECK(status.Synchronized);
	CHECK(status.ReplayHeadSequence == 3);
	CHECK(status.ReplayEpoch == 7);
	CHECK(status.ServerInstance == "server");

	CHECK(!receiver.Logged(LogLevel::Warning));
	receiver.Feed(server::ReplayComplete(-1, 8));
	CHECK(receiver.Logged(LogLevel::Warning));
	CHECK(receiver.Status().Synchronized);
}

void TestInPlaceResyncClearsReadyUntilApplied()
{
	Receiver receiver;
	static_cast<void>(receiver.Handshake());
	receiver.Feed(server::ReplayComplete(0, 1));
	CHECK(receiver.Endpoint.MarkReplayApplied());
	CHECK(receiver.Status().Synchronized);

	receiver.Feed(server::Resync());
	receiver.Feed(server::Event(1));
	receiver.Feed(server::ReplayComplete(1, 2));
	CHECK(!receiver.Status().Synchronized);
	CHECK((Kinds(receiver.Apply()) ==
		   std::vector<Payload>{Payload::Resync, Payload::BroadcastEvent}));
	const ReceiverStatus status = receiver.Status();
	CHECK(status.Synchronized);
	CHECK(status.ReplayHeadSequence == 1);
	CHECK(status.ReplayEpoch == 2);
}

void TestDataFrameAcceptResults()
{
	Receiver receiver;
	static_cast<void>(receiver.Handshake());
	static_cast<void>(receiver.Notices());
	receiver.Feed(server::Event(1));
	receiver.Feed(server::Event(2));
	static_cast<void>(receiver.Apply());
	receiver.Feed(server::Event(3));
	receiver.Feed(server::Event(2));
	CHECK(receiver.Status().QueuedFrames == 1);
	CHECK(!receiver.Logged(LogLevel::Warning));
	receiver.Feed(server::Event(0));
	CHECK(receiver.Logged(LogLevel::Warning));
	CHECK(receiver.Status().QueuedFrames == 1);
	CHECK(receiver.Commands().empty());

	receiver.Feed(server::Event(5));
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::SequenceGap);
	CHECK(receiver.Notice<Disconnected>().Reason == DisconnectReason::SequenceGap);
	const ReceiverStatus status = receiver.Status();
	CHECK(status.QueuedFrames == 0);
	CHECK(status.LastSequence == 2);

	// Bytes for a connection the endpoint closed are ignored.
	receiver.Feed(server::Event(3));
	CHECK(receiver.Status().QueuedFrames == 0);
	CHECK(receiver.Reconnect().SyncFrom == 3);

	// The applied cursor restarts with an applied reset.
	receiver.Feed(server::HelloOk());
	receiver.Feed(server::Resync());
	receiver.Feed(server::Event(1));
	static_cast<void>(receiver.Apply());
	receiver.Feed(server::Event(3));
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::SequenceGap);
	CHECK(receiver.Reconnect().SyncFrom == 2);
}

enum class DrainOutcome
{
	DrainedEarlier,
	DrainedDuringWait,
	TimedOut,
};

// An overflow closes the connection and reconnects from the received cursor
// once the consumer drained the queue, or anyway at the drain deadline.
void TestOverflowWaitsForTheDrain(DrainOutcome outcome)
{
	ReceiverConfig config = TestConfig();
	config.MaxQueue = 2;
	config.ReconnectMaxDelay = 2s;
	Receiver receiver(config);
	static_cast<void>(receiver.Handshake());
	static_cast<void>(receiver.Notices());
	receiver.Feed(server::Event(1));
	receiver.Feed(server::Event(2));
	receiver.Feed(server::Event(3));
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::QueueFull);
	CHECK(receiver.Notice<Disconnected>().Reason == DisconnectReason::QueueFull);
	if (outcome == DrainOutcome::DrainedEarlier)
	{
		static_cast<void>(receiver.Apply());
	}
	receiver.Disconnect();
	if (outcome != DrainOutcome::DrainedEarlier)
	{
		// The endpoint polls for the drain without reconnecting.
		const std::optional<TimePoint> poll = receiver.Endpoint.NextWake();
		CHECK(poll && *poll > receiver.Now && *poll < receiver.Now + 2s);
		receiver.Advance(1s);
		const std::vector<Action> commands = receiver.Commands();
		CHECK(std::all_of(commands.begin(), commands.end(),
						  [](const Action& action)
						  {
							  return std::holds_alternative<WakeAction>(action);
						  }));
	}
	if (outcome == DrainOutcome::DrainedDuringWait)
	{
		static_cast<void>(receiver.Apply());
		receiver.Now = *receiver.Endpoint.NextWake();
		receiver.Endpoint.OnTick(receiver.Now);
	}
	else if (outcome == DrainOutcome::TimedOut)
	{
		receiver.Advance(1s);
	}
	CHECK(receiver.Connect().SyncFrom == 3);
	CHECK(receiver.Status().QueuedFrames == (outcome == DrainOutcome::TimedOut ? 2U : 0U));
}

void TestConsecutiveReadTimeouts()
{
	ReceiverConfig config = TestConfig();
	config.MaxConsecutiveTimeouts = 3;
	Receiver receiver(config);
	static_cast<void>(receiver.Start());
	const auto time_out = [&](int count)
	{
		for (int timeout = 0; timeout < count; ++timeout)
		{
			receiver.Endpoint.OnReadTimeout();
		}
	};
	// The handshake counts too, and every received byte restarts the count.
	time_out(2);
	receiver.Feed(server::HelloOk());
	time_out(2);
	receiver.Feed(server::Ping());
	time_out(2);
	CHECK(receiver.Commands().empty());
	CHECK(receiver.Status().Connected);
	time_out(1);
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::ReadTimeout);
	receiver.Endpoint.OnReadTimeout();
	CHECK(receiver.Commands().empty());
}

void TestBackoffDoublesAndResetsAfterConnectedSession()
{
	ReceiverConfig config = TestConfig();
	config.ReconnectMaxDelay = 8s;
	Receiver receiver(config);
	CHECK(receiver.Endpoint.Start(receiver.Now));
	static_cast<void>(receiver.Single<ConnectAction>());
	std::vector<std::chrono::milliseconds> waits;
	const auto next_attempt = [&](DisconnectReason reason)
	{
		receiver.Disconnect(reason);
		const TimePoint due = receiver.Single<WakeAction>().Time;
		CHECK(receiver.Endpoint.NextWake() == due);
		waits.push_back(std::chrono::duration_cast<std::chrono::milliseconds>(due - receiver.Now));
		receiver.Now = due - 1ms;
		receiver.Endpoint.OnTick(receiver.Now);
		CHECK(receiver.Commands().empty());
		receiver.Advance(1ms);
		static_cast<void>(receiver.Single<ConnectAction>());
	};
	next_attempt(DisconnectReason::ConnectFailed);
	next_attempt(DisconnectReason::ConnectFailed);
	receiver.Endpoint.OnConnected({});
	static_cast<void>(receiver.Single<SendAction>());
	receiver.Feed(server::HelloOk());
	next_attempt(DisconnectReason::PeerClosed);
	for (int attempt = 0; attempt < 4; ++attempt)
	{
		next_attempt(DisconnectReason::ConnectFailed);
	}
	CHECK((waits == std::vector<std::chrono::milliseconds>{1s, 2s, 1s, 2s, 4s, 8s, 8s}));
}

void TestReconnectDisabledStops()
{
	ReceiverConfig config = TestConfig();
	config.Reconnect = false;
	{
		Receiver receiver(config);
		CHECK(receiver.Endpoint.Start(receiver.Now));
		static_cast<void>(receiver.Single<ConnectAction>());
		receiver.Disconnect(DisconnectReason::ConnectFailed);
		CHECK(receiver.Commands().empty());
		CHECK(receiver.Status().Stopped);
	}
	{
		Receiver receiver(config);
		static_cast<void>(receiver.Handshake());
		receiver.Disconnect();
		CHECK(receiver.Commands().empty());
		CHECK(receiver.Status().Stopped);
		CHECK(!receiver.Endpoint.NextWake());
	}
	{
		config.MaxQueue = 1;
		Receiver receiver(config);
		static_cast<void>(receiver.Handshake());
		receiver.Feed(server::Event(1));
		receiver.Feed(server::Event(2));
		CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::QueueFull);
		receiver.Disconnect();
		CHECK(receiver.Commands().empty());
		CHECK(receiver.Status().Stopped);
	}
}

// A toggle applies to the session that is open when it ends.
void TestReconnectToggleAppliesWhenTheSessionEnds()
{
	ReceiverConfig config = TestConfig();
	config.Reconnect = false;
	Receiver receiver(config);
	static_cast<void>(receiver.Handshake());
	receiver.Endpoint.SetReconnect(true);
	CHECK(receiver.Endpoint.RequestReplayFrom(1));
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::ReplayRequested);
	CHECK(receiver.Reconnect().SyncFrom == 1);
	receiver.Feed(server::HelloOk());
	receiver.Endpoint.SetReconnect(false);
	receiver.Disconnect();
	CHECK(receiver.Commands().empty());
	CHECK(receiver.Status().Stopped);
	receiver.Endpoint.SetReconnect(true);
	receiver.Advance(60s);
	CHECK(receiver.Commands().empty());
}

void TestReplayRequests()
{
	Receiver receiver;
	CHECK(!receiver.Endpoint.RequestReplayFrom(0));
	static_cast<void>(receiver.Handshake());
	receiver.Feed(server::Event(1));
	receiver.Feed(server::Event(2));
	const std::uint64_t marker = receiver.Endpoint.FreezeMarker();
	CHECK(!receiver.Endpoint.DrainedThrough(marker));
	CHECK(receiver.Endpoint.RequestReplayFrom(2));
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::ReplayRequested);
	CHECK(receiver.Endpoint.DrainedThrough(marker));
	const ReceiverStatus status = receiver.Status();
	CHECK(status.LastSequence == 1 && status.QueuedFrames == 0);
	CHECK(receiver.Reconnect().SyncFrom == 2);
	receiver.Feed(server::HelloOk());
	receiver.Feed(server::Event(2));
	CHECK(receiver.Status().LastSequence == 2);

	// Without an open connection the next Hello carries the request.
	receiver.Disconnect();
	static_cast<void>(receiver.Single<WakeAction>());
	CHECK(receiver.Endpoint.RequestReplayFrom(2));
	CHECK(receiver.Commands().empty());
	receiver.Advance(1s);
	CHECK(receiver.Connect().SyncFrom == 2);

	// A request during the handshake abandons it before the Hello is accepted.
	static_cast<void>(receiver.Notices());
	CHECK(receiver.Endpoint.RequestReplayFrom(1));
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::ReplayRequested);
	receiver.Feed(server::HelloOk());
	CHECK(!receiver.Status().Connected);
	CHECK(receiver.Notices().empty());
	CHECK(receiver.Reconnect().SyncFrom == 1);
}

void TestReconnectCursorFollowsReceivedFrames()
{
	ReceiverConfig config = TestConfig();
	config.SyncFrom = 10;
	Receiver receiver(config);
	static_cast<void>(receiver.Handshake());
	receiver.Feed(server::Event(10));
	CHECK(receiver.Reconnect().SyncFrom == 11);
	receiver.Feed(server::HelloOk());
	receiver.Feed(server::Resync());
	receiver.Feed(server::Event(1));
	CHECK(receiver.Reconnect().SyncFrom == 2);
}

void TestProtocolErrorsCloseTheConnection()
{
	flatbuffers::FlatBufferBuilder future(32);
	const Bytes future_schema = server::Frame(
		future, Payload::Ping, OpenUSDConnect::CreatePing(future).Union(), kSchemaVersion + 1);
	flatbuffers::FlatBufferBuilder none(32);
	const Bytes no_payload = server::Frame(none, Payload::NONE, 0);
	flatbuffers::FlatBufferBuilder unknown(32);
	const Bytes unknown_payload = server::Frame(unknown, static_cast<Payload>(200),
												OpenUSDConnect::CreatePing(unknown).Union());
	Bytes garbage;
	const std::uint8_t noise[] = {1, 2, 3, 4, 5, 6, 7, 8};
	CHECK(EncodeFrame(noise, sizeof(noise), garbage) == FrameResult::Success);
	const Bytes empty_header{0, 0, 0, 0};
	for (const bool handshaking : {true, false})
	{
		for (const Bytes& error :
			 {future_schema, no_payload, unknown_payload, garbage, empty_header})
		{
			Receiver receiver;
			static_cast<void>(receiver.Start());
			if (!handshaking)
			{
				receiver.Feed(server::HelloOk());
			}
			receiver.Feed(error);
			CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::ProtocolError);
			CHECK(receiver.Reconnect().SyncFrom == 1);
		}
	}
	// The server answers a Hello before it sends anything else.
	for (const Bytes& early : {server::Ping(), server::Event(1)})
	{
		Receiver receiver;
		static_cast<void>(receiver.Start());
		receiver.Feed(early);
		CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::ProtocolError);
		CHECK(!receiver.Status().Connected && receiver.Status().QueuedFrames == 0);
	}
}

void TestHostDisconnectEndsTheSession()
{
	Receiver receiver;
	static_cast<void>(receiver.Start());
	receiver.Disconnect(DisconnectReason::TransportError);
	CHECK(receiver.Notices().empty());
	static_cast<void>(receiver.Single<WakeAction>());
	receiver.Advance(1s);
	static_cast<void>(receiver.Connect());
	receiver.Feed(server::HelloOk());
	static_cast<void>(receiver.Notices());
	receiver.Feed(server::ReplayComplete(0, 0));
	CHECK(receiver.Endpoint.MarkReplayApplied());
	receiver.Disconnect(DisconnectReason::TransportError);
	CHECK(receiver.Notice<Disconnected>().Reason == DisconnectReason::TransportError);
	CHECK(!receiver.Status().Connected);
	CHECK(!receiver.Status().Synchronized);
	static_cast<void>(receiver.Single<WakeAction>());
	// A duplicate report for the same connection changes nothing.
	receiver.Disconnect();
	CHECK(receiver.Commands().empty());
}

void TestStop()
{
	{
		Receiver receiver;
		static_cast<void>(receiver.Handshake());
		static_cast<void>(receiver.Notices());
		receiver.Endpoint.Stop();
		CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::Stopped);
		CHECK(receiver.Notice<Disconnected>().Reason == DisconnectReason::Stopped);
		CHECK(receiver.Status().Stopped && !receiver.Status().Connected);
		receiver.Disconnect();
		receiver.Advance(60s);
		CHECK(receiver.Commands().empty());
		CHECK(!receiver.Endpoint.Start(receiver.Now));
		receiver.Endpoint.Stop();
		CHECK(receiver.Commands().empty());
	}
	{
		Receiver receiver;
		CHECK(receiver.Endpoint.Start(receiver.Now));
		static_cast<void>(receiver.Single<ConnectAction>());
		receiver.Endpoint.Stop();
		CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::Stopped);
		// A connect that completes after Stop is closed again, without a Hello.
		receiver.Endpoint.OnConnected({});
		CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::Stopped);
	}
	{
		Receiver receiver;
		static_cast<void>(receiver.Handshake());
		receiver.Disconnect();
		static_cast<void>(receiver.Single<WakeAction>());
		receiver.Endpoint.Stop();
		CHECK(receiver.Commands().empty());
		CHECK(!receiver.Endpoint.NextWake());
		receiver.Advance(60s);
		CHECK(receiver.Commands().empty());
	}
}

// Replay identity. Each case follows a receiver scenario from the Python
// receiver's unit and integration tests.

void TestReceivedIdentityIsPublishedOnlyWhenApplied()
{
	Receiver receiver;
	server::Hello old_server;
	old_server.ServerInstance = "old";
	old_server.ReplayEpoch.reset();
	static_cast<void>(receiver.Handshake(old_server));
	receiver.Feed(server::ReplayComplete(0, 2));
	CHECK(receiver.Status().ServerInstance.empty());
	CHECK(receiver.Endpoint.MarkReplayApplied());
	CHECK(receiver.Status().ServerInstance == "old");

	CHECK(receiver.Reconnect().Claims("old", 2));
	server::Hello new_server;
	new_server.ServerInstance = "new";
	new_server.ReplayEpoch.reset();
	receiver.Feed(server::HelloOk(new_server));
	CHECK(receiver.Status().ServerInstance == "old");
	CHECK(!receiver.Status().Synchronized);
	receiver.Feed(server::Resync());
	receiver.Feed(server::ReplayComplete(0, 0));
	CHECK(!receiver.Endpoint.MarkReplayApplied());
	CHECK(receiver.Endpoint.DrainFrames().size() == 1);
	CHECK(receiver.Endpoint.MarkReplayApplied());
	CHECK(receiver.Status().ServerInstance == "new");
	CHECK(receiver.Status().ReplayEpoch == 0);
}

void TestInterruptedLiveResetClaimsUnknownPrefix()
{
	Receiver receiver;
	server::Hello hello;
	hello.ReplayEpoch.reset();
	static_cast<void>(receiver.Handshake(hello));
	receiver.Feed(server::ReplayComplete(0, 3));
	CHECK(receiver.Endpoint.MarkReplayApplied());
	receiver.Feed(server::Resync());
	CHECK(receiver.Reconnect().ClaimsUnknownPrefix());
	CHECK(!receiver.Endpoint.MarkReplayApplied());
}

void TestFullReplayRequestQueuesItsOwnReset()
{
	Receiver receiver;
	static_cast<void>(receiver.Handshake());
	receiver.Feed(server::LayerStack());
	receiver.Feed(server::Event(1));
	static_cast<void>(receiver.Apply());
	CHECK(receiver.Reconnect().SyncFrom == 2);
	CHECK(receiver.Endpoint.RequestReplayFrom(1));
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::ReplayRequested);
	CHECK(receiver.Reconnect().SyncFrom == 1);
	receiver.Feed(server::HelloOk());
	receiver.Feed(server::LayerStack());
	receiver.Feed(server::Event(1));
	receiver.Feed(server::ReplayComplete(1, 0));
	CHECK(
		(Kinds(receiver.Apply()) ==
		 std::vector<Payload>{Payload::Resync, Payload::LayerStackState, Payload::BroadcastEvent}));
	CHECK(receiver.Status().Synchronized);

	// The reset belongs to that one replay.
	CHECK(receiver.Reconnect().SyncFrom == 2);
	receiver.Feed(server::HelloOk());
	CHECK(receiver.Status().QueuedFrames == 0);
}

void TestChangedHelloIdentityWaitsForTheReset(std::string_view instance, bool queue_full)
{
	ReceiverConfig config = TestConfig();
	config.MaxQueue = 1;
	Receiver receiver(config);
	static_cast<void>(receiver.Handshake());
	receiver.Feed(server::Event(1));
	CHECK(receiver.Endpoint.DrainFrames().size() == 1);
	const SentHello hello = receiver.Reconnect();
	CHECK(hello.SyncFrom == 2);
	CHECK(hello.Claims("server", 0));

	server::Hello changed;
	changed.ServerInstance = std::string(instance);
	changed.ReplayEpoch = 1;
	receiver.Feed(server::HelloOk(changed));
	const ReceiverStatus status = receiver.Status();
	CHECK(status.LastSequence == 1);
	CHECK(status.ServerInstance.empty());
	CHECK(!status.Synchronized);
	if (queue_full)
	{
		receiver.Feed(server::LayerStack());
	}
	receiver.Feed(server::Resync());
	CHECK(receiver.Status().LastSequence == (queue_full ? 1 : 0));
	if (queue_full)
	{
		CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::QueueFull);
		const SentHello retry = receiver.ReconnectAfterDrain();
		CHECK(retry.SyncFrom == 2);
		CHECK(retry.Claims("server", 0));
	}
	else
	{
		const SentHello retry = receiver.Reconnect();
		CHECK(retry.SyncFrom == 1);
		CHECK(retry.Claims(instance, 1));
	}
}

void TestOldServerRetainsCursorWithoutIdentity(std::string_view instance)
{
	ReceiverConfig config = TestConfig();
	config.SyncFrom = 4;
	config.LayeredReplay = false;
	Receiver receiver(config);
	static_cast<void>(receiver.Start());
	server::Hello old_server;
	old_server.ServerInstance = std::string(instance);
	old_server.ReplayIdentity = false;
	old_server.ReplayEpoch.reset();
	old_server.LayeredReplay = false;
	receiver.Feed(server::HelloOk(old_server));
	receiver.Feed(server::Event(4));
	receiver.Feed(server::ReplayComplete(4, 0));
	static_cast<void>(receiver.Apply());
	CHECK(receiver.Status().Synchronized);
	CHECK(receiver.Status().ServerInstance.empty());
	const SentHello retry = receiver.Reconnect();
	CHECK(retry.SyncFrom == 5);
	CHECK(retry.ClaimsUnknownPrefix());
}

void TestInitialSnapshotCursorIsNotProof()
{
	ReceiverConfig config = TestConfig();
	config.SyncFrom = 2;
	Receiver receiver(config);
	static_cast<void>(receiver.Handshake());
	receiver.Feed(server::Event(2));
	receiver.Feed(server::ReplayComplete(2, 0));
	static_cast<void>(receiver.Apply());
	CHECK(receiver.Status().Synchronized);
	CHECK(receiver.Status().ServerInstance.empty());

	const SentHello retry = receiver.Reconnect();
	CHECK(retry.SyncFrom == 3);
	CHECK(retry.ClaimsUnknownPrefix());
	receiver.Feed(server::HelloOk());
	receiver.Feed(server::Resync());
	receiver.Feed(server::Event(1));
	receiver.Feed(server::Event(2));
	receiver.Feed(server::ReplayComplete(2, 0));
	static_cast<void>(receiver.Apply());
	CHECK(receiver.Status().ServerInstance == "server");
}

// Feeds events from first until the queue overflows or last is queued.
std::int32_t FeedEvents(Receiver& receiver, std::int32_t first, std::int32_t last)
{
	for (std::int32_t sequence = first; sequence <= last; ++sequence)
	{
		receiver.Feed(server::Event(sequence));
		if (!receiver.Status().Connected)
		{
			CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::QueueFull);
			return sequence;
		}
	}
	return last + 1;
}

enum class OverflowStart
{
	FirstReplay,
	AfterServerReset,
	AfterLiveReset,
	SnapshotCursor,
	FullReplayRequest,
};

// A replay that overflows the queue resumes where the queue stopped, claims the
// replay's identity, and advances on every reconnect until it completes.
void TestQueueOverflowResumesTheReplay(OverflowStart start)
{
	const bool snapshot =
		start == OverflowStart::SnapshotCursor || start == OverflowStart::FullReplayRequest;
	const bool layered = start == OverflowStart::FullReplayRequest;
	ReceiverConfig config = TestConfig();
	config.SyncFrom = snapshot ? 4 : 1;
	config.LayeredReplay = layered || !snapshot;
	config.MaxQueue = snapshot ? (layered ? 2 : 1) : 3;
	const std::int32_t last = snapshot ? 6 : 8;
	Receiver receiver(config);
	server::Hello hello;
	hello.LayeredReplay = config.LayeredReplay;
	const auto accept = [&](bool reset)
	{
		receiver.Feed(server::HelloOk(hello));
		if (reset)
		{
			receiver.Feed(server::Resync());
		}
		if (layered)
		{
			receiver.Feed(server::LayerStack());
		}
	};
	static_cast<void>(receiver.Start());
	accept(false);
	if (start == OverflowStart::AfterServerReset || start == OverflowStart::AfterLiveReset)
	{
		receiver.Feed(server::Event(1));
		receiver.Feed(server::ReplayComplete(1, 0));
		static_cast<void>(receiver.Apply());
		hello.ReplayEpoch = 1;
	}
	if (start == OverflowStart::AfterServerReset)
	{
		static_cast<void>(receiver.Reconnect());
		accept(true);
	}
	else if (start == OverflowStart::AfterLiveReset)
	{
		// A live Resync names no epoch, so overflowing it needs one fresh reset.
		receiver.Feed(server::Resync());
		CHECK(FeedEvents(receiver, 1, last) == 3);
		CHECK(receiver.ReconnectAfterDrain().ClaimsUnknownPrefix());
		accept(true);
	}
	else if (start == OverflowStart::SnapshotCursor)
	{
		// The snapshot prefix is never proven, so its first overflow needs a reset.
		CHECK(FeedEvents(receiver, 4, last) == 5);
		CHECK(receiver.ReconnectAfterDrain().ClaimsUnknownPrefix());
		accept(true);
	}
	else if (start == OverflowStart::FullReplayRequest)
	{
		receiver.Feed(server::ReplayComplete(3, 0));
		static_cast<void>(receiver.Apply());
		CHECK(receiver.Endpoint.RequestReplayFrom(1));
		static_cast<void>(receiver.Single<CloseAction>());
		static_cast<void>(receiver.Reconnect());
		accept(false);
	}

	std::vector<std::int32_t> progress;
	std::int32_t next = 1;
	while ((next = FeedEvents(receiver, next, last)) <= last)
	{
		const SentHello resumed = receiver.ReconnectAfterDrain();
		progress.push_back(receiver.Applied);
		CHECK(resumed.SyncFrom == next);
		CHECK(resumed.Claims("server", *hello.ReplayEpoch));
		accept(false);
	}
	receiver.Feed(server::ReplayComplete(last, *hello.ReplayEpoch));
	const std::vector<Payload> kinds = Kinds(receiver.Apply());
	CHECK(std::find(kinds.begin(), kinds.end(), Payload::Resync) == kinds.end());
	CHECK(progress.size() > 1);
	CHECK(std::adjacent_find(progress.begin(), progress.end(), std::greater_equal<>()) ==
		  progress.end());
	CHECK(receiver.Applied == last);
	const ReceiverStatus status = receiver.Status();
	CHECK(status.Synchronized && status.ServerInstance == "server");
	CHECK(status.ReplayEpoch == *hello.ReplayEpoch);
}

// After a live Resync the applied cursor may still count the old sequence
// domain. A replay from it must claim the applied identity, so the server
// resets rather than resuming the new domain from an old-domain cursor.
void TestOldDomainCursorReplayClaimsTheAppliedIdentity()
{
	Receiver receiver;
	static_cast<void>(receiver.Handshake());
	for (std::int32_t sequence = 1; sequence <= 3; ++sequence)
	{
		receiver.Feed(server::Event(sequence));
	}
	receiver.Feed(server::ReplayComplete(3, 0));
	static_cast<void>(receiver.Apply());
	receiver.Feed(server::Event(4));
	receiver.Feed(server::Event(5));
	receiver.Feed(server::Resync());
	for (std::int32_t sequence = 1; sequence <= 5; ++sequence)
	{
		receiver.Feed(server::Event(sequence));
	}
	receiver.Feed(server::ReplayComplete(5, 1));

	const std::uint64_t generation = receiver.Endpoint.Generation();
	CHECK(Sequences(receiver.Endpoint.DrainFrames(1)) == std::vector<std::int32_t>{4});
	CHECK(receiver.Endpoint.MarkAppliedThrough(generation, 4));
	receiver.Feed(server::Event(7));
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::SequenceGap);
	const SentHello hello = receiver.Reconnect();
	CHECK(hello.SyncFrom == 5);
	CHECK(hello.Claims("server", 0));
}

// The consumer applied the replay, but a reconnect during its batch rejected
// MarkAppliedThrough and the next connection delivers no newer frame.
void TestReplayAppliesAcrossAMidBatchReconnect()
{
	Receiver receiver;
	static_cast<void>(receiver.Handshake());
	receiver.Feed(server::Event(1));
	receiver.Feed(server::Event(2));
	receiver.Feed(server::ReplayComplete(2, 0));
	const std::uint64_t generation = receiver.Endpoint.Generation();
	CHECK(receiver.Endpoint.DrainFrames().size() == 2);

	const SentHello hello = receiver.Reconnect();
	CHECK(hello.SyncFrom == 3 && hello.Claims("server", 0));
	receiver.Feed(server::HelloOk());
	receiver.Feed(server::ReplayComplete(2, 0));
	CHECK(!receiver.Endpoint.MarkAppliedThrough(generation, 2));
	CHECK(receiver.Endpoint.MarkReplayApplied());
	const ReceiverStatus status = receiver.Status();
	CHECK(status.Synchronized);
	CHECK(status.LastAppliedSequence == 2);
}

enum class ReplayCause
{
	ConsumerFailure,
	SequenceGap,
	ReplayCompleteGap,
};

// A replay request during the connection's first replay keeps the handshake
// identity it proved, so the server resumes from the applied cursor. A marker
// beyond the received records is such a gap, or marking it would skip them.
void TestReplayRequestWithoutResetResumes(ReplayCause cause)
{
	Receiver receiver;
	static_cast<void>(receiver.Handshake());
	receiver.Feed(server::Event(1));
	receiver.Feed(server::Event(2));
	const std::uint64_t generation = receiver.Endpoint.Generation();
	CHECK(Sequences(receiver.Endpoint.DrainFrames(1)) == std::vector<std::int32_t>{1});
	CHECK(receiver.Endpoint.MarkAppliedThrough(generation, 1));
	if (cause == ReplayCause::ConsumerFailure)
	{
		CHECK(Sequences(receiver.Endpoint.DrainFrames(1)) == std::vector<std::int32_t>{2});
		CHECK(receiver.Endpoint.RequestReplayFrom(2));
		CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::ReplayRequested);
	}
	else
	{
		receiver.Feed(cause == ReplayCause::SequenceGap ? server::Event(4)
														: server::ReplayComplete(3, 0));
		CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::SequenceGap);
	}
	if (cause == ReplayCause::ReplayCompleteGap)
	{
		CHECK(!receiver.Endpoint.MarkReplayApplied());
	}
	const SentHello hello = receiver.Reconnect();
	CHECK(hello.SyncFrom == 2);
	CHECK(hello.Claims("server", 0));
	CHECK(ServerResumes(hello, "server", 0, 3));
}

enum class ResetState
{
	Queued,
	Drained,
	Applied,
};

// A live reset that is still queued or unapplied separates the stage from the
// received identity, so the claim falls back to the applied replay.
void TestReplayRequestWithPendingResetClaimsTheAppliedReplay(ResetState state)
{
	Receiver receiver;
	static_cast<void>(receiver.Handshake());
	receiver.Feed(server::Event(1));
	receiver.Feed(server::ReplayComplete(1, 0));
	static_cast<void>(receiver.Apply());
	receiver.Feed(server::Resync());
	receiver.Feed(server::Event(1));
	receiver.Feed(server::ReplayComplete(1, 1));
	if (state != ResetState::Queued)
	{
		CHECK((Kinds(receiver.Endpoint.DrainFrames(1)) == std::vector<Payload>{Payload::Resync}));
	}
	if (state == ResetState::Applied)
	{
		receiver.Endpoint.ResetAppliedProgress();
	}
	CHECK(receiver.Endpoint.RequestReplayFrom(1));
	static_cast<void>(receiver.Single<CloseAction>());
	CHECK(receiver.Reconnect().Claims("server", state == ResetState::Applied ? 1 : 0));
}

// The reset the receiver queues ahead of a replay from one is pending too.
void TestOwnQueuedResetIsPending()
{
	Receiver receiver;
	static_cast<void>(receiver.Handshake());
	receiver.Feed(server::Event(1));
	static_cast<void>(receiver.Apply());
	CHECK(receiver.Endpoint.RequestReplayFrom(1));
	static_cast<void>(receiver.Single<CloseAction>());
	CHECK(receiver.Reconnect().SyncFrom == 1);
	receiver.Feed(server::HelloOk());
	CHECK(receiver.Status().QueuedFrames == 1);
	CHECK(receiver.Endpoint.RequestReplayFrom(1));
	static_cast<void>(receiver.Single<CloseAction>());
	CHECK(receiver.Reconnect().ClaimsUnknownPrefix());
}

} // namespace

int main()
{
	TestReconnectPolicy();
	TestConfigurationValidation();
	TestStartRequestsOneConnection();
	TestFirstHelloCarriesConfigurationWithoutClaim();
	TestAnonymousReceiverSendsEmptyIdentity();
	TestAcceptedHelloNotifiesInOrder();
	TestEmptyStageMetadataIsNotNotified();
	TestHandshakeRejectionsStop();
	TestNegotiationRejections();
	TestControlMessages();
	TestReplayCompleteWaitsForDrainedFrames();
	TestInPlaceResyncClearsReadyUntilApplied();
	TestDataFrameAcceptResults();
	for (const DrainOutcome outcome :
		 {DrainOutcome::DrainedEarlier, DrainOutcome::DrainedDuringWait, DrainOutcome::TimedOut})
	{
		TestOverflowWaitsForTheDrain(outcome);
	}
	TestConsecutiveReadTimeouts();
	TestBackoffDoublesAndResetsAfterConnectedSession();
	TestReconnectDisabledStops();
	TestReconnectToggleAppliesWhenTheSessionEnds();
	TestReplayRequests();
	TestReconnectCursorFollowsReceivedFrames();
	TestProtocolErrorsCloseTheConnection();
	TestHostDisconnectEndsTheSession();
	TestStop();
	TestReceivedIdentityIsPublishedOnlyWhenApplied();
	TestInterruptedLiveResetClaimsUnknownPrefix();
	TestFullReplayRequestQueuesItsOwnReset();
	for (const std::string_view instance : {"server", "replacement"})
	{
		TestChangedHelloIdentityWaitsForTheReset(instance, false);
		TestChangedHelloIdentityWaitsForTheReset(instance, true);
	}
	TestOldServerRetainsCursorWithoutIdentity("");
	TestOldServerRetainsCursorWithoutIdentity("older-checkpoint-server");
	TestInitialSnapshotCursorIsNotProof();
	for (const OverflowStart start : {OverflowStart::FirstReplay, OverflowStart::AfterServerReset,
									  OverflowStart::AfterLiveReset, OverflowStart::SnapshotCursor,
									  OverflowStart::FullReplayRequest})
	{
		TestQueueOverflowResumesTheReplay(start);
	}
	TestOldDomainCursorReplayClaimsTheAppliedIdentity();
	TestReplayAppliesAcrossAMidBatchReconnect();
	for (const ReplayCause cause :
		 {ReplayCause::ConsumerFailure, ReplayCause::SequenceGap, ReplayCause::ReplayCompleteGap})
	{
		TestReplayRequestWithoutResetResumes(cause);
	}
	for (const ResetState state : {ResetState::Queued, ResetState::Drained, ResetState::Applied})
	{
		TestReplayRequestWithPendingResetClaimsTheAppliedReplay(state);
	}
	TestOwnQueuedResetIsPending();
	return 0;
}
