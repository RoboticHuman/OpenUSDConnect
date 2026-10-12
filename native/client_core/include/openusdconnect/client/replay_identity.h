#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <utility>

namespace openusdconnect::client
{

struct ReplayIdentity final
{
	std::string ServerInstance;
	std::uint64_t Epoch = 0;

	[[nodiscard]] bool operator==(const ReplayIdentity& other) const noexcept
	{
		return ServerInstance == other.ServerInstance && Epoch == other.Epoch;
	}
};

// Presence of this value means the Hello explicitly carries replay-prefix
// information. Empty fields represent an unknown prefix and force a safe reset.
class ReplayPrefixIdentity final
{
public:
	[[nodiscard]] static ReplayPrefixIdentity Unknown()
	{
		return ReplayPrefixIdentity();
	}

	[[nodiscard]] static ReplayPrefixIdentity Known(std::string server_instance,
													std::uint64_t epoch)
	{
		if (server_instance.empty())
		{
			return Unknown();
		}
		ReplayPrefixIdentity identity;
		identity.Identity = ReplayIdentity{std::move(server_instance), epoch};
		return identity;
	}

	[[nodiscard]] bool IsKnown() const noexcept
	{
		return Identity.has_value();
	}

	[[nodiscard]] std::string_view ServerInstance() const noexcept
	{
		return Identity ? std::string_view(Identity->ServerInstance) : std::string_view();
	}

	[[nodiscard]] std::optional<std::uint64_t> Epoch() const noexcept
	{
		return Identity ? std::optional<std::uint64_t>(Identity->Epoch) : std::nullopt;
	}

private:
	std::optional<ReplayIdentity> Identity;
};

using ReplayPrefixClaim = std::optional<ReplayPrefixIdentity>;

// Tracks the replay sequence domain of the prefix a receiver holds. The received
// identity covers the applied frames plus the retained queue and is the next
// Hello's claim; the applied identity is published once a replay has applied.
// Callers provide synchronization when connection and consumer threads overlap.
class ReceiverReplayIdentity final
{
public:
	// The first Hello never claims, so an externally supplied cursor keeps its
	// contract without being taken as proof of its prefix.
	[[nodiscard]] ReplayPrefixClaim BeginConnection()
	{
		PendingIdentity.reset();
		ClaimedIdentity = ReceivedIdentity;
		ClaimIncluded = HelloSent;
		HelloSent = true;
		if (!ClaimIncluded)
		{
			return std::nullopt;
		}
		if (ClaimedIdentity)
		{
			return ReplayPrefixIdentity::Known(ClaimedIdentity->ServerInstance,
											   ClaimedIdentity->Epoch);
		}
		return ReplayPrefixIdentity::Unknown();
	}

	void AcceptHello(std::int32_t sync_from, bool replay_identity_supported,
					 std::string_view server_instance, std::optional<std::uint64_t> epoch)
	{
		ConnectionInstance =
			replay_identity_supported ? std::string(server_instance) : std::string();
		HandshakeIdentity.reset();
		if (!ConnectionInstance.empty() && epoch)
		{
			HandshakeIdentity = ReplayIdentity{ConnectionInstance, *epoch};
		}
		ConnectionPrefixProven = sync_from == 1 || (ClaimIncluded && HandshakeIdentity &&
													ClaimedIdentity == HandshakeIdentity);
		// A changed or unknown prefix keeps its identity until the server's Resync.
		if (!HandshakeIdentity)
		{
			ReceivedIdentity.reset();
		}
		else if (ConnectionPrefixProven)
		{
			ReceivedIdentity = HandshakeIdentity;
		}
	}

	void AcceptResync()
	{
		ReceivedIdentity = std::exchange(HandshakeIdentity, std::nullopt);
		ConnectionPrefixProven = true;
		PendingIdentity.reset();
		ResetRequiredValue = false;
	}

	void AcceptReplayComplete(std::uint64_t epoch)
	{
		HandshakeIdentity.reset();
		ReceivedIdentity.reset();
		if (ConnectionPrefixProven && !ConnectionInstance.empty())
		{
			ReceivedIdentity = ReplayIdentity{ConnectionInstance, epoch};
		}
		PendingIdentity = ReceivedIdentity;
	}

	void MarkReplayApplied()
	{
		AppliedIdentity = std::exchange(PendingIdentity, std::nullopt);
	}

	// A reset that is discarded or not yet applied separates the consumer's
	// prefix from the received frames, so only the applied replay names it.
	void RequestReplayFrom(std::int32_t sequence, bool reset_pending)
	{
		if (reset_pending)
		{
			ReceivedIdentity = AppliedIdentity;
		}
		HandshakeIdentity.reset();
		PendingIdentity.reset();
		ResetRequiredValue = sequence == 1;
	}

	// The server never resets a replay from sequence one, so the receiver must
	// queue the reset itself once the next Hello is accepted.
	[[nodiscard]] bool ResetRequired() const noexcept
	{
		return ResetRequiredValue;
	}

	[[nodiscard]] const std::optional<ReplayIdentity>& Received() const noexcept
	{
		return ReceivedIdentity;
	}

	[[nodiscard]] const std::optional<ReplayIdentity>& Pending() const noexcept
	{
		return PendingIdentity;
	}

	[[nodiscard]] const std::optional<ReplayIdentity>& Applied() const noexcept
	{
		return AppliedIdentity;
	}

	[[nodiscard]] bool IsConnectionPrefixProven() const noexcept
	{
		return ConnectionPrefixProven;
	}

private:
	bool HelloSent = false;
	bool ClaimIncluded = false;
	bool ConnectionPrefixProven = false;
	bool ResetRequiredValue = false;
	std::string ConnectionInstance;
	std::optional<ReplayIdentity> ClaimedIdentity;
	std::optional<ReplayIdentity> HandshakeIdentity;
	std::optional<ReplayIdentity> ReceivedIdentity;
	std::optional<ReplayIdentity> PendingIdentity;
	std::optional<ReplayIdentity> AppliedIdentity;
};

} // namespace openusdconnect::client
