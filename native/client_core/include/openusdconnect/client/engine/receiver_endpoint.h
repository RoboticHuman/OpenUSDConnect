#pragma once

#include "openusdconnect/client/engine/actions.h"
#include "openusdconnect/client/engine/notification.h"
#include "openusdconnect/client/protocol_codec.h"
#include "openusdconnect/client/receiver_session.h"

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

struct ReceiverConfig final
{
	std::string Host;
	std::uint16_t Port = 0;
	// Optional: a server that requires tokens rejects a receiver without one.
	std::string ClientId;
	std::string Origin;
	std::string Department;
	bool LayeredReplay = true;
	OpenUSDConnect::LayerMode LayerMode = OpenUSDConnect::LayerMode::Managed;
	// Above one, the consumer already holds the prefix, for example from a snapshot.
	std::int32_t SyncFrom = 1;
	std::size_t MaxQueue = 50'000;
	// Bounds a connect and a write, and how long a host read waits before OnReadTimeout.
	std::chrono::milliseconds SocketTimeout{30'000};
	std::uint32_t MaxConsecutiveTimeouts = 10;
	bool Reconnect = true;
	std::chrono::milliseconds ReconnectBaseDelay{1'000};
	std::chrono::milliseconds ReconnectMaxDelay{30'000};
};

struct ReceiverStatus final
{
	bool Connected = false;
	// The consumer applied the replay through the head the server advertised.
	bool Synchronized = false;
	// No further connection attempt will be made.
	bool Stopped = false;
	std::int32_t ReplayHeadSequence = 0;
	std::uint64_t ReplayEpoch = 0;
	// The server whose replay the consumer applied; empty when unproven.
	std::string ServerInstance;
	bool LayeredReplayActive = false;
	OpenUSDConnect::LayerMode LayerModeActive = OpenUSDConnect::LayerMode::Managed;
	std::optional<HandshakeRejected> Rejection;
	StageMetadata Metadata;
	std::size_t QueuedFrames = 0;
	std::int32_t LastSequence = 0;
	std::int32_t LastAppliedSequence = 0;
};

// Sans-IO receiver: the host applies the returned actions and reports socket
// events and time. Thread-safe; never blocks or calls into the host.
class ReceiverEndpoint final
{
public:
	ReceiverEndpoint(ReceiverConfig config, NotificationQueue& notifications);
	ReceiverEndpoint(const ReceiverEndpoint&) = delete;
	ReceiverEndpoint& operator=(const ReceiverEndpoint&) = delete;

	[[nodiscard]] static bool IsValidConfiguration(const ReceiverConfig& config) noexcept;

	[[nodiscard]] const ReceiverConfig& Configuration() const noexcept;
	// Applies when the current session ends; a stopped endpoint stays stopped.
	void SetReconnect(bool enabled);

	// Host I/O. Reports that do not match the current connection are ignored.
	[[nodiscard]] bool Start(TimePoint now);
	// Read the token just before calling, so a newly issued one is presented.
	void OnConnected(std::string_view token);
	void OnBytes(const std::uint8_t* data, std::size_t size);
	// A read waited SocketTimeout without receiving a byte.
	void OnReadTimeout();
	void OnDisconnected(DisconnectReason reason, TimePoint now);
	void OnTick(TimePoint now);
	void Stop();
	[[nodiscard]] std::vector<Action> TakeActions();
	// The token the latest accepted Hello issued, once.
	[[nodiscard]] std::optional<std::string> TakeIssuedToken();
	[[nodiscard]] std::optional<TimePoint> NextWake() const;

	// Stage-owning consumer. Read Generation before draining; report progress
	// only once every drained frame applied, and after a failure call
	// RequestReplayFrom with the applied cursor instead.
	[[nodiscard]] std::vector<std::vector<std::uint8_t>>
	DrainFrames(std::optional<std::size_t> max_frames = std::nullopt);
	[[nodiscard]] std::uint64_t Generation() const noexcept;
	[[nodiscard]] bool MarkAppliedThrough(std::uint64_t generation, std::int32_t sequence);
	void ResetAppliedProgress() noexcept;
	[[nodiscard]] bool MarkReplayApplied();
	// Discards the queued frames and reconnects to replay from sequence.
	[[nodiscard]] bool RequestReplayFrom(std::int32_t sequence);
	[[nodiscard]] std::uint64_t FreezeMarker() const noexcept;
	[[nodiscard]] bool DrainedThrough(std::uint64_t marker) const noexcept;

	[[nodiscard]] ReceiverStatus Status() const;

private:
	enum class ConnectionState : std::uint8_t
	{
		Idle,
		Connecting,
		Handshaking,
		Connected,
		Closing,
		Backoff,
		DrainWait,
		Stopped,
	};

	[[nodiscard]] bool IsOpen() const noexcept;
	void BeginAttempt(TimePoint now);
	void ScheduleNextAttempt(TimePoint now);
	void PollDrain(TimePoint now);
	void Close(DisconnectReason reason);
	void EndConnection(DisconnectReason reason);
	void RequestReplay(std::int32_t sequence, DisconnectReason reason);

	void HandleFrame(std::vector<std::uint8_t> frame);
	void HandleHandshake(EnvelopeView envelope);
	void AcceptHello(const OpenUSDConnect::HelloOk& hello);
	void Reject(HandshakeRejected rejection);
	void HandleMessage(EnvelopeView envelope, std::vector<std::uint8_t>& frame);
	void AcceptReplayComplete(const OpenUSDConnect::ReplayComplete& complete);
	void AcceptFrame(ReceiverMessageKind kind, std::int32_t sequence,
					 std::vector<std::uint8_t> frame);
	void ReplayAfterGap(const std::string& description);

	void Notify(Notification notification);
	void Log(LogLevel level, std::string message);

	mutable std::mutex Mutex;
	const ReceiverConfig Config;
	// A leaf lock, so pushing while Mutex is held keeps notifications ordered.
	NotificationQueue& Notifications;
	ReceiverInbox Inbox;
	ReceiverReplayIdentity Identity;
	FrameDecoder Decoder;
	ReconnectPolicy Reconnect;
	std::vector<Action> Actions;
	ConnectionState State = ConnectionState::Idle;
	std::uint64_t ConnectionGeneration = 0;
	std::int32_t ConnectionSyncFrom = 0;
	std::uint32_t ConsecutiveTimeouts = 0;
	TimePoint WakeTime;
	TimePoint DrainDeadline;
	std::optional<HandshakeRejected> Rejection;
	std::optional<std::string> IssuedToken;
	bool LayeredReplayActive = false;
	OpenUSDConnect::LayerMode LayerModeActive = OpenUSDConnect::LayerMode::Managed;
	StageMetadata Metadata;
};

} // namespace openusdconnect::client
