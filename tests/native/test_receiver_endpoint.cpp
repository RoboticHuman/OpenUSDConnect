#include "openusdconnect/client/engine/receiver_endpoint.h"

#include "test_check.h"

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <string_view>
#include <type_traits>
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

using Bytes = std::vector<std::uint8_t>;

[[nodiscard]] ReceiverConfig TestConfig()
{
	ReceiverConfig config;
	config.Host = "127.0.0.1";
	config.Port = 7200;
	config.ClientId = "client";
	config.Origin = "origin";
	return config;
}

template <typename Field>
[[nodiscard]] ReceiverConfig With(ReceiverConfig config, Field ReceiverConfig::* field,
								  std::common_type_t<Field> value)
{
	config.*field = value;
	return config;
}

[[nodiscard]] std::string Text(const flatbuffers::String* value)
{
	return value ? value->str() : std::string();
}

[[nodiscard]] const OpenUSDConnect::Envelope& Decode(const Bytes& payload)
{
	EnvelopeView view;
	CHECK(DecodeEnvelope(payload.data(), payload.size(), view) == ProtocolResult::Success);
	return *view.Get();
}

[[nodiscard]] std::vector<Payload> Kinds(const std::vector<Bytes>& frames)
{
	std::vector<Payload> kinds;
	for (const Bytes& frame : frames)
	{
		kinds.push_back(Decode(frame).payload_type());
	}
	return kinds;
}

[[nodiscard]] std::vector<std::int32_t> Sequences(const std::vector<Bytes>& frames)
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

[[nodiscard]] Bytes Frame(flatbuffers::FlatBufferBuilder& builder, Payload type,
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

[[nodiscard]] flatbuffers::Offset<flatbuffers::String>
OptionalString(flatbuffers::FlatBufferBuilder& builder, std::string_view text)
{
	return text.empty() ? flatbuffers::Offset<flatbuffers::String>() : CreateString(builder, text);
}

[[nodiscard]] flatbuffers::Optional<double> Wire(std::optional<double> value)
{
	return value ? flatbuffers::Optional<double>(*value) : flatbuffers::nullopt;
}

[[nodiscard]] Bytes HelloOk(const Hello& hello = {})
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

[[nodiscard]] Bytes AuthRejected(std::string_view reason)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto rejected =
		OpenUSDConnect::CreateAuthRejected(builder, CreateString(builder, reason));
	return Frame(builder, Payload::AuthRejected, rejected.Union());
}

[[nodiscard]] Bytes HelloRejected(HelloRejectionCode code, std::string_view reason)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto rejected =
		OpenUSDConnect::CreateHelloRejected(builder, code, CreateString(builder, reason));
	return Frame(builder, Payload::HelloRejected, rejected.Union());
}

[[nodiscard]] Bytes Ping()
{
	flatbuffers::FlatBufferBuilder builder(32);
	return Frame(builder, Payload::Ping, OpenUSDConnect::CreatePing(builder).Union());
}

[[nodiscard]] Bytes Resync()
{
	flatbuffers::FlatBufferBuilder builder(32);
	return Frame(builder, Payload::Resync, OpenUSDConnect::CreateResync(builder).Union());
}

[[nodiscard]] Bytes ReplayComplete(std::int32_t head, std::uint64_t epoch)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto complete = OpenUSDConnect::CreateReplayComplete(builder, head, epoch);
	return Frame(builder, Payload::ReplayComplete, complete.Union());
}

[[nodiscard]] Bytes Event(std::int32_t sequence)
{
	flatbuffers::FlatBufferBuilder builder(128);
	const auto prim = OpenUSDConnect::CreateEnsurePrim(
		builder, CreateString(builder, "/World/P" + std::to_string(sequence)));
	const auto event = OpenUSDConnect::CreateEventWrapper(
		builder, OpenUSDConnect::EventPayload::EnsurePrim, prim.Union());
	const auto broadcast = OpenUSDConnect::CreateBroadcastEvent(builder, sequence, event);
	return Frame(builder, Payload::BroadcastEvent, broadcast.Union());
}

[[nodiscard]] Bytes LayerGraph(std::int32_t sequence)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto state = OpenUSDConnect::CreateLayerGraphState(builder, sequence);
	return Frame(builder, Payload::LayerGraphState, state.Union());
}

[[nodiscard]] Bytes LayerStack()
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto layer = OpenUSDConnect::CreateLogicalLayerState(builder, CreateString(builder, "a"));
	const auto stack = OpenUSDConnect::CreateLayerStackState(
		builder, CreateString(builder, "generation"), 1, builder.CreateVector(&layer, 1));
	return Frame(builder, Payload::LayerStackState, stack.Union());
}

[[nodiscard]] Bytes Playback(double time, bool playing, double rate, std::string_view leader)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto state = OpenUSDConnect::CreatePlaybackState(builder, time, playing, rate,
														   CreateString(builder, leader));
	return Frame(builder, Payload::PlaybackState, state.Union());
}

[[nodiscard]] Bytes Claimed(std::string_view leader)
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto claimed =
		OpenUSDConnect::CreatePlaybackClaimed(builder, CreateString(builder, leader));
	return Frame(builder, Payload::PlaybackClaimed, claimed.Union());
}

[[nodiscard]] Bytes ClaimRejected(std::string_view reason, std::string_view leader)
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

[[nodiscard]] SentHello DecodeHello(const Bytes& frame)
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

// Whether the server resumes this Hello rather than sending Resync.
[[nodiscard]] bool ServerResumes(const SentHello& hello, std::string_view instance,
								 std::uint64_t epoch, std::int32_t head)
{
	const bool claim_holds = !hello.ReplayServerInstance || hello.Claims(instance, epoch);
	return hello.SyncFrom <= head + 1 && (hello.SyncFrom == 1 || claim_holds);
}

template <typename T>
[[nodiscard]] const T& As(const Notification& notification)
{
	CHECK(std::holds_alternative<T>(notification));
	return std::get<T>(notification);
}

// Plays both the host and the stage-owning consumer around one endpoint.
class Receiver final
{
public:
	explicit Receiver(const ReceiverConfig& config = TestConfig())
		: Endpoint(config, Notifications)
		, Applied(config.SyncFrom - 1)
	{
	}

	// Actions since the last call, without log lines.
	[[nodiscard]] std::vector<Action> Commands()
	{
		Collect();
		return std::exchange(Pending, {});
	}

	template <typename T>
	[[nodiscard]] T Single()
	{
		std::vector<Action> commands = Commands();
		CHECK(commands.size() == 1);
		CHECK(std::holds_alternative<T>(commands.front()));
		return std::get<T>(std::move(commands.front()));
	}

	[[nodiscard]] bool Logged(LogLevel level, std::string_view text)
	{
		Collect();
		return std::any_of(Logs.begin(), Logs.end(),
						   [&](const LogAction& log)
						   {
							   return log.Level == level &&
									  log.Message.find(text) != std::string::npos;
						   });
	}

	[[nodiscard]] std::vector<Notification> Notices()
	{
		return Notifications.Drain();
	}

	[[nodiscard]] ReceiverStatus Status() const
	{
		return Endpoint.Status();
	}

	// Completes the pending connect attempt and returns the Hello it sent.
	SentHello Connect(std::string_view token = {})
	{
		static_cast<void>(Single<ConnectAction>());
		Endpoint.OnConnected(token);
		return DecodeHello(Single<SendAction>().Bytes);
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

	void Feed(const Bytes& bytes)
	{
		Endpoint.OnBytes(bytes.data(), bytes.size());
	}

	void Disconnect(DisconnectReason reason = DisconnectReason::PeerClosed)
	{
		Endpoint.OnDisconnected(reason, Now);
	}

	void Advance(std::chrono::milliseconds elapsed)
	{
		Now += elapsed;
		Endpoint.OnTick(Now);
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
	// returns the next Hello.
	SentHello ReconnectAfterDrain()
	{
		Disconnect();
		CHECK(Single<WakeAction>().Time == Now + 100ms);
		static_cast<void>(Apply());
		Advance(1ms);
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

	NotificationQueue Notifications;
	ReceiverEndpoint Endpoint;
	TimePoint Now;
	// The consumer's applied cursor.
	std::int32_t Applied;

private:
	void Collect()
	{
		for (Action& action : Endpoint.TakeActions())
		{
			if (LogAction* log = std::get_if<LogAction>(&action))
			{
				Logs.push_back(std::move(*log));
			}
			else
			{
				Pending.push_back(std::move(action));
			}
		}
	}

	std::vector<Action> Pending;
	std::vector<LogAction> Logs;
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
		{With(valid, &ReceiverConfig::ClientId, ""), false},
		{With(valid, &ReceiverConfig::Origin, ""), false},
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
	CHECK(receiver.Logged(LogLevel::Info, "connecting to 127.0.0.1:7200"));
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

void TestAcceptedHelloNotifiesInOrder()
{
	Receiver receiver;
	static_cast<void>(receiver.Start());
	// Anything before the handshake response is ignored.
	receiver.Feed(server::Ping());
	receiver.Feed(server::Event(1));
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
	CHECK(receiver.Logged(LogLevel::Info, "token issued by server"));
	CHECK(receiver.Logged(LogLevel::Info, "connected (sync_from=1)"));

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
	const std::vector<Notification> notices = receiver.Notices();
	CHECK(notices.size() == 1);
	static_cast<void>(As<Connected>(notices[0]));
}

void TestAuthenticationRejectionStops()
{
	Receiver receiver;
	static_cast<void>(receiver.Start());
	receiver.Feed(server::AuthRejected("invalid token"));
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::HandshakeRejected);
	CHECK(receiver.Logged(LogLevel::Error, "authentication rejected: invalid token"));
	const std::vector<Notification> notices = receiver.Notices();
	CHECK(notices.size() == 1);
	const HandshakeRejected& rejected = As<HandshakeRejected>(notices[0]);
	CHECK(rejected.Authentication);
	CHECK(rejected.Code == HelloRejectionCode::Unspecified);
	CHECK(rejected.Reason == "invalid token");

	receiver.Disconnect();
	CHECK(receiver.Commands().empty());
	CHECK(!receiver.Endpoint.NextWake());
	const ReceiverStatus status = receiver.Status();
	CHECK(status.Stopped && status.Rejection && status.Rejection->Authentication);
	CHECK(receiver.Notices().empty());
}

void TestHelloRejectionStops()
{
	Receiver receiver;
	static_cast<void>(receiver.Start());
	receiver.Feed(server::HelloRejected(HelloRejectionCode::LayeredReplayRequired,
										"layered replay is required"));
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::HandshakeRejected);
	CHECK(receiver.Logged(LogLevel::Error, "connection rejected (code 1): layered replay"));
	receiver.Disconnect();
	const ReceiverStatus status = receiver.Status();
	CHECK(status.Stopped);
	CHECK(status.Rejection && !status.Rejection->Authentication);
	CHECK(status.Rejection->Code == HelloRejectionCode::LayeredReplayRequired);
	CHECK(status.Rejection->Reason == "layered replay is required");
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
		const std::vector<Notification> notices = receiver.Notices();
		CHECK(notices.size() == 1);
		const HandshakeRejected& rejected = As<HandshakeRejected>(notices[0]);
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

	receiver.Feed(server::ReplayComplete(-1, 8));
	CHECK(receiver.Logged(LogLevel::Warning, "invalid head -1"));
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
	receiver.Feed(server::Event(0));
	CHECK(receiver.Logged(LogLevel::Warning, "invalid sequence 0"));
	CHECK(receiver.Status().QueuedFrames == 1);
	CHECK(receiver.Commands().empty());

	receiver.Feed(server::Event(5));
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::SequenceGap);
	CHECK(receiver.Logged(LogLevel::Error, "sequence gap before 5; replaying from applied 3"));
	const std::vector<Notification> notices = receiver.Notices();
	CHECK(notices.size() == 1);
	CHECK(As<Disconnected>(notices[0]).Reason == DisconnectReason::SequenceGap);
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
	CHECK(receiver.Logged(LogLevel::Warning, "queue full (2)"));
	CHECK(As<Disconnected>(receiver.Notices().at(0)).Reason == DisconnectReason::QueueFull);
	if (outcome == DrainOutcome::DrainedEarlier)
	{
		static_cast<void>(receiver.Apply());
	}
	receiver.Disconnect();
	CHECK(receiver.Logged(LogLevel::Info, "waiting for the queue to drain"));
	if (outcome != DrainOutcome::DrainedEarlier)
	{
		CHECK(receiver.Single<WakeAction>().Time == receiver.Now + 100ms);
		CHECK(receiver.Endpoint.NextWake() == receiver.Now + 100ms);
		receiver.Advance(50ms);
		CHECK(receiver.Commands().empty());
		receiver.Advance(50ms);
		CHECK(receiver.Single<WakeAction>().Time == receiver.Now + 100ms);
	}
	if (outcome == DrainOutcome::DrainedDuringWait)
	{
		static_cast<void>(receiver.Apply());
		receiver.Advance(1ms);
	}
	else if (outcome == DrainOutcome::TimedOut)
	{
		receiver.Advance(1900ms);
		CHECK(receiver.Logged(LogLevel::Warning, "drain wait timed out"));
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
	CHECK(receiver.Logged(LogLevel::Debug, "read timeout 2/3"));
	receiver.Feed(server::HelloOk());
	time_out(2);
	receiver.Feed(server::Ping());
	time_out(2);
	CHECK(receiver.Commands().empty());
	CHECK(receiver.Status().Connected);
	time_out(1);
	CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::ReadTimeout);
	CHECK(receiver.Logged(LogLevel::Warning, "3 consecutive read timeouts"));
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
	CHECK(receiver.Logged(LogLevel::Info, "reconnecting in 8000 ms"));
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
		CHECK(receiver.Logged(LogLevel::Info, "stopped"));
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

void TestFramingAcrossReads()
{
	Receiver receiver;
	static_cast<void>(receiver.Start());
	const Bytes hello = server::HelloOk();
	for (const std::uint8_t byte : hello)
	{
		receiver.Endpoint.OnBytes(&byte, 1);
	}
	CHECK(receiver.Status().Connected);
	Bytes batch = server::Event(1);
	const Bytes second = server::Event(2);
	batch.insert(batch.end(), second.begin(), second.end());
	receiver.Feed(batch);
	CHECK(receiver.Status().LastSequence == 2);
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

	struct Case final
	{
		const Bytes* Frame;
		std::string_view Log;
	};
	const Case cases[] = {
		{&future_schema, "unsupported schema version"}, {&no_payload, "malformed frame"},
		{&unknown_payload, "malformed frame"},			{&garbage, "malformed frame"},
		{&empty_header, "invalid frame header"},
	};
	for (const bool handshaking : {true, false})
	{
		for (const Case& error : cases)
		{
			Receiver receiver;
			static_cast<void>(receiver.Start());
			if (!handshaking)
			{
				receiver.Feed(server::HelloOk());
			}
			receiver.Feed(*error.Frame);
			CHECK(receiver.Single<CloseAction>().Reason == DisconnectReason::ProtocolError);
			CHECK(receiver.Logged(
				error.Frame == &empty_header ? LogLevel::Warning : LogLevel::Error, error.Log));
			CHECK(receiver.Reconnect().SyncFrom == 1);
		}
	}
}

void TestHostDisconnectEndsTheSession()
{
	Receiver receiver;
	static_cast<void>(receiver.Start());
	receiver.Disconnect(DisconnectReason::TransportError);
	CHECK(receiver.Logged(LogLevel::Warning, "connection to 127.0.0.1:7200 lost"));
	CHECK(receiver.Notices().empty());
	static_cast<void>(receiver.Single<WakeAction>());
	receiver.Advance(1s);
	static_cast<void>(receiver.Connect());
	receiver.Feed(server::HelloOk());
	static_cast<void>(receiver.Notices());
	receiver.Feed(server::ReplayComplete(0, 0));
	CHECK(receiver.Endpoint.MarkReplayApplied());
	receiver.Disconnect(DisconnectReason::TransportError);
	const std::vector<Notification> notices = receiver.Notices();
	CHECK(notices.size() == 1);
	CHECK(As<Disconnected>(notices[0]).Reason == DisconnectReason::TransportError);
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
		CHECK(As<Disconnected>(receiver.Notices().at(0)).Reason == DisconnectReason::Stopped);
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
		CHECK(receiver.Logged(LogLevel::Error,
							  "replay head 3 was not received; replaying from applied 2"));
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
	TestAcceptedHelloNotifiesInOrder();
	TestEmptyStageMetadataIsNotNotified();
	TestAuthenticationRejectionStops();
	TestHelloRejectionStops();
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
	TestReplayRequests();
	TestReconnectCursorFollowsReceivedFrames();
	TestFramingAcrossReads();
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
