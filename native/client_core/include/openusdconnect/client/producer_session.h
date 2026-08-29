#pragma once

#include "openusdconnect/client/detail/ordered_outbox_storage.h"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace openusdconnect::client
{

enum class ProducerPhase : std::uint8_t
{
	Disconnected,
	AwaitingHello,
	Ready,
	RecoveryRequired,
};

enum class ProducerResult : std::uint8_t
{
	Accepted,
	StaleGeneration,
	InvalidPhase,
	RecoveryRequired,
	HighwaterAhead,
	HighwaterRegressed,
	OutboxFull,
	SequenceMismatch,
	NoPendingTransaction,
	TransactionMissing,
	RecoveryNotRecoverable,
	InvalidArgument,
};

enum class ProducerRecoveryDisposition : std::uint8_t
{
	None,
	RecoverableConflict,
	InvalidOperation,
	SessionFatal,
};

struct ProducerConnectionStart final
{
	std::uint64_t Generation;
};

template <typename Payload>
class OrderedProducerSession final
{
public:
	using Entry = detail::OrderedOutboxEntry<Payload>;

	explicit OrderedProducerSession(std::size_t capacity)
		: Outbox(capacity)
	{
	}

	[[nodiscard]] static bool IsValidConfiguration(std::size_t capacity) noexcept
	{
		return capacity != 0;
	}

	[[nodiscard]] std::optional<ProducerConnectionStart> BeginConnection()
	{
		std::lock_guard lock(Mutex);
		if (PhaseValue == ProducerPhase::RecoveryRequired)
		{
			return std::nullopt;
		}
		++GenerationValue;
		Outbox.MarkAllUnsent();
		PhaseValue = ProducerPhase::AwaitingHello;
		return ProducerConnectionStart{GenerationValue};
	}

	ProducerResult AcceptHello(std::uint64_t generation, std::uint64_t committed_through)
	{
		std::lock_guard lock(Mutex);
		const ProducerResult active = RequirePhase(generation, ProducerPhase::AwaitingHello);
		if (active != ProducerResult::Accepted)
		{
			return active;
		}
		const ProducerResult highwater = ValidateHighwater(committed_through);
		if (highwater != ProducerResult::Accepted)
		{
			EnterRecovery(0, ProducerRecoveryDisposition::SessionFatal);
			return highwater;
		}
		Outbox.AcknowledgeThrough(committed_through);
		PhaseValue = ProducerPhase::Ready;
		return ProducerResult::Accepted;
	}

	ProducerResult Disconnect(std::uint64_t generation) noexcept
	{
		std::lock_guard lock(Mutex);
		if (generation != GenerationValue)
		{
			return ProducerResult::StaleGeneration;
		}
		Outbox.MarkAllUnsent();
		if (PhaseValue != ProducerPhase::RecoveryRequired)
		{
			PhaseValue = ProducerPhase::Disconnected;
		}
		return ProducerResult::Accepted;
	}

	ProducerResult Append(std::uint64_t generation, std::uint64_t id, Payload payload,
						  std::size_t events, std::string layer)
	{
		if (events == 0)
		{
			return ProducerResult::InvalidArgument;
		}
		std::lock_guard lock(Mutex);
		const ProducerResult active = RequirePhase(generation, ProducerPhase::Ready);
		if (active != ProducerResult::Accepted)
		{
			return active;
		}
		if (!Outbox.CanAppend())
		{
			return ProducerResult::OutboxFull;
		}
		if (id != Outbox.NextTransactionId())
		{
			return ProducerResult::SequenceMismatch;
		}
		Outbox.Append(id, std::move(payload), events, std::move(layer));
		return ProducerResult::Accepted;
	}

	ProducerResult ClaimNextUnsent(std::uint64_t generation, Entry& entry)
	{
		std::lock_guard lock(Mutex);
		const ProducerResult active = RequirePhase(generation, ProducerPhase::Ready);
		if (active != ProducerResult::Accepted)
		{
			return active;
		}
		return Outbox.ClaimNextUnsent(entry) ? ProducerResult::Accepted
											 : ProducerResult::NoPendingTransaction;
	}

	ProducerResult AcknowledgeThrough(std::uint64_t generation, std::uint64_t id)
	{
		std::lock_guard lock(Mutex);
		const ProducerResult active = RequirePhase(generation, ProducerPhase::Ready);
		if (active != ProducerResult::Accepted)
		{
			return active;
		}
		const ProducerResult highwater = ValidateHighwater(id);
		if (highwater != ProducerResult::Accepted)
		{
			EnterRecovery(0, ProducerRecoveryDisposition::SessionFatal);
			return highwater;
		}
		Outbox.AcknowledgeThrough(id);
		return ProducerResult::Accepted;
	}

	ProducerResult Reject(std::uint64_t generation, std::uint64_t id,
						  ProducerRecoveryDisposition disposition)
	{
		std::lock_guard lock(Mutex);
		const ProducerResult active = RequirePhase(generation, ProducerPhase::Ready);
		if (active != ProducerResult::Accepted)
		{
			return active;
		}
		if (!Outbox.Contains(id))
		{
			EnterRecovery(id, ProducerRecoveryDisposition::SessionFatal);
			return ProducerResult::TransactionMissing;
		}
		EnterRecovery(id, disposition);
		return ProducerResult::Accepted;
	}

	ProducerResult RepairRejected(Payload payload, std::size_t events, std::string layer)
	{
		if (events == 0)
		{
			return ProducerResult::InvalidArgument;
		}
		std::lock_guard lock(Mutex);
		if (PhaseValue != ProducerPhase::RecoveryRequired)
		{
			return ProducerResult::InvalidPhase;
		}
		if (RecoveryDispositionValue != ProducerRecoveryDisposition::RecoverableConflict)
		{
			return ProducerResult::RecoveryNotRecoverable;
		}
		Outbox.Replace(RejectedTransactionIdValue, std::move(payload), events, std::move(layer));
		ClearRecovery();
		PhaseValue = ProducerPhase::Disconnected;
		return ProducerResult::Accepted;
	}

	void ResetSession()
	{
		std::lock_guard lock(Mutex);
		++GenerationValue;
		Outbox.ResetSession();
		ClearRecovery();
		PhaseValue = ProducerPhase::Disconnected;
	}

	[[nodiscard]] ProducerPhase Phase() const noexcept
	{
		return Read(&Self::PhaseUnlocked);
	}
	[[nodiscard]] std::uint64_t Generation() const noexcept
	{
		return Read(&Self::GenerationUnlocked);
	}
	[[nodiscard]] bool CanAppend() const noexcept
	{
		return Read(&Self::CanAppendUnlocked);
	}
	[[nodiscard]] bool Empty() const noexcept
	{
		return Read(&Self::EmptyUnlocked);
	}
	[[nodiscard]] bool RecoveryRequired() const noexcept
	{
		return Read(&Self::RecoveryRequiredUnlocked);
	}
	[[nodiscard]] ProducerRecoveryDisposition RecoveryDisposition() const noexcept
	{
		return Read(&Self::RecoveryDispositionUnlocked);
	}
	[[nodiscard]] std::uint64_t RejectedTransactionId() const noexcept
	{
		return Read(&Self::RejectedTransactionIdUnlocked);
	}
	[[nodiscard]] bool Contains(std::uint64_t id) const noexcept
	{
		std::lock_guard lock(Mutex);
		return Outbox.Contains(id);
	}
	[[nodiscard]] std::size_t PendingTransactionCount() const noexcept
	{
		return Read(&Self::PendingTransactionCountUnlocked);
	}
	[[nodiscard]] std::size_t PendingEventCount() const noexcept
	{
		return Read(&Self::PendingEventCountUnlocked);
	}
	[[nodiscard]] std::uint64_t NextTransactionId() const noexcept
	{
		return Read(&Self::NextTransactionIdUnlocked);
	}
	[[nodiscard]] std::uint64_t AcknowledgedTransactionCount() const noexcept
	{
		return Read(&Self::AcknowledgedTransactionCountUnlocked);
	}
	[[nodiscard]] std::uint64_t AcknowledgedEventCount() const noexcept
	{
		return Read(&Self::AcknowledgedEventCountUnlocked);
	}
	[[nodiscard]] std::uint64_t SubmittedTransactionCount() const noexcept
	{
		return Read(&Self::SubmittedTransactionCountUnlocked);
	}
	[[nodiscard]] std::uint64_t LastAcknowledgedTransactionId() const noexcept
	{
		return Read(&Self::LastAcknowledgedTransactionIdUnlocked);
	}
	[[nodiscard]] std::uint64_t DrainAcknowledgedEventCount() noexcept
	{
		std::lock_guard lock(Mutex);
		return Outbox.DrainAcknowledgedEventCount();
	}
	[[nodiscard]] std::vector<Entry> Entries() const
	{
		std::lock_guard lock(Mutex);
		return Outbox.Entries();
	}

private:
	using Self = OrderedProducerSession<Payload>;

	template <typename Result>
	[[nodiscard]] Result Read(Result (Self::*getter)() const noexcept) const noexcept
	{
		std::lock_guard lock(Mutex);
		return (this->*getter)();
	}

	[[nodiscard]] ProducerResult RequirePhase(std::uint64_t generation,
											  ProducerPhase required) const noexcept
	{
		if (generation != GenerationValue)
		{
			return ProducerResult::StaleGeneration;
		}
		if (PhaseValue == ProducerPhase::RecoveryRequired)
		{
			return ProducerResult::RecoveryRequired;
		}
		return PhaseValue == required ? ProducerResult::Accepted : ProducerResult::InvalidPhase;
	}
	[[nodiscard]] ProducerResult ValidateHighwater(std::uint64_t id) const noexcept
	{
		if (id > Outbox.HighestSubmittedTransactionId())
		{
			return ProducerResult::HighwaterAhead;
		}
		if (id < Outbox.LastAcknowledgedTransactionId())
		{
			return ProducerResult::HighwaterRegressed;
		}
		return ProducerResult::Accepted;
	}
	void EnterRecovery(std::uint64_t id, ProducerRecoveryDisposition disposition) noexcept
	{
		Outbox.MarkAllUnsent();
		RejectedTransactionIdValue = id;
		RecoveryDispositionValue = disposition;
		PhaseValue = ProducerPhase::RecoveryRequired;
	}
	void ClearRecovery() noexcept
	{
		RejectedTransactionIdValue = 0;
		RecoveryDispositionValue = ProducerRecoveryDisposition::None;
	}
	[[nodiscard]] ProducerPhase PhaseUnlocked() const noexcept
	{
		return PhaseValue;
	}
	[[nodiscard]] std::uint64_t GenerationUnlocked() const noexcept
	{
		return GenerationValue;
	}
	[[nodiscard]] bool CanAppendUnlocked() const noexcept
	{
		return PhaseValue == ProducerPhase::Ready && Outbox.CanAppend();
	}
	[[nodiscard]] bool EmptyUnlocked() const noexcept
	{
		return Outbox.Empty();
	}
	[[nodiscard]] bool RecoveryRequiredUnlocked() const noexcept
	{
		return PhaseValue == ProducerPhase::RecoveryRequired;
	}
	[[nodiscard]] ProducerRecoveryDisposition RecoveryDispositionUnlocked() const noexcept
	{
		return RecoveryDispositionValue;
	}
	[[nodiscard]] std::uint64_t RejectedTransactionIdUnlocked() const noexcept
	{
		return RejectedTransactionIdValue;
	}
	[[nodiscard]] std::size_t PendingTransactionCountUnlocked() const noexcept
	{
		return Outbox.PendingTransactionCount();
	}
	[[nodiscard]] std::size_t PendingEventCountUnlocked() const noexcept
	{
		return Outbox.PendingEventCount();
	}
	[[nodiscard]] std::uint64_t NextTransactionIdUnlocked() const noexcept
	{
		return Outbox.NextTransactionId();
	}
	[[nodiscard]] std::uint64_t AcknowledgedTransactionCountUnlocked() const noexcept
	{
		return Outbox.AcknowledgedTransactionCount();
	}
	[[nodiscard]] std::uint64_t AcknowledgedEventCountUnlocked() const noexcept
	{
		return Outbox.AcknowledgedEventCount();
	}
	[[nodiscard]] std::uint64_t SubmittedTransactionCountUnlocked() const noexcept
	{
		return Outbox.SubmittedTransactionCount();
	}
	[[nodiscard]] std::uint64_t LastAcknowledgedTransactionIdUnlocked() const noexcept
	{
		return Outbox.LastAcknowledgedTransactionId();
	}

	mutable std::mutex Mutex;
	detail::OrderedOutboxStorage<Payload> Outbox;
	ProducerPhase PhaseValue = ProducerPhase::Disconnected;
	ProducerRecoveryDisposition RecoveryDispositionValue = ProducerRecoveryDisposition::None;
	std::uint64_t GenerationValue = 0;
	std::uint64_t RejectedTransactionIdValue = 0;
};

using SharedByteBuffer = std::shared_ptr<const std::vector<std::uint8_t>>;
using ProducerSession = OrderedProducerSession<SharedByteBuffer>;
using ProducerSessionEntry = detail::OrderedOutboxEntry<SharedByteBuffer>;

} // namespace openusdconnect::client
