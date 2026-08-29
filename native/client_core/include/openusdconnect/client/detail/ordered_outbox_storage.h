#pragma once

#include <algorithm>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <string>
#include <utility>
#include <vector>

namespace openusdconnect::client::detail
{

template <typename PayloadType>
struct OrderedOutboxEntry final
{
	std::uint64_t TransactionId;
	PayloadType Payload;
	std::size_t EventCount;
	std::string LayerKey;
};

template <typename Payload>
class OrderedOutboxStorage final
{
public:
	using Entry = OrderedOutboxEntry<Payload>;

	explicit OrderedOutboxStorage(std::size_t capacity)
		: Capacity(capacity)
	{
		assert(Capacity != 0);
	}

	void Append(std::uint64_t transaction_id, Payload payload, std::size_t event_count,
				std::string layer_key)
	{
		assert(transaction_id != 0);
		assert(event_count != 0);
		assert(CanAppend());
		assert(transaction_id == NextTransactionIdValue);
		PendingEvents += event_count;
		Queue.push_back({transaction_id, std::move(payload), event_count, std::move(layer_key)});
		++NextTransactionIdValue;
		++SubmittedTransactions;
	}

	void Replace(std::uint64_t transaction_id, Payload payload, std::size_t event_count,
				 std::string layer_key)
	{
		assert(transaction_id != 0);
		assert(event_count != 0);
		auto entry = Find(transaction_id);
		assert(entry != Queue.end());
		PendingEvents -= entry->EventCount;
		PendingEvents += event_count;
		*entry = {transaction_id, std::move(payload), event_count, std::move(layer_key)};
	}

	void AcknowledgeThrough(std::uint64_t transaction_id)
	{
		std::uint64_t transaction_count = 0;
		std::uint64_t event_count = 0;
		while (!Queue.empty() && Queue.front().TransactionId <= transaction_id)
		{
			++transaction_count;
			event_count += Queue.front().EventCount;
			PendingEvents -= Queue.front().EventCount;
			Queue.pop_front();
		}
		const auto removed = static_cast<std::size_t>(transaction_count);
		NextUnsentIndex = removed < NextUnsentIndex ? NextUnsentIndex - removed : 0;
		NextTransactionIdValue = std::max(NextTransactionIdValue, transaction_id + 1);
		LastAcknowledgedTransactionIdValue =
			std::max(LastAcknowledgedTransactionIdValue, transaction_id);
		AcknowledgedTransactions += transaction_count;
		AcknowledgedEvents += event_count;
		AcknowledgedEventsSinceDrain += event_count;
	}

	void ResetSession()
	{
		Queue.clear();
		NextUnsentIndex = 0;
		PendingEvents = 0;
		NextTransactionIdValue = 1;
		LastAcknowledgedTransactionIdValue = 0;
	}

	bool ClaimNextUnsent(Entry& entry)
	{
		if (NextUnsentIndex == Queue.size())
		{
			return false;
		}
		entry = Queue[NextUnsentIndex++];
		return true;
	}

	void MarkAllUnsent() noexcept
	{
		NextUnsentIndex = 0;
	}

	[[nodiscard]] bool CanAppend() const noexcept
	{
		return Queue.size() < Capacity;
	}
	[[nodiscard]] bool Contains(std::uint64_t transaction_id) const noexcept
	{
		return Find(transaction_id) != Queue.end();
	}
	[[nodiscard]] bool Empty() const noexcept
	{
		return Queue.empty();
	}
	[[nodiscard]] std::size_t PendingTransactionCount() const noexcept
	{
		return Queue.size();
	}
	[[nodiscard]] std::size_t PendingEventCount() const noexcept
	{
		return PendingEvents;
	}
	[[nodiscard]] std::uint64_t NextTransactionId() const noexcept
	{
		return NextTransactionIdValue;
	}
	[[nodiscard]] std::uint64_t HighestSubmittedTransactionId() const noexcept
	{
		return NextTransactionIdValue - 1;
	}
	[[nodiscard]] std::uint64_t AcknowledgedTransactionCount() const noexcept
	{
		return AcknowledgedTransactions;
	}
	[[nodiscard]] std::uint64_t AcknowledgedEventCount() const noexcept
	{
		return AcknowledgedEvents;
	}
	[[nodiscard]] std::uint64_t SubmittedTransactionCount() const noexcept
	{
		return SubmittedTransactions;
	}
	[[nodiscard]] std::uint64_t LastAcknowledgedTransactionId() const noexcept
	{
		return LastAcknowledgedTransactionIdValue;
	}
	[[nodiscard]] std::uint64_t DrainAcknowledgedEventCount() noexcept
	{
		const std::uint64_t count = AcknowledgedEventsSinceDrain;
		AcknowledgedEventsSinceDrain = 0;
		return count;
	}
	[[nodiscard]] std::vector<Payload> Payloads() const
	{
		std::vector<Payload> payloads;
		payloads.reserve(Queue.size());
		for (const auto& entry : Queue)
		{
			payloads.push_back(entry.Payload);
		}
		return payloads;
	}
	[[nodiscard]] std::vector<Entry> Entries() const
	{
		return {Queue.begin(), Queue.end()};
	}

private:
	using Iterator = typename std::deque<Entry>::iterator;
	using ConstIterator = typename std::deque<Entry>::const_iterator;

	[[nodiscard]] Iterator Find(std::uint64_t transaction_id) noexcept
	{
		return std::find_if(Queue.begin(), Queue.end(),
							[transaction_id](const Entry& entry)
							{
								return entry.TransactionId == transaction_id;
							});
	}
	[[nodiscard]] ConstIterator Find(std::uint64_t transaction_id) const noexcept
	{
		return std::find_if(Queue.begin(), Queue.end(),
							[transaction_id](const Entry& entry)
							{
								return entry.TransactionId == transaction_id;
							});
	}
	std::deque<Entry> Queue;
	const std::size_t Capacity;
	std::size_t NextUnsentIndex = 0;
	std::size_t PendingEvents = 0;
	std::uint64_t NextTransactionIdValue = 1;
	std::uint64_t SubmittedTransactions = 0;
	std::uint64_t LastAcknowledgedTransactionIdValue = 0;
	std::uint64_t AcknowledgedTransactions = 0;
	std::uint64_t AcknowledgedEvents = 0;
	std::uint64_t AcknowledgedEventsSinceDrain = 0;
};

} // namespace openusdconnect::client::detail
