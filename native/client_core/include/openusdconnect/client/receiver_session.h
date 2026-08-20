#pragma once

#include <algorithm>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>
#include <utility>
#include <vector>

namespace openusdconnect::client
{

enum class ReceiverMessageKind : std::uint8_t
{
	Event,
	LayerGraphState,
	Resync,
	Other,
};

enum class AcceptResult : std::uint8_t
{
	Accepted,
	StaleGeneration,
	QueueFull,
	Duplicate,
	SequenceGap,
	InvalidSequence,
};

struct ConnectionStart final
{
	std::uint64_t Generation;
	std::int32_t SyncFrom;
};

struct ReplayState final
{
	std::uint64_t Generation;
	std::int32_t HeadSeq;
	std::uint64_t Epoch;
	std::uint64_t Serial;
};

template <typename Payload>
class OrderedReceiverSession final
{
public:
	OrderedReceiverSession(std::int32_t initial_sync_from, std::size_t max_messages,
						   bool require_contiguous = false)
		: InitialSyncFrom(initial_sync_from)
		, MaxMessages(max_messages)
		, RequireContiguous(require_contiguous)
		, LastReceivedSequence(initial_sync_from - 1)
		, LastAppliedSequenceValue(initial_sync_from - 1)
	{
		assert(IsValidConfiguration(InitialSyncFrom, MaxMessages));
	}

	[[nodiscard]] static bool IsValidConfiguration(std::int32_t initial_sync_from,
												   std::size_t max_messages) noexcept
	{
		return initial_sync_from >= 1 && max_messages != 0;
	}

	[[nodiscard]] ConnectionStart BeginConnection()
	{
		std::lock_guard lock(Mutex);
		++GenerationValue;
		ResetSynchronization();
		const std::int32_t SyncFrom = RequestedReplayFrom.value_or(
			LastReceivedSequence > 0 ? LastReceivedSequence + 1 : InitialSyncFrom);
		RequestedReplayFrom.reset();
		return {GenerationValue, SyncFrom};
	}

	void Disconnect(std::uint64_t generation) noexcept
	{
		std::lock_guard lock(Mutex);
		if (generation == GenerationValue)
		{
			ResetSynchronization();
		}
	}

	[[nodiscard]] AcceptResult Accept(std::uint64_t generation, ReceiverMessageKind kind,
									  std::int32_t sequence, Payload payload)
	{
		if ((kind == ReceiverMessageKind::Event || kind == ReceiverMessageKind::LayerGraphState) &&
			sequence < 1)
		{
			return AcceptResult::InvalidSequence;
		}

		std::lock_guard lock(Mutex);
		if (generation != GenerationValue)
		{
			return AcceptResult::StaleGeneration;
		}
		if (RequireContiguous &&
			(kind == ReceiverMessageKind::Event || kind == ReceiverMessageKind::LayerGraphState))
		{
			if (sequence <= LastReceivedSequence)
			{
				return AcceptResult::Duplicate;
			}
			if (sequence != LastReceivedSequence + 1)
			{
				return AcceptResult::SequenceGap;
			}
		}
		if (Frames.size() == MaxMessages)
		{
			OverflowedValue = true;
			return AcceptResult::QueueFull;
		}
		if (kind == ReceiverMessageKind::Resync)
		{
			LastReceivedSequence = 0;
			ResetSynchronization();
		}
		else if (kind == ReceiverMessageKind::Event || kind == ReceiverMessageKind::LayerGraphState)
		{
			LastReceivedSequence = std::max(LastReceivedSequence, sequence);
		}
		Frames.push_back(std::move(payload));
		++IncomingSerial;
		return AcceptResult::Accepted;
	}

	[[nodiscard]] AcceptResult AcceptReplayComplete(std::uint64_t generation, std::int32_t head_seq,
													std::uint64_t epoch)
	{
		if (head_seq < 0)
		{
			return AcceptResult::InvalidSequence;
		}
		std::lock_guard lock(Mutex);
		if (generation != GenerationValue)
		{
			return AcceptResult::StaleGeneration;
		}
		PendingReplay = ReplayState{generation, head_seq, epoch, IncomingSerial};
		return AcceptResult::Accepted;
	}

	[[nodiscard]] std::vector<Payload> Drain(std::optional<std::size_t> max_messages = std::nullopt)
	{
		assert(!max_messages.has_value() || *max_messages != 0);
		std::lock_guard lock(Mutex);
		const std::size_t Count = std::min(max_messages.value_or(Frames.size()), Frames.size());
		std::vector<Payload> Drained;
		Drained.reserve(Count);
		for (std::size_t Index = 0; Index < Count; ++Index)
		{
			Drained.push_back(std::move(Frames.front()));
			Frames.pop_front();
		}
		DrainedSerial += Count;
		return Drained;
	}

	[[nodiscard]] bool TryPop(Payload& payload)
	{
		std::lock_guard lock(Mutex);
		if (Frames.empty())
		{
			return false;
		}
		payload = std::move(Frames.front());
		Frames.pop_front();
		++DrainedSerial;
		return true;
	}

	[[nodiscard]] bool MarkAppliedThrough(std::uint64_t generation, std::int32_t sequence)
	{
		std::lock_guard lock(Mutex);
		if (generation != GenerationValue || sequence < LastAppliedSequenceValue ||
			sequence > LastReceivedSequence)
		{
			return false;
		}
		LastAppliedSequenceValue = sequence;
		return true;
	}

	[[nodiscard]] bool MarkReplayApplied()
	{
		std::lock_guard lock(Mutex);
		if (!PendingReplay.has_value())
		{
			return false;
		}
		const ReplayState Marker = *PendingReplay;
		if (Marker.Generation != GenerationValue || DrainedSerial < Marker.Serial)
		{
			return false;
		}
		LastAppliedSequenceValue = std::max(LastAppliedSequenceValue, Marker.HeadSeq);
		ReplayHeadSequenceValue = Marker.HeadSeq;
		ReplayEpochValue = Marker.Epoch;
		SynchronizedValue = true;
		PendingReplay.reset();
		return true;
	}

	[[nodiscard]] bool TryMarkReplayApplied()
	{
		std::lock_guard lock(Mutex);
		if (!PendingReplay.has_value())
		{
			return false;
		}
		const ReplayState Marker = *PendingReplay;
		if (Marker.Generation != GenerationValue || DrainedSerial < Marker.Serial ||
			LastAppliedSequenceValue < Marker.HeadSeq)
		{
			return false;
		}
		ReplayHeadSequenceValue = Marker.HeadSeq;
		ReplayEpochValue = Marker.Epoch;
		SynchronizedValue = true;
		PendingReplay.reset();
		return true;
	}

	[[nodiscard]] bool RequestReplayFrom(std::int32_t sequence)
	{
		if (sequence < 1)
		{
			return false;
		}
		std::lock_guard lock(Mutex);
		++GenerationValue;
		RequestedReplayFrom = sequence;
		LastReceivedSequence = sequence - 1;
		LastAppliedSequenceValue = sequence - 1;
		Frames.clear();
		DrainedSerial = IncomingSerial;
		OverflowedValue = false;
		ResetSynchronization();
		return true;
	}

	void ResetAppliedProgress() noexcept
	{
		std::lock_guard lock(Mutex);
		LastAppliedSequenceValue = 0;
		ResetSynchronization();
	}

	void ClearOverflow() noexcept
	{
		std::lock_guard lock(Mutex);
		OverflowedValue = false;
	}

	[[nodiscard]] std::uint64_t Generation() const noexcept
	{
		std::lock_guard lock(Mutex);
		return GenerationValue;
	}
	[[nodiscard]] std::int32_t LastSequence() const noexcept
	{
		std::lock_guard lock(Mutex);
		return LastReceivedSequence;
	}
	[[nodiscard]] std::int32_t LastAppliedSequence() const noexcept
	{
		std::lock_guard lock(Mutex);
		return LastAppliedSequenceValue;
	}
	[[nodiscard]] std::size_t Size() const noexcept
	{
		std::lock_guard lock(Mutex);
		return Frames.size();
	}
	[[nodiscard]] bool Synchronized() const noexcept
	{
		std::lock_guard lock(Mutex);
		return SynchronizedValue;
	}
	[[nodiscard]] bool Overflowed() const noexcept
	{
		std::lock_guard lock(Mutex);
		return OverflowedValue;
	}
	[[nodiscard]] std::int32_t ReplayHeadSequence() const noexcept
	{
		std::lock_guard lock(Mutex);
		return ReplayHeadSequenceValue;
	}
	[[nodiscard]] std::uint64_t ReplayEpoch() const noexcept
	{
		std::lock_guard lock(Mutex);
		return ReplayEpochValue;
	}

private:
	void ResetSynchronization() noexcept
	{
		SynchronizedValue = false;
		PendingReplay.reset();
	}

	mutable std::mutex Mutex;
	std::deque<Payload> Frames;
	std::optional<std::int32_t> RequestedReplayFrom;
	std::optional<ReplayState> PendingReplay;
	const std::int32_t InitialSyncFrom;
	const std::size_t MaxMessages;
	const bool RequireContiguous;
	std::uint64_t GenerationValue = 0;
	std::uint64_t IncomingSerial = 0;
	std::uint64_t DrainedSerial = 0;
	std::int32_t LastReceivedSequence = 0;
	std::int32_t LastAppliedSequenceValue = 0;
	std::int32_t ReplayHeadSequenceValue = 0;
	std::uint64_t ReplayEpochValue = 0;
	bool SynchronizedValue = false;
	bool OverflowedValue = false;
};

using ReceiverInbox = OrderedReceiverSession<std::vector<std::uint8_t>>;

} // namespace openusdconnect::client
