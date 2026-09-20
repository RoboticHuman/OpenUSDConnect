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

// Tracks which replay sequence domain has actually been applied by a receiver.
// Callers provide synchronization when connection and consumer threads overlap.
class ReceiverReplayIdentity final
{
public:
	[[nodiscard]] ReplayPrefixClaim BeginConnection()
	{
		PendingIdentity.reset();
		ClaimedIdentity = AppliedIdentity;
		const bool IncludeClaim = HelloSent;
		HelloSent = true;
		ClaimIncluded = IncludeClaim;
		if (!IncludeClaim)
		{
			return std::nullopt;
		}

		if (AppliedIdentity)
		{
			return ReplayPrefixIdentity::Known(AppliedIdentity->ServerInstance,
											   AppliedIdentity->Epoch);
		}
		return ReplayPrefixIdentity::Unknown();
	}

	void AcceptHello(std::int32_t sync_from, bool replay_identity_supported,
					 std::string_view server_instance, std::optional<std::uint64_t> epoch)
	{
		ConnectionIdentity.reset();
		if (replay_identity_supported && !server_instance.empty() && epoch)
		{
			ConnectionIdentity = ReplayIdentity{std::string(server_instance), *epoch};
		}
		ConnectionPrefixProven =
			sync_from == 1 || (ClaimIncluded && ClaimedIdentity && ConnectionIdentity &&
							   *ClaimedIdentity == *ConnectionIdentity);
	}

	void AcceptResync() noexcept
	{
		ConnectionPrefixProven = true;
		PendingIdentity.reset();
	}

	void AcceptReplayComplete(std::uint64_t epoch)
	{
		if (ConnectionPrefixProven && ConnectionIdentity)
		{
			PendingIdentity = ReplayIdentity{ConnectionIdentity->ServerInstance, epoch};
		}
		else
		{
			PendingIdentity.reset();
		}
	}

	void MarkReplayApplied()
	{
		AppliedIdentity = PendingIdentity;
		PendingIdentity.reset();
	}

	[[nodiscard]] const std::optional<ReplayIdentity>& Applied() const noexcept
	{
		return AppliedIdentity;
	}

	[[nodiscard]] const std::optional<ReplayIdentity>& Pending() const noexcept
	{
		return PendingIdentity;
	}

	[[nodiscard]] bool IsConnectionPrefixProven() const noexcept
	{
		return ConnectionPrefixProven;
	}

private:
	bool HelloSent = false;
	bool ClaimIncluded = false;
	bool ConnectionPrefixProven = false;
	std::optional<ReplayIdentity> ClaimedIdentity;
	std::optional<ReplayIdentity> ConnectionIdentity;
	std::optional<ReplayIdentity> PendingIdentity;
	std::optional<ReplayIdentity> AppliedIdentity;
};

} // namespace openusdconnect::client
