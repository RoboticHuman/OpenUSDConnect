#pragma once

#include "openusdconnect/client/driver/socket.h"
#include "openusdconnect/client/engine/actions.h"
#include "openusdconnect/client/engine/notification.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <variant>
#include <vector>

namespace openusdconnect::client
{

// Reference host loop: one thread with blocking sockets applies a started
// endpoint's actions. Of the endpoint it calls only the host I/O both
// endpoints share (OnConnected, OnBytes, OnDisconnected, OnTick, Stop,
// TakeActions, NextWake, Status().Stopped) and the OnReadTimeout its role
// supplies. It ticks at NextWake, during reads too, and exits once the
// endpoint stops. A host thread that queues actions through the endpoint must
// then Wake it. Never destroy the driver from one of its callbacks. Hosts with
// their own scheduler drive the endpoint instead.
template <typename Endpoint>
class ThreadedDriver
{
public:
	using EndpointType = Endpoint;

	ThreadedDriver(const ThreadedDriver&) = delete;
	ThreadedDriver& operator=(const ThreadedDriver&) = delete;

	// Starts the loop; false when already started.
	[[nodiscard]] bool Start()
	{
		std::lock_guard lock(Mutex);
		if (Started)
		{
			return false;
		}
		Thread = std::thread(
			[this]
			{
				Run();
			});
		Started = true;
		ThreadIdentity = Thread.get_id();
		return true;
	}

	// Stops the endpoint and wakes the loop, which then exits. Never blocks.
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

	// Waits for the loop to exit; false on timeout or on the driver thread.
	[[nodiscard]] bool Join(std::optional<std::chrono::milliseconds> timeout = std::nullopt)
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
		// The loop takes Mutex no more once it exited, so joining under it cannot block it.
		if (Thread.joinable())
		{
			Thread.join();
		}
		return true;
	}

	[[nodiscard]] bool Running() const
	{
		std::lock_guard lock(Mutex);
		return Started && !Exited;
	}

	// The loop ran and exited.
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

	// Why the latest connection attempt failed, if it did.
	[[nodiscard]] std::optional<TransportFailure> LastFailure() const
	{
		std::lock_guard lock(Mutex);
		return Failure;
	}

protected:
	// notifications must be the queue the endpoint pushes to. A write still
	// waiting for the peer socket_timeout after it began is a TransportError;
	// with on_read_timeout set, a read that waits as long without a byte calls it.
	ThreadedDriver(Endpoint& endpoint, NotificationQueue& notifications,
				   std::shared_ptr<SocketFactory> sockets, DriverCallbacks callbacks,
				   std::chrono::milliseconds socket_timeout,
				   void (Endpoint::*on_read_timeout)() = nullptr)
		: Target(endpoint)
		, Notifications(notifications)
		, Sockets(std::move(sockets))
		, Callbacks(std::move(callbacks))
		, SocketTimeout(socket_timeout)
		, OnReadTimeout(on_read_timeout)
		, Buffer(kReadBufferSize)
	{
	}

	// Stops and joins the loop.
	~ThreadedDriver()
	{
		Stop();
		static_cast<void>(Join());
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

	[[nodiscard]] static TimePoint Now() noexcept
	{
		return std::chrono::steady_clock::now();
	}

	Endpoint& Target;

private:
	static constexpr std::size_t kReadBufferSize = 64 * 1024;

	void Run()
	{
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
		{
			std::lock_guard lock(Mutex);
			Exited = true;
		}
		Changed.notify_all();
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
		const SocketResult result =
			Connection->SendAll(action.Bytes->data(), action.Bytes->size(), Now() + SocketTimeout);
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

	void Apply(const LogAction& action)
	{
		Log(action.Level, action.Message);
	}

	void Read()
	{
		const std::optional<TimePoint> wake = Target.NextWake();
		std::optional<TimePoint> deadline = wake;
		if (OnReadTimeout)
		{
			const TimePoint timeout = Now() + SocketTimeout;
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
			else if (OnReadTimeout)
			{
				(Target.*OnReadTimeout)();
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

	NotificationQueue& Notifications;
	const std::shared_ptr<SocketFactory> Sockets;
	const DriverCallbacks Callbacks;
	const std::chrono::milliseconds SocketTimeout;
	void (Endpoint::* const OnReadTimeout)();
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
	std::thread Thread;
};

} // namespace openusdconnect::client
