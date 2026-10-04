#include "openusdconnect/client/driver/testing/scripted_socket.h"

#include <algorithm>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <mutex>
#include <optional>
#include <utility>

namespace openusdconnect::client
{
namespace detail
{

struct ScriptedChannel final
{
	enum class Kind : std::uint8_t
	{
		Bytes,
		Timeout,
		Closed,
	};

	struct Delivery final
	{
		Kind Type = Kind::Bytes;
		std::vector<std::uint8_t> Bytes;
	};

	std::deque<Delivery> Inbound;
	std::vector<std::uint8_t> Outbound;
	bool Receiving = false;
	bool ClientClosed = false;
};

struct ScriptedAttempt final
{
	enum class Outcome : std::uint8_t
	{
		Pending,
		Accepted,
		Refused,
	};

	Outcome Result = Outcome::Pending;
	std::shared_ptr<ScriptedChannel> Channel;
	int SystemError = 0;
};

// One lock for every socket, channel, and attempt of a factory.
struct ScriptState final
{
	mutable std::mutex Mutex;
	mutable std::condition_variable Changed;
	std::deque<std::shared_ptr<ScriptedAttempt>> Pending;
	std::size_t Attempts = 0;
};

} // namespace detail

namespace
{

using detail::ScriptedAttempt;
using detail::ScriptedChannel;
using detail::ScriptState;

class ScriptedSocket final : public Socket
{
public:
	explicit ScriptedSocket(std::shared_ptr<ScriptState> state)
		: State(std::move(state))
	{
	}

	~ScriptedSocket() override
	{
		std::lock_guard lock(State->Mutex);
		if (Channel)
		{
			Channel->ClientClosed = true;
			State->Changed.notify_all();
		}
	}

	SocketResult Connect(const std::string&, std::uint16_t, TimePoint deadline) override
	{
		std::unique_lock lock(State->Mutex);
		const auto attempt = std::make_shared<ScriptedAttempt>();
		State->Pending.push_back(attempt);
		++State->Attempts;
		State->Changed.notify_all();
		State->Changed.wait_until(lock, deadline,
								  [&]
								  {
									  return Interrupted ||
											 attempt->Result != ScriptedAttempt::Outcome::Pending;
								  });
		if (attempt->Result == ScriptedAttempt::Outcome::Pending)
		{
			State->Pending.erase(std::find(State->Pending.begin(), State->Pending.end(), attempt));
			return Interrupted ? SocketResult::Interrupted : SocketResult::Timeout;
		}
		if (attempt->Result == ScriptedAttempt::Outcome::Refused)
		{
			Error = attempt->SystemError;
			return SocketResult::Failed;
		}
		Channel = attempt->Channel;
		if (Interrupted)
		{
			Channel->ClientClosed = true;
			State->Changed.notify_all();
			return SocketResult::Interrupted;
		}
		return SocketResult::Success;
	}

	SocketResult SendAll(const std::uint8_t* data, std::size_t size, TimePoint) override
	{
		std::lock_guard lock(State->Mutex);
		if (Interrupted)
		{
			return SocketResult::Interrupted;
		}
		if (!Channel)
		{
			return SocketResult::Failed;
		}
		Channel->Outbound.insert(Channel->Outbound.end(), data, data + size);
		State->Changed.notify_all();
		return SocketResult::Success;
	}

	SocketResult Receive(std::uint8_t* buffer, std::size_t capacity,
						 std::optional<TimePoint> deadline, std::size_t& received) override
	{
		received = 0;
		std::unique_lock lock(State->Mutex);
		for (;;)
		{
			if (Interrupted || std::exchange(WakePending, false))
			{
				return SocketResult::Interrupted;
			}
			if (!Channel)
			{
				return SocketResult::Failed;
			}
			if (!Channel->Inbound.empty())
			{
				return Read(buffer, capacity, received);
			}
			Channel->Receiving = true;
			State->Changed.notify_all();
			bool expired = false;
			if (deadline)
			{
				expired = State->Changed.wait_until(lock, *deadline) == std::cv_status::timeout;
			}
			else
			{
				State->Changed.wait(lock);
			}
			Channel->Receiving = false;
			if (expired && Channel->Inbound.empty() && !Interrupted && !WakePending)
			{
				return SocketResult::Timeout;
			}
		}
	}

	int SystemError() const noexcept override
	{
		return Error;
	}

	void Interrupt() noexcept override
	{
		std::lock_guard lock(State->Mutex);
		Interrupted = true;
		State->Changed.notify_all();
	}

	void Wake() noexcept override
	{
		std::lock_guard lock(State->Mutex);
		WakePending = true;
		State->Changed.notify_all();
	}

private:
	SocketResult Read(std::uint8_t* buffer, std::size_t capacity, std::size_t& received)
	{
		ScriptedChannel::Delivery& delivery = Channel->Inbound.front();
		switch (delivery.Type)
		{
		case ScriptedChannel::Kind::Bytes:
		{
			received = std::min(capacity, delivery.Bytes.size());
			std::memcpy(buffer, delivery.Bytes.data(), received);
			if (received == delivery.Bytes.size())
			{
				Channel->Inbound.pop_front();
			}
			else
			{
				delivery.Bytes.erase(delivery.Bytes.begin(),
									 delivery.Bytes.begin() +
										 static_cast<std::ptrdiff_t>(received));
			}
			return SocketResult::Success;
		}
		case ScriptedChannel::Kind::Timeout:
			Channel->Inbound.pop_front();
			return SocketResult::Timeout;
		case ScriptedChannel::Kind::Closed:
			return SocketResult::Closed;
		}
		return SocketResult::Failed;
	}

	const std::shared_ptr<ScriptState> State;
	std::shared_ptr<ScriptedChannel> Channel;
	int Error = 0;
	bool Interrupted = false;
	bool WakePending = false;
};

} // namespace

ScriptedConnection::ScriptedConnection(std::shared_ptr<ScriptState> state,
									   std::shared_ptr<ScriptedChannel> channel)
	: State(std::move(state))
	, Channel(std::move(channel))
{
}

bool ScriptedConnection::Deliver(std::vector<std::uint8_t> bytes)
{
	std::lock_guard lock(State->Mutex);
	if (Channel->ClientClosed)
	{
		return false;
	}
	Channel->Inbound.push_back({ScriptedChannel::Kind::Bytes, std::move(bytes)});
	State->Changed.notify_all();
	return true;
}

bool ScriptedConnection::DeliverTimeout()
{
	std::lock_guard lock(State->Mutex);
	if (Channel->ClientClosed)
	{
		return false;
	}
	Channel->Inbound.push_back({ScriptedChannel::Kind::Timeout, {}});
	State->Changed.notify_all();
	return true;
}

void ScriptedConnection::Close()
{
	std::lock_guard lock(State->Mutex);
	Channel->Inbound.push_back({ScriptedChannel::Kind::Closed, {}});
	State->Changed.notify_all();
}

std::vector<std::uint8_t> ScriptedConnection::Sent() const
{
	std::lock_guard lock(State->Mutex);
	return Channel->Outbound;
}

bool ScriptedConnection::WaitIdle(std::chrono::milliseconds timeout) const
{
	std::unique_lock lock(State->Mutex);
	State->Changed.wait_for(lock, timeout,
							[&]
							{
								return Channel->ClientClosed ||
									   (Channel->Receiving && Channel->Inbound.empty());
							});
	return !Channel->ClientClosed && Channel->Receiving && Channel->Inbound.empty();
}

bool ScriptedConnection::WaitClosed(std::chrono::milliseconds timeout) const
{
	std::unique_lock lock(State->Mutex);
	return State->Changed.wait_for(lock, timeout,
								   [&]
								   {
									   return Channel->ClientClosed;
								   });
}

bool ScriptedConnection::ClosedByClient() const
{
	std::lock_guard lock(State->Mutex);
	return Channel->ClientClosed;
}

ScriptedSocketFactory::ScriptedSocketFactory()
	: State(std::make_shared<ScriptState>())
{
}

std::unique_ptr<Socket> ScriptedSocketFactory::Create()
{
	return std::make_unique<ScriptedSocket>(State);
}

std::shared_ptr<ScriptedConnection> ScriptedSocketFactory::Accept(std::chrono::milliseconds timeout)
{
	std::unique_lock lock(State->Mutex);
	if (!State->Changed.wait_for(lock, timeout,
								 [&]
								 {
									 return !State->Pending.empty();
								 }))
	{
		return nullptr;
	}
	const std::shared_ptr<ScriptedAttempt> attempt = std::move(State->Pending.front());
	State->Pending.pop_front();
	attempt->Channel = std::make_shared<ScriptedChannel>();
	attempt->Result = ScriptedAttempt::Outcome::Accepted;
	State->Changed.notify_all();
	return std::make_shared<ScriptedConnection>(State, attempt->Channel);
}

bool ScriptedSocketFactory::Refuse(std::chrono::milliseconds timeout, int system_error)
{
	std::unique_lock lock(State->Mutex);
	if (!State->Changed.wait_for(lock, timeout,
								 [&]
								 {
									 return !State->Pending.empty();
								 }))
	{
		return false;
	}
	const std::shared_ptr<ScriptedAttempt> attempt = std::move(State->Pending.front());
	State->Pending.pop_front();
	attempt->SystemError = system_error;
	attempt->Result = ScriptedAttempt::Outcome::Refused;
	State->Changed.notify_all();
	return true;
}

std::size_t ScriptedSocketFactory::Attempts() const
{
	std::lock_guard lock(State->Mutex);
	return State->Attempts;
}

} // namespace openusdconnect::client
