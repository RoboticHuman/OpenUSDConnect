#pragma once

#include "openusdconnect/client/engine/actions.h"
#include "openusdconnect/client/engine/notification.h"
#include "openusdconnect/client/producer_recovery.h"
#include "openusdconnect/client/producer_session.h"
#include "openusdconnect/client/protocol_codec.h"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace openusdconnect::client
{

struct ProducerConfig final
{
	std::string Host;
	std::uint16_t Port = 0;
	std::string ClientId;
	std::string Origin;
	std::string Department;
	OpenUSDConnect::LayerMode LayerMode = OpenUSDConnect::LayerMode::Managed;
	// The first producer session; AbandonRejectedSession starts each later one.
	std::string SessionId;
	// Bounds an attempt from its ConnectAction to the handshake result. The host
	// reports a write that makes no progress for this long as TransportError.
	std::chrono::milliseconds HandshakeTimeout{10'000};
	std::size_t MaxPendingTransactions = 10'000;
	std::chrono::milliseconds ReconnectBaseDelay{1'000};
	std::chrono::milliseconds ReconnectMaxDelay{8'000};
};

// The replay position at which the server made the latest acknowledgement durable.
struct MirrorCheckpoint final
{
	std::string ServerInstance;
	std::uint64_t Epoch = 0;
	std::int32_t HeadSequence = 0;
};

// The unacknowledged outbox a failure quarantined, with the frames as appended.
struct RecoveryArtifact final
{
	std::string SessionId;
	TransactionFailure Failure;
	std::vector<ProducerSessionEntry> Transactions;
};

struct ProducerStatus final
{
	// The handshake completed; appended transactions are sent.
	bool Connected = false;
	// An attempt is connecting or awaiting the handshake response.
	bool Handshaking = false;
	// The endpoint closed a connection or attempt that the host has not yet
	// reported gone; Connect is Busy until it does.
	bool Closing = false;
	bool Stopped = false;
	std::optional<HandshakeRejected> Rejection;
	OpenUSDConnect::LayerMode LayerModeActive = OpenUSDConnect::LayerMode::Managed;
	StageMetadata Metadata;
	std::string SessionId;
	std::size_t PendingTransactions = 0;
	std::size_t PendingEvents = 0;
	std::uint64_t AcknowledgedTransactions = 0;
	std::uint64_t AcknowledgedEvents = 0;
	std::uint64_t NextTransactionId = 1;
	// Set exactly while recovery is required.
	std::optional<TransactionFailure> Failure;
	// Attempts are refused before this time, which the server's RateLimited set.
	TimePoint RetryAfter;
};

enum class ConnectResult : std::uint8_t
{
	Started,
	Connected,
	// An attempt or a close is in flight; try again once it ends.
	Busy,
	// Stopped, recovery required, rate limited, or no time left.
	Refused,
};

// Sans-IO producer: the host applies the returned actions in order, on the
// thread that reports its socket events, and reports time. It connects only
// when asked. Thread-safe; never blocks or calls into the host.
class ProducerEndpoint final
{
public:
	ProducerEndpoint(ProducerConfig config, NotificationQueue& notifications);
	ProducerEndpoint(const ProducerEndpoint&) = delete;
	ProducerEndpoint& operator=(const ProducerEndpoint&) = delete;

	[[nodiscard]] static bool IsValidConfiguration(const ProducerConfig& config) noexcept;

	[[nodiscard]] const ProducerConfig& Configuration() const noexcept;

	// One attempt for a retry loop. Refused while a connection, attempt, or close
	// exists, after a handshake rejection, while recovery is required, inside the
	// rate-limit window, and inside the backoff that a failed request starts.
	[[nodiscard]] bool RequestConnect(TimePoint now, TimePoint deadline);
	// One attempt regardless of backoff; it clears a handshake rejection.
	[[nodiscard]] ConnectResult Connect(TimePoint now, TimePoint deadline);
	// Abandons an attempt in flight and resets the backoff, leaving a connection
	// intact. Returns whether nothing remains for the host to close.
	bool CancelConnect();
	// Cancels any attempt, then says Quit and closes the connection. The outbox
	// is kept for the next connection.
	void Disconnect();

	// Host I/O. Reports that do not match the current connection are ignored.
	// Read the token just before calling, so a newly issued one is presented.
	void OnConnected(std::string_view token);
	void OnBytes(const std::uint8_t* data, std::size_t size);
	void OnDisconnected(DisconnectReason reason, TimePoint now);
	void OnTick(TimePoint now);
	// Like Disconnect, and refuses every later attempt.
	void Stop();
	[[nodiscard]] std::vector<Action> TakeActions();
	// The token the latest accepted Hello issued, once.
	[[nodiscard]] std::optional<std::string> TakeIssuedToken();
	[[nodiscard]] std::optional<TimePoint> NextWake() const;

	// Frames are complete and length-prefixed. Append's frame must encode
	// NextTransactionId(), so a host submitting from several threads holds one
	// lock from reading the id through Append.
	[[nodiscard]] std::uint64_t NextTransactionId() const noexcept;
	[[nodiscard]] ProducerResult Append(std::uint64_t transaction_id,
										std::vector<std::uint8_t> frame, std::size_t event_count,
										std::string layer_key);
	// Sent in order with transactions while connected, and never replayed.
	[[nodiscard]] bool QueueControl(std::vector<std::uint8_t> frame);
	[[nodiscard]] bool OutboxEmpty() const noexcept;
	[[nodiscard]] std::uint64_t DrainAcknowledgedEventCount() noexcept;
	// Set while every appended transaction is acknowledged, if the latest
	// acknowledgement carried a checkpoint.
	[[nodiscard]] std::optional<MirrorCheckpoint> AcknowledgedCheckpoint() const;

	[[nodiscard]] std::optional<TransactionFailure> Failure() const;
	[[nodiscard]] std::optional<RecoveryArtifact> Artifact() const;
	// Replaces a recoverable rejected transaction with a frame that encodes
	// Failure()->TransactionId; later transactions follow it unchanged.
	[[nodiscard]] ProducerResult RepairRejected(std::vector<std::uint8_t> frame,
												std::size_t event_count, std::string layer_key);
	// Discards a failed session and continues as session_id from transaction 1.
	// Nullopt without a failure, or for an invalid or unchanged session_id.
	[[nodiscard]] std::optional<RecoveryArtifact> AbandonRejectedSession(std::string session_id);

	[[nodiscard]] ProducerStatus Status() const;

private:
	enum class ConnectionState : std::uint8_t
	{
		Idle,
		Connecting,
		Handshaking,
		Connected,
		Closing,
		Stopped,
	};

	[[nodiscard]] bool IsAttempting() const noexcept;
	[[nodiscard]] bool IsOpen() const noexcept;
	void BeginAttempt(TimePoint now, TimePoint deadline, bool backoff_on_failure);
	void ResetBackoff() noexcept;
	void Close(DisconnectReason reason);
	void EndConnection(DisconnectReason reason);

	void HandleFrame(const std::vector<std::uint8_t>& frame);
	void HandleHandshake(EnvelopeView envelope);
	void AcceptHello(const OpenUSDConnect::HelloOk& hello);
	void Reject(HandshakeRejected rejection);
	void Publish();
	std::size_t SendUnsent();
	void HandleMessage(EnvelopeView envelope);
	void AcceptResult(const OpenUSDConnect::TransactionResult& result);
	void AcceptRateLimit(const OpenUSDConnect::RateLimited& limited);
	void Fail(TransactionFailure failure);
	[[nodiscard]] std::string HighwaterFailureReason(ProducerResult result,
													 std::uint64_t transaction_id) const;
	[[nodiscard]] RecoveryArtifact CaptureArtifact() const;

	void Notify(Notification notification);
	void Log(LogLevel level, std::string message);

	mutable std::mutex Mutex;
	const ProducerConfig Config;
	// A leaf lock, so pushing while Mutex is held keeps notifications ordered.
	NotificationQueue& Notifications;
	ProducerSession Session;
	FrameDecoder Decoder;
	ReconnectPolicy Reconnect;
	std::vector<Action> Actions;
	ConnectionState State = ConnectionState::Idle;
	// The session generation of the attempt or connection in flight.
	std::uint64_t ConnectionGeneration = 0;
	TimePoint AttemptDeadline;
	bool BackoffOnFailure = false;
	TimePoint BackoffUntil;
	TimePoint RetryAfterUntil;
	// Applied once the host reports the close that RateLimited requested.
	std::optional<std::chrono::steady_clock::duration> PendingRetryAfter;
	std::optional<HandshakeRejected> Rejection;
	std::optional<std::string> IssuedToken;
	std::optional<TransactionFailure> SessionFailure;
	std::string SessionId;
	std::string ServerInstance;
	std::optional<MirrorCheckpoint> Checkpoint;
	OpenUSDConnect::LayerMode LayerModeActive = OpenUSDConnect::LayerMode::Managed;
	StageMetadata Metadata;
};

} // namespace openusdconnect::client
