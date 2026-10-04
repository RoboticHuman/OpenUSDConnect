#include "openusdconnect/client/engine/receiver_endpoint.h"

#include <algorithm>
#include <cassert>
#include <utility>

namespace openusdconnect::client
{
namespace
{

// The consumer drains on its own thread, so an overflowed receiver polls for
// the empty queue.
constexpr std::chrono::milliseconds kDrainPollInterval{100};

[[nodiscard]] std::string Text(const flatbuffers::String* value)
{
	return value ? value->str() : std::string();
}

template <typename T>
[[nodiscard]] std::optional<T> Value(flatbuffers::Optional<T> value) noexcept
{
	return value.has_value() ? std::optional<T>(*value) : std::nullopt;
}

[[nodiscard]] StageMetadata DecodeStageMetadata(const OpenUSDConnect::SetStageMetadata& table)
{
	StageMetadata metadata;
	metadata.TimeCodesPerSecond = Value(table.timeCodesPerSecond());
	metadata.FramesPerSecond = Value(table.framesPerSecond());
	metadata.StartTimeCode = Value(table.startTimeCode());
	metadata.EndTimeCode = Value(table.endTimeCode());
	metadata.MetersPerUnit = Value(table.metersPerUnit());
	if (table.upAxis() && table.upAxis()->size() != 0)
	{
		metadata.UpAxis = table.upAxis()->str();
	}
	return metadata;
}

[[nodiscard]] bool IsEmpty(const StageMetadata& metadata) noexcept
{
	return !metadata.TimeCodesPerSecond && !metadata.FramesPerSecond && !metadata.StartTimeCode &&
		   !metadata.EndTimeCode && !metadata.MetersPerUnit && !metadata.UpAxis;
}

[[nodiscard]] std::vector<std::uint8_t> BuildResyncPayload()
{
	flatbuffers::FlatBufferBuilder builder(64);
	const auto resync = OpenUSDConnect::CreateResync(builder);
	OpenUSDConnect::FinishEnvelopeBuffer(
		builder, OpenUSDConnect::CreateEnvelope(builder, OpenUSDConnect::Payload::Resync,
												resync.Union(), kSchemaVersion));
	const std::uint8_t* bytes = builder.GetBufferPointer();
	return {bytes, bytes + builder.GetSize()};
}

[[nodiscard]] bool IsKnownPayload(OpenUSDConnect::Payload type) noexcept
{
	return type != OpenUSDConnect::Payload::NONE && type <= OpenUSDConnect::Payload::MAX;
}

[[nodiscard]] std::string Address(const ReceiverConfig& config)
{
	return config.Host + ":" + std::to_string(config.Port);
}

} // namespace

ReceiverEndpoint::ReceiverEndpoint(ReceiverConfig config, NotificationQueue& notifications)
	: Config(std::move(config))
	, Notifications(notifications)
	, Inbox(Config.SyncFrom, Config.MaxQueue, true)
	, Reconnect(Config.Reconnect, Config.ReconnectBaseDelay, Config.ReconnectMaxDelay)
{
	assert(IsValidConfiguration(Config));
}

bool ReceiverEndpoint::IsValidConfiguration(const ReceiverConfig& config) noexcept
{
	const bool valid_mode = config.LayerMode == OpenUSDConnect::LayerMode::Managed ||
							(config.LayerMode == OpenUSDConnect::LayerMode::SharedStage &&
							 !config.LayeredReplay && config.Department.empty());
	return valid_mode && !config.Host.empty() && config.Port != 0 &&
		   ReceiverInbox::IsValidConfiguration(config.SyncFrom, config.MaxQueue) &&
		   config.SocketTimeout.count() > 0 && config.MaxConsecutiveTimeouts != 0 &&
		   ReconnectPolicy::IsValidConfiguration(config.ReconnectBaseDelay,
												 config.ReconnectMaxDelay);
}

const ReceiverConfig& ReceiverEndpoint::Configuration() const noexcept
{
	return Config;
}

void ReceiverEndpoint::SetReconnect(bool enabled)
{
	std::lock_guard lock(Mutex);
	Reconnect.SetEnabled(enabled);
}

bool ReceiverEndpoint::Start(TimePoint now)
{
	std::lock_guard lock(Mutex);
	if (State != ConnectionState::Idle)
	{
		return false;
	}
	BeginAttempt(now);
	return true;
}

void ReceiverEndpoint::OnConnected(std::string_view token)
{
	std::lock_guard lock(Mutex);
	if (State == ConnectionState::Stopped)
	{
		Actions.push_back(CloseAction{DisconnectReason::Stopped});
		return;
	}
	if (State != ConnectionState::Connecting)
	{
		return;
	}
	const ConnectionStart connection = Inbox.BeginConnection();
	ConnectionGeneration = connection.Generation;
	ConnectionSyncFrom = connection.SyncFrom;
	ConsecutiveTimeouts = 0;
	Decoder.Reset();
	Rejection.reset();
	State = ConnectionState::Handshaking;

	HelloParameters hello;
	hello.Role = "receiver";
	hello.SyncFrom = ConnectionSyncFrom;
	hello.ClientId = Config.ClientId;
	hello.Origin = Config.Origin;
	hello.Department = Config.Department;
	hello.Token = token;
	hello.LayeredReplay = Config.LayeredReplay;
	hello.LayerMode = Config.LayerMode;
	hello.ReplayPrefix = Identity.BeginConnection();
	flatbuffers::FlatBufferBuilder builder(256);
	if (BuildHelloFrame(builder, hello) != ProtocolResult::Success)
	{
		Log(LogLevel::Error, "could not build the Hello frame");
		CloseConnection(DisconnectReason::ProtocolError);
		return;
	}
	const std::uint8_t* bytes = builder.GetBufferPointer();
	Actions.push_back(SendAction{{bytes, bytes + builder.GetSize()}});
}

void ReceiverEndpoint::OnBytes(const std::uint8_t* data, std::size_t size)
{
	std::lock_guard lock(Mutex);
	if (!IsOpen())
	{
		return;
	}
	if (size != 0)
	{
		ConsecutiveTimeouts = 0;
	}
	std::vector<std::vector<std::uint8_t>> frames;
	if (Decoder.Feed(data, size, frames) != FrameResult::Success)
	{
		Log(LogLevel::Warning, "invalid frame header");
		CloseConnection(DisconnectReason::ProtocolError);
		return;
	}
	for (std::vector<std::uint8_t>& frame : frames)
	{
		if (!IsOpen())
		{
			break;
		}
		HandleFrame(std::move(frame));
	}
}

void ReceiverEndpoint::OnReadTimeout()
{
	std::lock_guard lock(Mutex);
	if (!IsOpen())
	{
		return;
	}
	++ConsecutiveTimeouts;
	const std::string count = std::to_string(ConsecutiveTimeouts);
	if (ConsecutiveTimeouts < Config.MaxConsecutiveTimeouts)
	{
		Log(LogLevel::Debug,
			"read timeout " + count + "/" + std::to_string(Config.MaxConsecutiveTimeouts));
		return;
	}
	Log(LogLevel::Warning, count + " consecutive read timeouts, reconnecting");
	CloseConnection(DisconnectReason::ReadTimeout);
}

void ReceiverEndpoint::OnDisconnected(DisconnectReason reason, TimePoint now)
{
	std::lock_guard lock(Mutex);
	if (IsOpen())
	{
		Log(LogLevel::Warning, "connection to " + Address(Config) + " lost");
		EndSession(reason);
	}
	else if (State != ConnectionState::Connecting && State != ConnectionState::Closing)
	{
		return;
	}
	ScheduleNextAttempt(now);
}

void ReceiverEndpoint::OnTick(TimePoint now)
{
	std::lock_guard lock(Mutex);
	if (State == ConnectionState::Backoff && now >= WakeTime)
	{
		BeginAttempt(now);
	}
	else if (State == ConnectionState::DrainWait)
	{
		PollDrain(now);
	}
}

void ReceiverEndpoint::Stop()
{
	std::lock_guard lock(Mutex);
	if (State == ConnectionState::Stopped)
	{
		return;
	}
	if (IsOpen())
	{
		CloseConnection(DisconnectReason::Stopped);
	}
	else if (State == ConnectionState::Connecting)
	{
		Actions.push_back(CloseAction{DisconnectReason::Stopped});
	}
	State = ConnectionState::Stopped;
	Log(LogLevel::Info, "stopped");
}

std::vector<Action> ReceiverEndpoint::TakeActions()
{
	std::lock_guard lock(Mutex);
	return std::exchange(Actions, {});
}

std::optional<TimePoint> ReceiverEndpoint::NextWake() const
{
	std::lock_guard lock(Mutex);
	if (State == ConnectionState::Backoff || State == ConnectionState::DrainWait)
	{
		return WakeTime;
	}
	return std::nullopt;
}

std::vector<std::vector<std::uint8_t>>
ReceiverEndpoint::DrainFrames(std::optional<std::size_t> max_frames)
{
	return Inbox.Drain(max_frames);
}

std::uint64_t ReceiverEndpoint::Generation() const noexcept
{
	return Inbox.Generation();
}

bool ReceiverEndpoint::MarkAppliedThrough(std::uint64_t generation, std::int32_t sequence)
{
	return Inbox.MarkAppliedThrough(generation, sequence);
}

void ReceiverEndpoint::ResetAppliedProgress() noexcept
{
	Inbox.ResetAppliedProgress();
}

bool ReceiverEndpoint::MarkReplayApplied()
{
	std::lock_guard lock(Mutex);
	// Every drained frame applied, so the replay head counts as applied even
	// when a reconnect rejected the batch's MarkAppliedThrough.
	if (!Inbox.MarkReplayApplied())
	{
		return false;
	}
	Identity.MarkReplayApplied();
	return true;
}

bool ReceiverEndpoint::RequestReplayFrom(std::int32_t sequence)
{
	if (sequence < 1)
	{
		return false;
	}
	std::lock_guard lock(Mutex);
	RequestReplay(sequence, DisconnectReason::ReplayRequested);
	return true;
}

std::uint64_t ReceiverEndpoint::FreezeMarker() const noexcept
{
	return Inbox.FreezeMarker();
}

bool ReceiverEndpoint::DrainedThrough(std::uint64_t marker) const noexcept
{
	return Inbox.DrainedThrough(marker);
}

ReceiverStatus ReceiverEndpoint::Status() const
{
	std::lock_guard lock(Mutex);
	ReceiverStatus status;
	status.Connected = State == ConnectionState::Connected;
	status.Synchronized = status.Connected && Inbox.Synchronized();
	status.Stopped = State == ConnectionState::Stopped;
	status.ReplayHeadSequence = Inbox.ReplayHeadSequence();
	status.ReplayEpoch = Inbox.ReplayEpoch();
	if (const std::optional<ReplayIdentity>& applied = Identity.Applied())
	{
		status.ServerInstance = applied->ServerInstance;
	}
	status.LayeredReplayActive = LayeredReplayActive;
	status.LayerModeActive = LayerModeActive;
	status.Rejection = Rejection;
	status.Metadata = Metadata;
	status.QueuedFrames = Inbox.Size();
	status.LastSequence = Inbox.LastSequence();
	status.LastAppliedSequence = Inbox.LastAppliedSequence();
	return status;
}

bool ReceiverEndpoint::IsOpen() const noexcept
{
	return State == ConnectionState::Handshaking || State == ConnectionState::Connected;
}

void ReceiverEndpoint::BeginAttempt(TimePoint now)
{
	State = ConnectionState::Connecting;
	Log(LogLevel::Info, "connecting to " + Address(Config));
	Actions.push_back(ConnectAction{Config.Host, Config.Port, now + Config.SocketTimeout});
}

void ReceiverEndpoint::ScheduleNextAttempt(TimePoint now)
{
	if (Rejection || !Reconnect.Enabled())
	{
		State = ConnectionState::Stopped;
		Log(LogLevel::Info, "stopped");
		return;
	}
	if (Inbox.Overflowed())
	{
		Inbox.ClearOverflow();
		DrainDeadline = Reconnect.DrainDeadline(now);
		WakeTime = now;
		State = ConnectionState::DrainWait;
		Log(LogLevel::Info, "waiting for the queue to drain before reconnecting");
		PollDrain(now);
		return;
	}
	WakeTime = Reconnect.NextAttempt(now);
	State = ConnectionState::Backoff;
	const auto delay = std::chrono::duration_cast<std::chrono::milliseconds>(WakeTime - now);
	Log(LogLevel::Info, "reconnecting in " + std::to_string(delay.count()) + " ms");
	Actions.push_back(WakeAction{WakeTime});
}

void ReceiverEndpoint::PollDrain(TimePoint now)
{
	if (Inbox.Size() == 0)
	{
		BeginAttempt(now);
	}
	else if (now >= DrainDeadline)
	{
		Log(LogLevel::Warning, "drain wait timed out, reconnecting anyway");
		BeginAttempt(now);
	}
	else if (now >= WakeTime)
	{
		WakeTime = std::min(now + kDrainPollInterval, DrainDeadline);
		Actions.push_back(WakeAction{WakeTime});
	}
}

void ReceiverEndpoint::CloseConnection(DisconnectReason reason)
{
	Actions.push_back(CloseAction{reason});
	EndSession(reason);
	State = ConnectionState::Closing;
}

void ReceiverEndpoint::EndSession(DisconnectReason reason)
{
	if (State == ConnectionState::Connected)
	{
		Notify(Disconnected{reason});
	}
	Inbox.Disconnect(ConnectionGeneration);
}

void ReceiverEndpoint::RequestReplay(std::int32_t sequence, DisconnectReason reason)
{
	Identity.RequestReplayFrom(sequence, Inbox.ResetPending());
	[[maybe_unused]] const bool requested = Inbox.RequestReplayFrom(sequence);
	assert(requested);
	if (IsOpen())
	{
		CloseConnection(reason);
	}
}

void ReceiverEndpoint::HandleFrame(std::vector<std::uint8_t> frame)
{
	EnvelopeView envelope;
	const ProtocolResult decoded = DecodeEnvelope(frame.data(), frame.size(), envelope);
	if (decoded != ProtocolResult::Success || !IsKnownPayload(envelope.PayloadType()))
	{
		Log(LogLevel::Error, decoded == ProtocolResult::SchemaVersionMismatch
								 ? "frame uses an unsupported schema version"
								 : "malformed frame");
		CloseConnection(DisconnectReason::ProtocolError);
		return;
	}
	if (State == ConnectionState::Handshaking)
	{
		HandleHandshake(envelope);
	}
	else
	{
		HandleMessage(envelope, frame);
	}
}

void ReceiverEndpoint::HandleHandshake(EnvelopeView envelope)
{
	const HandshakeResponseView response(envelope);
	switch (response.Kind())
	{
	case HandshakeResponseKind::Accepted:
		AcceptHello(*response.Accepted());
		return;
	case HandshakeResponseKind::AuthenticationRejected:
		Reject({true, OpenUSDConnect::HelloRejectionCode::Unspecified,
				Text(response.AuthenticationRejection()->reason())});
		return;
	case HandshakeResponseKind::ConfigurationRejected:
	{
		const OpenUSDConnect::HelloRejected& rejection = *response.ConfigurationRejection();
		Reject({false, rejection.code(), Text(rejection.reason())});
		return;
	}
	case HandshakeResponseKind::Unexpected:
		return;
	}
}

void ReceiverEndpoint::AcceptHello(const OpenUSDConnect::HelloOk& hello)
{
	LayerModeActive = hello.layer_mode();
	if (LayerModeActive != Config.LayerMode)
	{
		Reject({false, OpenUSDConnect::HelloRejectionCode::LayerModeMismatch,
				"server did not negotiate requested layer mode"});
		return;
	}
	LayeredReplayActive = Config.LayeredReplay && hello.layered_replay();
	if (Config.LayeredReplay && !LayeredReplayActive)
	{
		Reject({false, OpenUSDConnect::HelloRejectionCode::LayeredReplayRequired,
				"server did not negotiate requested layered replay"});
		return;
	}
	if (std::string token = Text(hello.token()); !token.empty())
	{
		Log(LogLevel::Info, "token issued by server");
		Notify(TokenIssued{std::move(token)});
	}
	if (const OpenUSDConnect::SetStageMetadata* table = hello.stage_metadata())
	{
		if (StageMetadata metadata = DecodeStageMetadata(*table); !IsEmpty(metadata))
		{
			Metadata = metadata;
			Notify(std::move(metadata));
		}
	}

	Identity.AcceptHello(ConnectionSyncFrom, hello.replay_identity(), Text(hello.server_instance()),
						 Value(hello.replay_epoch()));
	if (Identity.ResetRequired())
	{
		assert(ConnectionSyncFrom == 1);
		AcceptFrame(ReceiverMessageKind::Resync, 0, BuildResyncPayload());
		if (!IsOpen())
		{
			return;
		}
	}
	State = ConnectionState::Connected;
	Reconnect.Reset();
	Notify(Connected{});
	Log(LogLevel::Info, "connected (sync_from=" + std::to_string(ConnectionSyncFrom) + ")");
}

void ReceiverEndpoint::Reject(HandshakeRejected rejection)
{
	const std::string description =
		rejection.Authentication
			? "authentication rejected"
			: "connection rejected (code " + std::to_string(static_cast<int>(rejection.Code)) + ")";
	Log(LogLevel::Error, description + ": " + rejection.Reason);
	Notify(rejection);
	Rejection = std::move(rejection);
	CloseConnection(DisconnectReason::HandshakeRejected);
}

void ReceiverEndpoint::HandleMessage(EnvelopeView envelope, std::vector<std::uint8_t>& frame)
{
	const OpenUSDConnect::Envelope& message = *envelope.Get();
	ReceiverMessageKind kind = ReceiverMessageKind::Other;
	std::int32_t sequence = 0;
	switch (message.payload_type())
	{
	case OpenUSDConnect::Payload::Ping:
		return;
	case OpenUSDConnect::Payload::ReplayComplete:
		AcceptReplayComplete(*message.payload_as_ReplayComplete());
		return;
	case OpenUSDConnect::Payload::PlaybackState:
	{
		const OpenUSDConnect::PlaybackState& state = *message.payload_as_PlaybackState();
		Notify(PlaybackState{state.time(), state.playing(), state.rate(),
							 Text(state.leader_client_id())});
		return;
	}
	case OpenUSDConnect::Payload::PlaybackClaimed:
		Notify(PlaybackClaimed{Text(message.payload_as_PlaybackClaimed()->leader_client_id())});
		return;
	case OpenUSDConnect::Payload::PlaybackRejected:
	{
		const OpenUSDConnect::PlaybackRejected& rejected = *message.payload_as_PlaybackRejected();
		Notify(
			PlaybackRejected{Text(rejected.reason()), Text(rejected.current_leader_client_id())});
		return;
	}
	case OpenUSDConnect::Payload::BroadcastEvent:
		kind = ReceiverMessageKind::Event;
		sequence = message.payload_as_BroadcastEvent()->seq();
		break;
	case OpenUSDConnect::Payload::LayerGraphState:
		kind = ReceiverMessageKind::LayerGraphState;
		sequence = message.payload_as_LayerGraphState()->seq();
		break;
	case OpenUSDConnect::Payload::Resync:
		kind = ReceiverMessageKind::Resync;
		break;
	default:
		break;
	}
	AcceptFrame(kind, sequence, std::move(frame));
}

void ReceiverEndpoint::AcceptReplayComplete(const OpenUSDConnect::ReplayComplete& complete)
{
	const AcceptResult result =
		Inbox.AcceptReplayComplete(ConnectionGeneration, complete.head_seq(), complete.epoch());
	if (result == AcceptResult::Accepted)
	{
		Identity.AcceptReplayComplete(complete.epoch());
	}
	else if (result == AcceptResult::SequenceGap)
	{
		ReplayAfterGap("replay head " + std::to_string(complete.head_seq()) + " was not received");
	}
	else if (result == AcceptResult::InvalidSequence)
	{
		Log(LogLevel::Warning,
			"ignoring ReplayComplete with invalid head " + std::to_string(complete.head_seq()));
	}
}

void ReceiverEndpoint::AcceptFrame(ReceiverMessageKind kind, std::int32_t sequence,
								   std::vector<std::uint8_t> frame)
{
	switch (Inbox.Accept(ConnectionGeneration, kind, sequence, std::move(frame)))
	{
	case AcceptResult::Accepted:
		if (kind == ReceiverMessageKind::Resync)
		{
			Identity.AcceptResync();
		}
		return;
	case AcceptResult::StaleGeneration:
	case AcceptResult::Duplicate:
		return;
	case AcceptResult::InvalidSequence:
		Log(LogLevel::Warning, "ignoring frame with invalid sequence " + std::to_string(sequence));
		return;
	case AcceptResult::SequenceGap:
		ReplayAfterGap("sequence gap before " + std::to_string(sequence));
		return;
	case AcceptResult::QueueFull:
		Log(LogLevel::Warning, "queue full (" + std::to_string(Config.MaxQueue) +
								   "), disconnecting to replay from server");
		CloseConnection(DisconnectReason::QueueFull);
		return;
	}
}

void ReceiverEndpoint::ReplayAfterGap(const std::string& description)
{
	const std::int32_t replay_from = Inbox.LastAppliedSequence() + 1;
	Log(LogLevel::Error, description + "; replaying from applied " + std::to_string(replay_from));
	RequestReplay(replay_from, DisconnectReason::SequenceGap);
}

void ReceiverEndpoint::Notify(Notification notification)
{
	Notifications.Push(std::move(notification));
}

void ReceiverEndpoint::Log(LogLevel level, std::string message)
{
	Actions.push_back(LogAction{level, std::move(message)});
}

} // namespace openusdconnect::client
