#include "openusdconnect/client/engine/producer_endpoint.h"

#include "endpoint_common.h"

#include <algorithm>
#include <cassert>
#include <utility>

namespace openusdconnect::client
{
namespace
{

using detail::Address;
using detail::Share;
using detail::Text;

// Bounds a hostile retry-after so the deadline arithmetic cannot overflow.
constexpr std::chrono::hours kMaxRetryAfter{1};

constexpr auto kUnexpectedId = static_cast<std::uint8_t>(RejectionCode::UnexpectedId);

[[nodiscard]] bool IsCompleteFrame(const std::vector<std::uint8_t>& frame) noexcept
{
	std::size_t payload_size = 0;
	return frame.size() > kFrameHeaderSize &&
		   TryReadFrameHeader(frame.data(), kDefaultMaxFrameSize, payload_size) &&
		   payload_size == frame.size() - kFrameHeaderSize;
}

[[nodiscard]] SharedByteBuffer BuildQuitFrame()
{
	flatbuffers::FlatBufferBuilder builder(32);
	const auto quit = OpenUSDConnect::CreateQuit(builder);
	[[maybe_unused]] const ProtocolResult finished = FinishEnvelopeFrame(
		builder, OpenUSDConnect::CreateEnvelope(builder, OpenUSDConnect::Payload::Quit,
												quit.Union(), kSchemaVersion));
	assert(finished == ProtocolResult::Success);
	return Share(builder);
}

[[nodiscard]] std::string_view LayerModeName(OpenUSDConnect::LayerMode mode) noexcept
{
	switch (mode)
	{
	case OpenUSDConnect::LayerMode::Managed:
		return "managed";
	case OpenUSDConnect::LayerMode::SharedStage:
		return "shared_stage";
	}
	return "unknown";
}

// Erases the queued actions of the given kinds; returns whether there were any.
template <typename... Kinds>
bool Discard(std::vector<Action>& actions)
{
	const auto kept = std::remove_if(actions.begin(), actions.end(),
									 [](const Action& action)
									 {
										 return (std::holds_alternative<Kinds>(action) || ...);
									 });
	const bool discarded = kept != actions.end();
	actions.erase(kept, actions.end());
	return discarded;
}

[[nodiscard]] std::chrono::steady_clock::duration RetryAfter(float seconds)
{
	const std::chrono::duration<double> requested(seconds);
	if (!(requested > requested.zero()))
	{
		return std::chrono::steady_clock::duration::zero();
	}
	return std::chrono::duration_cast<std::chrono::steady_clock::duration>(
		std::min<std::chrono::duration<double>>(requested, kMaxRetryAfter));
}

} // namespace

ProducerEndpoint::ProducerEndpoint(ProducerConfig config, NotificationQueue& notifications)
	: Config(std::move(config))
	, Notifications(notifications)
	, Session(Config.MaxPendingTransactions)
	, Reconnect(true, Config.ReconnectBaseDelay, Config.ReconnectMaxDelay)
	, SessionId(Config.SessionId)
{
	assert(IsValidConfiguration(Config));
}

bool ProducerEndpoint::IsValidConfiguration(const ProducerConfig& config) noexcept
{
	const bool valid_mode =
		config.LayerMode == OpenUSDConnect::LayerMode::Managed ||
		(config.LayerMode == OpenUSDConnect::LayerMode::SharedStage && config.Department.empty());
	return valid_mode && !config.Host.empty() && config.Port != 0 && !config.ClientId.empty() &&
		   IsValidProducerSessionId(config.SessionId) && config.HandshakeTimeout.count() > 0 &&
		   ProducerSession::IsValidConfiguration(config.MaxPendingTransactions) &&
		   ReconnectPolicy::IsValidConfiguration(config.ReconnectBaseDelay,
												 config.ReconnectMaxDelay);
}

const ProducerConfig& ProducerEndpoint::Configuration() const noexcept
{
	return Config;
}

bool ProducerEndpoint::RequestConnect(TimePoint now, TimePoint deadline)
{
	std::lock_guard lock(Mutex);
	if (State != ConnectionState::Idle || Rejection || SessionFailure ||
		now < std::max(BackoffUntil, RetryAfterUntil) || deadline <= now)
	{
		return false;
	}
	BeginAttempt(now, deadline, true);
	return true;
}

ConnectResult ProducerEndpoint::Connect(TimePoint now, TimePoint deadline)
{
	std::lock_guard lock(Mutex);
	switch (State)
	{
	case ConnectionState::Connected:
		return ConnectResult::Connected;
	case ConnectionState::Connecting:
	case ConnectionState::Handshaking:
	case ConnectionState::Closing:
		return ConnectResult::Busy;
	case ConnectionState::Stopped:
		return ConnectResult::Refused;
	case ConnectionState::Idle:
		break;
	}
	if (SessionFailure || now < RetryAfterUntil || deadline <= now)
	{
		return ConnectResult::Refused;
	}
	Rejection.reset();
	BeginAttempt(now, deadline, false);
	return ConnectResult::Started;
}

bool ProducerEndpoint::CancelConnect()
{
	std::lock_guard lock(Mutex);
	ResetBackoff();
	if (IsAttempting())
	{
		Log(LogLevel::Info, "connection attempt cancelled");
		Close(DisconnectReason::Cancelled);
	}
	return State != ConnectionState::Closing;
}

void ProducerEndpoint::Disconnect()
{
	std::lock_guard lock(Mutex);
	ResetBackoff();
	Quit(DisconnectReason::Cancelled);
}

void ProducerEndpoint::OnConnected(std::string_view token)
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
	// Attempts start only without a failure, and none is recorded before this.
	const std::optional<ProducerConnectionStart> connection = Session.BeginConnection();
	assert(connection);
	ConnectionGeneration = connection->Generation;
	Decoder.Reset();
	State = ConnectionState::Handshaking;

	HelloParameters hello = detail::CommonHello("emitter", Config, token);
	hello.ProducerSessionId = SessionId;
	if (!detail::QueueHello(hello, Actions))
	{
		Close(DisconnectReason::ProtocolError);
	}
}

void ProducerEndpoint::OnBytes(const std::uint8_t* data, std::size_t size)
{
	std::lock_guard lock(Mutex);
	if (!IsOpen())
	{
		return;
	}
	const bool framed = detail::HandleFrames(Decoder, data, size,
											 [this](const std::vector<std::uint8_t>& frame)
											 {
												 HandleFrame(frame);
												 return IsOpen();
											 });
	if (!framed)
	{
		Log(LogLevel::Warning, "invalid frame header");
		Close(DisconnectReason::ProtocolError);
	}
}

void ProducerEndpoint::OnDisconnected(DisconnectReason reason, TimePoint now)
{
	std::lock_guard lock(Mutex);
	if (IsOpen())
	{
		Log(LogLevel::Warning, "connection to " + Address(Config) + " lost");
		EndConnection(reason);
	}
	else if (State != ConnectionState::Connecting && State != ConnectionState::Closing)
	{
		return;
	}
	// The socket is gone, and a queued close would end the next attempt instead.
	Discard<SendAction, CloseAction>(Actions);
	if (PendingRetryAfter)
	{
		RetryAfterUntil = std::max(RetryAfterUntil, now + *PendingRetryAfter);
		PendingRetryAfter.reset();
	}
	if (BackoffOnFailure)
	{
		BackoffUntil = Reconnect.NextAttempt(now);
		BackoffOnFailure = false;
	}
	State = ConnectionState::Idle;
}

void ProducerEndpoint::OnTick(TimePoint now)
{
	std::lock_guard lock(Mutex);
	if (IsAttempting() && now >= AttemptDeadline)
	{
		Log(LogLevel::Warning, "handshake with " + Address(Config) + " timed out");
		Close(DisconnectReason::HandshakeTimeout);
	}
}

void ProducerEndpoint::Stop()
{
	std::lock_guard lock(Mutex);
	if (State == ConnectionState::Stopped)
	{
		return;
	}
	Quit(DisconnectReason::Stopped);
	State = ConnectionState::Stopped;
	Log(LogLevel::Info, "stopped");
}

std::vector<Action> ProducerEndpoint::TakeActions()
{
	std::lock_guard lock(Mutex);
	return std::exchange(Actions, {});
}

std::optional<TimePoint> ProducerEndpoint::NextWake() const
{
	std::lock_guard lock(Mutex);
	return IsAttempting() ? std::optional<TimePoint>(AttemptDeadline) : std::nullopt;
}

std::uint64_t ProducerEndpoint::NextTransactionId() const noexcept
{
	return Session.NextTransactionId();
}

ProducerResult ProducerEndpoint::Append(std::uint64_t transaction_id,
										std::vector<std::uint8_t> frame, std::size_t event_count,
										std::string layer_key)
{
	if (!IsCompleteFrame(frame))
	{
		return ProducerResult::InvalidArgument;
	}
	SharedByteBuffer payload = Share(std::move(frame));
	std::lock_guard lock(Mutex);
	// The session is Ready exactly while connected, so it refuses every other state.
	const ProducerResult result =
		Session.Append(ConnectionGeneration, transaction_id, std::move(payload), event_count,
					   std::move(layer_key));
	if (result == ProducerResult::Accepted)
	{
		SendUnsent();
	}
	return result;
}

bool ProducerEndpoint::QueueControl(std::vector<std::uint8_t> frame)
{
	if (!IsCompleteFrame(frame))
	{
		return false;
	}
	SharedByteBuffer payload = Share(std::move(frame));
	std::lock_guard lock(Mutex);
	if (State != ConnectionState::Connected)
	{
		return false;
	}
	Actions.push_back(SendAction{std::move(payload)});
	return true;
}

bool ProducerEndpoint::OutboxEmpty() const noexcept
{
	return Session.Empty();
}

std::uint64_t ProducerEndpoint::DrainAcknowledgedEventCount() noexcept
{
	return Session.DrainAcknowledgedEventCount();
}

std::optional<MirrorCheckpoint> ProducerEndpoint::AcknowledgedCheckpoint() const
{
	std::lock_guard lock(Mutex);
	if (SessionFailure || !Session.Empty())
	{
		return std::nullopt;
	}
	return Checkpoint;
}

std::optional<TransactionFailure> ProducerEndpoint::Failure() const
{
	std::lock_guard lock(Mutex);
	return SessionFailure;
}

std::optional<RecoveryArtifact> ProducerEndpoint::Artifact() const
{
	std::lock_guard lock(Mutex);
	if (!SessionFailure)
	{
		return std::nullopt;
	}
	return CaptureArtifact();
}

ProducerResult ProducerEndpoint::RepairRejected(std::vector<std::uint8_t> frame,
												std::size_t event_count, std::string layer_key)
{
	if (!IsCompleteFrame(frame))
	{
		return ProducerResult::InvalidArgument;
	}
	SharedByteBuffer payload = Share(std::move(frame));
	std::lock_guard lock(Mutex);
	const ProducerResult result =
		Session.RepairRejected(std::move(payload), event_count, std::move(layer_key));
	if (result == ProducerResult::Accepted)
	{
		Log(LogLevel::Info,
			"repaired transaction " + std::to_string(SessionFailure->TransactionId));
		SessionFailure.reset();
		RetryAfterUntil = {};
		ResetBackoff();
	}
	return result;
}

std::optional<RecoveryArtifact> ProducerEndpoint::AbandonRejectedSession(std::string session_id)
{
	if (!IsValidProducerSessionId(session_id))
	{
		return std::nullopt;
	}
	std::lock_guard lock(Mutex);
	if (!SessionFailure || session_id == SessionId)
	{
		return std::nullopt;
	}
	RecoveryArtifact artifact = CaptureArtifact();
	Log(LogLevel::Info, "abandoned producer session " + SessionId + " for " + session_id);
	Session.ResetSession();
	SessionId = std::move(session_id);
	SessionFailure.reset();
	RetryAfterUntil = {};
	ResetBackoff();
	return artifact;
}

ProducerStatus ProducerEndpoint::Status() const
{
	std::lock_guard lock(Mutex);
	ProducerStatus status;
	status.Connected = State == ConnectionState::Connected;
	status.Handshaking = IsAttempting();
	status.Closing = State == ConnectionState::Closing;
	status.Stopped = State == ConnectionState::Stopped;
	status.Rejection = Rejection;
	status.LayerModeActive = LayerModeActive;
	status.Metadata = Metadata;
	status.SessionId = SessionId;
	status.PendingTransactions = Session.PendingTransactionCount();
	status.PendingEvents = Session.PendingEventCount();
	status.AcknowledgedTransactions = Session.AcknowledgedTransactionCount();
	status.AcknowledgedEvents = Session.AcknowledgedEventCount();
	status.NextTransactionId = Session.NextTransactionId();
	status.Failure = SessionFailure;
	status.RetryAfter = RetryAfterUntil;
	return status;
}

bool ProducerEndpoint::IsAttempting() const noexcept
{
	return State == ConnectionState::Connecting || State == ConnectionState::Handshaking;
}

bool ProducerEndpoint::IsOpen() const noexcept
{
	return State == ConnectionState::Handshaking || State == ConnectionState::Connected;
}

void ProducerEndpoint::BeginAttempt(TimePoint now, TimePoint deadline, bool backoff_on_failure)
{
	State = ConnectionState::Connecting;
	AttemptDeadline = std::min(deadline, now + Config.HandshakeTimeout);
	BackoffOnFailure = backoff_on_failure;
	Log(LogLevel::Info, "connecting to " + Address(Config));
	Actions.push_back(ConnectAction{Config.Host, Config.Port, AttemptDeadline});
	Actions.push_back(WakeAction{AttemptDeadline});
}

void ProducerEndpoint::ResetBackoff() noexcept
{
	Reconnect.Reset();
	BackoffUntil = {};
	BackoffOnFailure = false;
}

void ProducerEndpoint::Quit(DisconnectReason reason)
{
	if (State == ConnectionState::Connected)
	{
		Actions.push_back(SendAction{BuildQuitFrame()});
	}
	if (State == ConnectionState::Connecting || IsOpen())
	{
		Close(reason);
	}
}

void ProducerEndpoint::Close(DisconnectReason reason)
{
	EndConnection(reason);
	// An attempt the host has not taken ends here, so no close can follow it.
	if (State == ConnectionState::Connecting && Discard<ConnectAction, WakeAction>(Actions))
	{
		BackoffOnFailure = false;
		State = ConnectionState::Idle;
		return;
	}
	Actions.push_back(CloseAction{reason});
	State = ConnectionState::Closing;
}

void ProducerEndpoint::EndConnection(DisconnectReason reason)
{
	if (State == ConnectionState::Connected)
	{
		Notify(Disconnected{reason});
	}
	if (IsOpen())
	{
		[[maybe_unused]] const ProducerResult ended = Session.Disconnect(ConnectionGeneration);
		assert(ended == ProducerResult::Accepted);
	}
}

void ProducerEndpoint::HandleFrame(const std::vector<std::uint8_t>& frame)
{
	EnvelopeView envelope;
	const ProtocolResult decoded = DecodeEnvelope(frame.data(), frame.size(), envelope);
	if (decoded != ProtocolResult::Success)
	{
		Log(LogLevel::Error, detail::DescribeDecodeFailure(decoded));
		Close(DisconnectReason::ProtocolError);
		return;
	}
	if (State == ConnectionState::Handshaking)
	{
		HandleHandshake(envelope);
	}
	else
	{
		HandleMessage(envelope);
	}
}

void ProducerEndpoint::HandleHandshake(EnvelopeView envelope)
{
	const detail::HandshakeOutcome outcome = detail::ClassifyHandshake(envelope);
	if (outcome.Accepted)
	{
		AcceptHello(*outcome.Accepted);
	}
	else if (outcome.Rejection)
	{
		Reject(*outcome.Rejection);
	}
	else
	{
		Log(LogLevel::Error, "unexpected handshake response");
		Close(DisconnectReason::ProtocolError);
	}
}

void ProducerEndpoint::AcceptHello(const OpenUSDConnect::HelloOk& hello)
{
	ServerInstance = Text(hello.server_instance());
	Checkpoint.reset();
	if (hello.layer_mode() != Config.LayerMode)
	{
		Reject({false, OpenUSDConnect::HelloRejectionCode::LayerModeMismatch,
				std::string("server negotiated ")
					.append(LayerModeName(hello.layer_mode()))
					.append(" instead of ")
					.append(LayerModeName(Config.LayerMode))});
		return;
	}
	LayerModeActive = hello.layer_mode();
	const std::uint64_t committed_through = hello.committed_through();
	const ProducerResult accepted = Session.AcceptHello(ConnectionGeneration, committed_through);
	if (accepted != ProducerResult::Accepted)
	{
		Fail({committed_through, kUnexpectedId,
			  HighwaterFailureReason(accepted, committed_through)});
		return;
	}
	detail::NotifyHelloFields(hello, Metadata, Notifications, Actions);
	Publish();
}

void ProducerEndpoint::Reject(HandshakeRejected rejection)
{
	Log(LogLevel::Error, detail::DescribeRejection(rejection));
	Notify(rejection);
	Rejection = std::move(rejection);
	Close(DisconnectReason::HandshakeRejected);
}

void ProducerEndpoint::Publish()
{
	State = ConnectionState::Connected;
	ResetBackoff();
	Notify(Connected{});
	const std::size_t replayed = SendUnsent();
	Log(LogLevel::Info, "connected to " + Address(Config) + " (session=" + SessionId +
							", pending=" + std::to_string(replayed) + ")");
}

std::size_t ProducerEndpoint::SendUnsent()
{
	std::size_t sent = 0;
	ProducerSessionEntry entry{};
	while (Session.ClaimNextUnsent(ConnectionGeneration, entry) == ProducerResult::Accepted)
	{
		Actions.emplace_back(SendAction{std::move(entry.Payload)});
		++sent;
	}
	return sent;
}

void ProducerEndpoint::HandleMessage(EnvelopeView envelope)
{
	// Other messages, such as playback replies to control frames, need no action.
	const ControlMessageView message(envelope);
	if (message.Kind() == ControlMessageKind::TransactionResult)
	{
		AcceptResult(*message.TransactionResult());
	}
	else if (message.Kind() == ControlMessageKind::RateLimited)
	{
		AcceptRateLimit(*message.RateLimit());
	}
}

void ProducerEndpoint::AcceptResult(const OpenUSDConnect::TransactionResult& result)
{
	const std::uint64_t transaction_id = result.txn_id();
	if (result.status() == OpenUSDConnect::TransactionStatus::Acknowledged)
	{
		const ProducerResult accepted =
			Session.AcknowledgeThrough(ConnectionGeneration, transaction_id);
		if (accepted != ProducerResult::Accepted)
		{
			Fail({transaction_id, kUnexpectedId, HighwaterFailureReason(accepted, transaction_id)});
			return;
		}
		const OpenUSDConnect::TransactionCheckpoint* checkpoint = result.checkpoint();
		if (checkpoint && !ServerInstance.empty())
		{
			Checkpoint =
				MirrorCheckpoint{ServerInstance, checkpoint->epoch(), checkpoint->head_seq()};
		}
		else
		{
			Checkpoint.reset();
		}
		return;
	}
	const auto code = static_cast<std::uint8_t>(result.rejection_code());
	TransactionFailure failure{transaction_id, code, Text(result.reason()),
							   result.expected_txn_id()};
	const ProducerResult rejected =
		Session.Reject(ConnectionGeneration, transaction_id, RejectionDisposition(code));
	assert(rejected == ProducerResult::Accepted || rejected == ProducerResult::TransactionMissing);
	if (rejected == ProducerResult::TransactionMissing)
	{
		failure = {transaction_id, kUnexpectedId,
				   "server rejected unknown transaction " + std::to_string(transaction_id)};
	}
	Fail(std::move(failure));
}

void ProducerEndpoint::AcceptRateLimit(const OpenUSDConnect::RateLimited& limited)
{
	PendingRetryAfter = RetryAfter(limited.retry_after());
	const auto wait = std::chrono::duration_cast<std::chrono::milliseconds>(*PendingRetryAfter);
	Log(LogLevel::Warning,
		"rate limited by the server; retrying after " + std::to_string(wait.count()) + " ms");
	Close(DisconnectReason::RateLimited);
}

void ProducerEndpoint::Fail(TransactionFailure failure)
{
	assert(Session.RecoveryRequired());
	Log(LogLevel::Error, failure.Describe());
	SessionFailure = std::move(failure);
	Close(DisconnectReason::RecoveryRequired);
}

std::string ProducerEndpoint::HighwaterFailureReason(ProducerResult result,
													 std::uint64_t transaction_id) const
{
	assert(result == ProducerResult::HighwaterAhead ||
		   result == ProducerResult::HighwaterRegressed);
	if (result == ProducerResult::HighwaterAhead)
	{
		return "server producer highwater " + std::to_string(transaction_id) +
			   " is ahead of local transaction " + std::to_string(Session.NextTransactionId() - 1);
	}
	return "server producer highwater regressed from " +
		   std::to_string(Session.LastAcknowledgedTransactionId()) + " to " +
		   std::to_string(transaction_id);
}

RecoveryArtifact ProducerEndpoint::CaptureArtifact() const
{
	return {SessionId, *SessionFailure, Session.Entries()};
}

void ProducerEndpoint::Notify(Notification notification)
{
	Notifications.Push(std::move(notification));
}

void ProducerEndpoint::Log(LogLevel level, std::string message)
{
	Actions.push_back(LogAction{level, std::move(message)});
}

} // namespace openusdconnect::client
