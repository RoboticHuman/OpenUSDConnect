#pragma once

#include "openusdconnect/client/driver/socket.h"
#include "openusdconnect/client/driver/threaded_receiver_driver.h"
#include "openusdconnect/client/engine/actions.h"
#include "openusdconnect/client/engine/notification.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <variant>
#include <vector>

namespace openusdconnect::client::detail
{

// What a DriverLoop needs from an endpoint beyond the host I/O they share.
template <typename Endpoint>
struct LoopRole final
{
	// A write that still waits for the peer this long after it began is a TransportError.
	std::chrono::milliseconds SendTimeout{};
	// Optional: called when a read waits ReadTimeout without receiving a byte.
	void (Endpoint::*OnReadTimeout)() = nullptr;
	std::chrono::milliseconds ReadTimeout{};
};

// One thread applying a started endpoint's actions with blocking sockets. Of
// the endpoint it calls only the host I/O both endpoints share (OnConnected,
// OnBytes, OnDisconnected, OnTick, Stop, TakeActions, NextWake,
// Status().Stopped) and LoopRole. It ticks at NextWake, during reads too, and
// exits once the endpoint stops. A host thread that queues actions through the
// endpoint, such as Append, QueueControl, or RequestConnect, must then Wake it.
template <typename Endpoint>
class DriverLoop final
{
public:
	DriverLoop(Endpoint& endpoint, LoopRole<Endpoint> role, NotificationQueue& notifications,
			   std::shared_ptr<SocketFactory> sockets, DriverCallbacks callbacks)
		: Target(endpoint)
		, Role(role)
		, Notifications(notifications)
		, Sockets(std::move(sockets))
		, Callbacks(std::move(callbacks))
		, Buffer(kReadBufferSize)
	{
	}

	~DriverLoop()
	{
		Stop();
		std::lock_guard join_lock(JoinMutex);
		if (!Thread.joinable())
		{
			return;
		}
		if (Thread.get_id() == std::this_thread::get_id())
		{
			Thread.detach();
		}
		else
		{
			Thread.join();
		}
	}

	DriverLoop(const DriverLoop&) = delete;
	DriverLoop& operator=(const DriverLoop&) = delete;

	[[nodiscard]] bool Start()
	{
		std::lock_guard join_lock(JoinMutex);
		{
			std::lock_guard lock(Mutex);
			if (Started)
			{
				return false;
			}
		}
		Thread = std::thread(
			[this]
			{
				Run();
			});
		std::lock_guard lock(Mutex);
		Started = true;
		ThreadIdentity = Thread.get_id();
		return true;
	}

	void Stop()
	{
		// First, so a loop that StopRequested wakes finds the endpoint stopped.
		Target.Stop();
		std::shared_ptr<Socket> socket;
		{
			std::lock_guard lock(Mutex);
			StopRequested = true;
			socket = Connection;
		}
		if (socket)
		{
			socket->Interrupt();
		}
		Changed.notify_all();
	}

	void Wake()
	{
		std::shared_ptr<Socket> socket;
		{
			std::lock_guard lock(Mutex);
			WakeRequested = true;
			socket = Connection;
		}
		if (socket)
		{
			socket->Wake();
		}
		Changed.notify_all();
	}

	[[nodiscard]] bool Join(std::optional<std::chrono::milliseconds> timeout)
	{
		{
			std::unique_lock lock(Mutex);
			if (!Started)
			{
				return true;
			}
			if (ThreadIdentity == std::this_thread::get_id())
			{
				return false;
			}
			const auto exited = [this]
			{
				return Exited;
			};
			if (!timeout)
			{
				Changed.wait(lock, exited);
			}
			else if (!Changed.wait_for(lock, *timeout, exited))
			{
				return false;
			}
		}
		std::lock_guard join_lock(JoinMutex);
		if (Thread.joinable())
		{
			Thread.join();
		}
		return true;
	}

	// Waits until ready holds, the loop stops or exits, or timeout passes.
	template <typename Predicate>
	[[nodiscard]] bool Wait(Predicate ready, std::optional<std::chrono::milliseconds> timeout)
	{
		std::unique_lock lock(Mutex);
		const auto done = [&]
		{
			return StopRequested || Exited || ready();
		};
		if (!timeout)
		{
			Changed.wait(lock, done);
		}
		else
		{
			Changed.wait_for(lock, *timeout, done);
		}
		lock.unlock();
		return ready();
	}

	[[nodiscard]] bool Running() const
	{
		std::lock_guard lock(Mutex);
		return Started && !Exited;
	}

	[[nodiscard]] bool Stopped() const
	{
		std::lock_guard lock(Mutex);
		return Started && Exited;
	}

	[[nodiscard]] std::optional<std::thread::id> ThreadId() const
	{
		std::lock_guard lock(Mutex);
		if (ThreadIdentity == std::thread::id())
		{
			return std::nullopt;
		}
		return ThreadIdentity;
	}

	[[nodiscard]] std::optional<TransportFailure> LastFailure() const
	{
		std::lock_guard lock(Mutex);
		return Failure;
	}

private:
	static constexpr std::size_t kReadBufferSize = 64 * 1024;

	[[nodiscard]] static TimePoint Now() noexcept
	{
		return std::chrono::steady_clock::now();
	}

	void Run()
	{
		{
			std::lock_guard lock(Mutex);
			ThreadIdentity = std::this_thread::get_id();
		}
		for (;;)
		{
			Dispatch();
			if (Connection)
			{
				Read();
				continue;
			}
			if (Target.Status().Stopped)
			{
				break;
			}
			Sleep(Target.NextWake());
			Target.OnTick(Now());
		}
		Finish();
	}

	// Delivers notifications before applying each batch of actions, so a
	// token issued by one handshake is stored before the next connects.
	void Dispatch()
	{
		{
			std::lock_guard lock(Mutex);
			WakeRequested = false;
		}
		for (;;)
		{
			Deliver();
			std::vector<Action> actions = Target.TakeActions();
			if (actions.empty())
			{
				break;
			}
			for (Action& action : actions)
			{
				std::visit(
					[this](auto& value)
					{
						Apply(value);
					},
					action);
			}
		}
		// Taking the lock orders this notification after a waiter's check.
		{
			std::lock_guard lock(Mutex);
		}
		Changed.notify_all();
	}

	void Deliver()
	{
		if (!Callbacks.Notifications)
		{
			return;
		}
		for (Notification& notification : Notifications.Drain())
		{
			Callbacks.Notifications(std::move(notification));
		}
	}

	void Apply(const ConnectAction& action)
	{
		std::shared_ptr<Socket> socket = Sockets->Create();
		{
			std::lock_guard lock(Mutex);
			Failure.reset();
			Connection = socket;
			if (StopRequested)
			{
				socket->Interrupt();
			}
		}
		const SocketResult result = socket->Connect(action.Host, action.Port, action.Deadline);
		if (result != SocketResult::Success)
		{
			Fail(SocketOperation::Connect, result, *socket,
				 "could not connect to " + action.Host + ":" + std::to_string(action.Port));
			Close(DisconnectReason::ConnectFailed);
			return;
		}
		const std::optional<std::string> token =
			Callbacks.Token ? Callbacks.Token() : std::optional<std::string>(std::in_place);
		if (!token)
		{
			Close(DisconnectReason::ConnectFailed);
			return;
		}
		Target.OnConnected(*token);
	}

	void Apply(const SendAction& action)
	{
		if (!Connection)
		{
			return;
		}
		const SocketResult result = Connection->SendAll(action.Bytes->data(), action.Bytes->size(),
														Now() + Role.SendTimeout);
		if (result != SocketResult::Success)
		{
			Fail(SocketOperation::Send, result, *Connection, "send failed");
			Close(DisconnectReason::TransportError);
		}
	}

	void Apply(const CloseAction& action)
	{
		Close(action.Reason);
	}

	void Apply(const WakeAction&)
	{
		// Run and Read tick at NextWake, which covers every WakeAction.
	}

	void Apply(const LogAction& action)
	{
		Log(action.Level, action.Message);
	}

	void Read()
	{
		const std::optional<TimePoint> wake = Target.NextWake();
		std::optional<TimePoint> deadline = wake;
		if (Role.OnReadTimeout)
		{
			const TimePoint timeout = Now() + Role.ReadTimeout;
			deadline = wake ? std::min(*wake, timeout) : timeout;
		}
		std::size_t received = 0;
		const SocketResult result =
			Connection->Receive(Buffer.data(), Buffer.size(), deadline, received);
		switch (result)
		{
		case SocketResult::Success:
			Target.OnBytes(Buffer.data(), received);
			return;
		case SocketResult::Timeout:
			if (const TimePoint now = Now(); wake && now >= *wake)
			{
				Target.OnTick(now);
			}
			else if (Role.OnReadTimeout)
			{
				(Target.*Role.OnReadTimeout)();
			}
			return;
		case SocketResult::Interrupted:
			return;
		case SocketResult::Closed:
			Close(DisconnectReason::PeerClosed);
			return;
		case SocketResult::Failed:
			Fail(SocketOperation::Receive, result, *Connection, "receive failed");
			Close(DisconnectReason::TransportError);
			return;
		}
	}

	void Sleep(std::optional<TimePoint> until)
	{
		const auto woken = [this]
		{
			return StopRequested || WakeRequested;
		};
		std::unique_lock lock(Mutex);
		if (until)
		{
			Changed.wait_until(lock, *until, woken);
		}
		else
		{
			Changed.wait(lock, woken);
		}
	}

	void Close(DisconnectReason reason)
	{
		std::shared_ptr<Socket> closed;
		{
			std::lock_guard lock(Mutex);
			closed = std::exchange(Connection, nullptr);
		}
		closed.reset();
		Target.OnDisconnected(reason, Now());
	}

	// Records a Timeout or Failed result; an interrupted call is not a failure.
	void Fail(SocketOperation operation, SocketResult result, const Socket& socket,
			  const std::string& context)
	{
		if (result == SocketResult::Interrupted)
		{
			return;
		}
		const TransportFailure failure{operation, result,
									   result == SocketResult::Failed ? socket.SystemError() : 0};
		{
			std::lock_guard lock(Mutex);
			Failure = failure;
		}
		Log(LogLevel::Warning, context + ": " + Describe(failure));
	}

	void Log(LogLevel level, const std::string& message)
	{
		if (Callbacks.Log)
		{
			Callbacks.Log(level, message);
		}
	}

	// Run returns right after this; nothing here may touch the loop after
	// the Exited callback, which may destroy it.
	void Finish()
	{
		std::function<void()> exited;
		{
			std::lock_guard lock(Mutex);
			Exited = true;
			exited = std::move(Callbacks.Exited);
		}
		Changed.notify_all();
		if (exited)
		{
			exited();
		}
	}

	Endpoint& Target;
	const LoopRole<Endpoint> Role;
	NotificationQueue& Notifications;
	const std::shared_ptr<SocketFactory> Sockets;
	DriverCallbacks Callbacks;
	std::vector<std::uint8_t> Buffer;

	mutable std::mutex Mutex;
	std::condition_variable Changed;
	// Written only by the loop thread under Mutex, so the loop reads it unlocked.
	std::shared_ptr<Socket> Connection;
	std::optional<TransportFailure> Failure;
	std::thread::id ThreadIdentity;
	bool Started = false;
	bool Exited = false;
	bool StopRequested = false;
	bool WakeRequested = false;

	// Serializes joining, apart from Mutex so Exited may still use the loop.
	std::mutex JoinMutex;
	std::thread Thread;
};

} // namespace openusdconnect::client::detail
