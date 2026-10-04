#include "openusdconnect/client/driver/socket.h"

#include <algorithm>
#include <atomic>
#include <climits>
#include <initializer_list>
#include <optional>
#include <string>
#include <system_error>
#include <utility>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <cerrno>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace openusdconnect::client
{
namespace
{

#ifdef _WIN32
using NativeSocket = SOCKET;
using SocketLength = int;
constexpr NativeSocket kInvalidSocket = INVALID_SOCKET;
constexpr int kShutdownBoth = SD_BOTH;
constexpr int kSendFlags = 0;

[[nodiscard]] int LastSocketError() noexcept
{
	return WSAGetLastError();
}

[[nodiscard]] bool WouldBlock(int error) noexcept
{
	return error == WSAEWOULDBLOCK;
}

[[nodiscard]] bool ConnectPending(int error) noexcept
{
	return error == WSAEWOULDBLOCK;
}

[[nodiscard]] bool Retry(int) noexcept
{
	return false;
}

[[nodiscard]] int ResolutionError(int result) noexcept
{
	return result;
}

void CloseNative(NativeSocket handle) noexcept
{
	closesocket(handle);
}

// A manual-reset event the blocking calls wait on beside the socket.
class Signal final
{
public:
	Signal() noexcept
		: Event(WSACreateEvent())
		, CreationError(Event == WSA_INVALID_EVENT ? WSAGetLastError() : 0)
	{
	}

	~Signal()
	{
		if (Event != WSA_INVALID_EVENT)
		{
			WSACloseEvent(Event);
		}
	}

	Signal(const Signal&) = delete;
	Signal& operator=(const Signal&) = delete;

	[[nodiscard]] int Error() const noexcept
	{
		return CreationError;
	}

	void Raise() noexcept
	{
		WSASetEvent(Event);
	}

	void Clear() noexcept
	{
		WSAResetEvent(Event);
	}

	[[nodiscard]] WSAEVENT Handle() const noexcept
	{
		return Event;
	}

private:
	const WSAEVENT Event;
	const int CreationError;
};
#else
using NativeSocket = int;
using SocketLength = socklen_t;
constexpr NativeSocket kInvalidSocket = -1;
constexpr int kShutdownBoth = SHUT_RDWR;
#ifdef MSG_NOSIGNAL
constexpr int kSendFlags = MSG_NOSIGNAL;
#else
constexpr int kSendFlags = 0;
#endif

[[nodiscard]] int LastSocketError() noexcept
{
	return errno;
}

[[nodiscard]] bool WouldBlock(int error) noexcept
{
	return error == EAGAIN || error == EWOULDBLOCK;
}

[[nodiscard]] bool ConnectPending(int error) noexcept
{
	return error == EINPROGRESS;
}

[[nodiscard]] bool Retry(int error) noexcept
{
	return error == EINTR;
}

[[nodiscard]] int ResolutionError(int result) noexcept
{
	return result == EAI_SYSTEM ? errno : EHOSTUNREACH;
}

void CloseNative(NativeSocket handle) noexcept
{
	close(handle);
}

[[nodiscard]] bool ConfigureDescriptor(int descriptor) noexcept
{
	return fcntl(descriptor, F_SETFD, FD_CLOEXEC) == 0 &&
		   fcntl(descriptor, F_SETFL, fcntl(descriptor, F_GETFL) | O_NONBLOCK) == 0;
}

// A self-pipe the blocking calls poll beside the socket.
class Signal final
{
public:
	Signal() noexcept
	{
		int descriptors[2] = {-1, -1};
		if (pipe(descriptors) != 0)
		{
			CreationError = errno;
			return;
		}
		Read = descriptors[0];
		Write = descriptors[1];
		if (!ConfigureDescriptor(Read) || !ConfigureDescriptor(Write))
		{
			CreationError = errno;
		}
	}

	~Signal()
	{
		for (const int descriptor : {Read, Write})
		{
			if (descriptor >= 0)
			{
				close(descriptor);
			}
		}
	}

	Signal(const Signal&) = delete;
	Signal& operator=(const Signal&) = delete;

	[[nodiscard]] int Error() const noexcept
	{
		return CreationError;
	}

	void Raise() noexcept
	{
		const char byte = 0;
		// A full pipe is already raised.
		[[maybe_unused]] const ssize_t written = write(Write, &byte, 1);
	}

	void Clear() noexcept
	{
		char bytes[64];
		while (read(Read, bytes, sizeof(bytes)) > 0)
		{
		}
	}

	[[nodiscard]] int Handle() const noexcept
	{
		return Read;
	}

private:
	int Read = -1;
	int Write = -1;
	int CreationError = 0;
};
#endif

enum class Readiness : std::uint8_t
{
	Readable,
	Writable,
};

enum class WaitResult : std::uint8_t
{
	Ready,
	Signaled,
	TimedOut,
	Failed,
};

[[nodiscard]] int RemainingMilliseconds(std::optional<TimePoint> deadline) noexcept
{
	if (!deadline)
	{
		return -1;
	}
	const auto remaining =
		std::chrono::ceil<std::chrono::milliseconds>(*deadline - std::chrono::steady_clock::now());
	return static_cast<int>(
		std::clamp<std::chrono::milliseconds::rep>(remaining.count(), 0, INT_MAX - 1));
}

// Blocking calls wait on the socket and a Signal, so Interrupt and Wake end
// them on every platform; shutdown() does not wake a blocked Winsock call.
class BsdSocket final : public Socket
{
public:
	BsdSocket() noexcept
#ifdef _WIN32
		: NetworkEvent(WSACreateEvent())
#endif
	{
	}

	~BsdSocket() override
	{
		CloseNativeSocket();
#ifdef _WIN32
		if (NetworkEvent != WSA_INVALID_EVENT)
		{
			WSACloseEvent(NetworkEvent);
		}
#endif
	}

	SocketResult Connect(const std::string& host, std::uint16_t port, TimePoint deadline) override
	{
		if (const int error = SetupError(); error != 0)
		{
			return Fail(error);
		}
		addrinfo hints{};
		hints.ai_family = AF_UNSPEC;
		hints.ai_socktype = SOCK_STREAM;
		hints.ai_protocol = IPPROTO_TCP;
		addrinfo* addresses = nullptr;
		const std::string service = std::to_string(port);
		if (const int result = getaddrinfo(host.c_str(), service.c_str(), &hints, &addresses);
			result != 0)
		{
			return Fail(ResolutionError(result));
		}
		SocketResult result = SocketResult::Failed;
		for (const addrinfo* address = addresses; address != nullptr; address = address->ai_next)
		{
			result = ConnectTo(*address, deadline);
			if (result != SocketResult::Failed)
			{
				break;
			}
		}
		freeaddrinfo(addresses);
		return result;
	}

	SocketResult SendAll(const std::uint8_t* data, std::size_t size, TimePoint deadline) override
	{
		while (size != 0)
		{
			if (Interrupted.load())
			{
				return SocketResult::Interrupted;
			}
			const int chunk = static_cast<int>(std::min<std::size_t>(size, INT_MAX));
			const auto sent = send(Handle, reinterpret_cast<const char*>(data), chunk, kSendFlags);
			if (sent > 0)
			{
				data += sent;
				size -= static_cast<std::size_t>(sent);
				continue;
			}
			const int error = LastSocketError();
			if (Retry(error))
			{
				continue;
			}
			if (!WouldBlock(error))
			{
				return Fail(error);
			}
			switch (Await(Readiness::Writable, deadline))
			{
			case WaitResult::Ready:
				continue;
			case WaitResult::Signaled:
				return SocketResult::Interrupted;
			case WaitResult::TimedOut:
				return SocketResult::Timeout;
			case WaitResult::Failed:
				return SocketResult::Failed;
			}
		}
		return SocketResult::Success;
	}

	SocketResult Receive(std::uint8_t* buffer, std::size_t capacity,
						 std::optional<TimePoint> deadline, std::size_t& received) override
	{
		received = 0;
		for (;;)
		{
			if (Interrupted.load() || WakePending.exchange(false))
			{
				return SocketResult::Interrupted;
			}
			const int chunk = static_cast<int>(std::min<std::size_t>(capacity, INT_MAX));
			const auto count = recv(Handle, reinterpret_cast<char*>(buffer), chunk, 0);
			if (count > 0)
			{
				received = static_cast<std::size_t>(count);
				return SocketResult::Success;
			}
			if (count == 0)
			{
				return SocketResult::Closed;
			}
			const int error = LastSocketError();
			if (Retry(error))
			{
				continue;
			}
			if (!WouldBlock(error))
			{
				return Fail(error);
			}
			switch (Await(Readiness::Readable, deadline))
			{
			case WaitResult::Ready:
			case WaitResult::Signaled:
				continue;
			case WaitResult::TimedOut:
				return SocketResult::Timeout;
			case WaitResult::Failed:
				return SocketResult::Failed;
			}
		}
	}

	int SystemError() const noexcept override
	{
		return Error;
	}

	void Interrupt() noexcept override
	{
		Interrupted.store(true);
		Wakeup.Raise();
	}

	void Wake() noexcept override
	{
		WakePending.store(true);
		Wakeup.Raise();
	}

private:
	[[nodiscard]] SocketResult Fail(int error) noexcept
	{
		Error = error;
		return SocketResult::Failed;
	}

	[[nodiscard]] int SetupError() const noexcept
	{
#ifdef _WIN32
		if (NetworkEvent == WSA_INVALID_EVENT)
		{
			return WSA_INVALID_HANDLE;
		}
#endif
		return Wakeup.Error();
	}

	[[nodiscard]] SocketResult ConnectTo(const addrinfo& address, TimePoint deadline)
	{
		CloseNativeSocket();
		if (const int error = Open(address.ai_family); error != 0)
		{
			return Fail(error);
		}
		if (connect(Handle, address.ai_addr, static_cast<SocketLength>(address.ai_addrlen)) == 0)
		{
			return SocketResult::Success;
		}
		if (const int error = LastSocketError(); !ConnectPending(error))
		{
			return Fail(error);
		}
		for (;;)
		{
			switch (Await(Readiness::Writable, deadline))
			{
			case WaitResult::Ready:
				if (const std::optional<int> error = ConnectResult())
				{
					return *error == 0 ? SocketResult::Success : Fail(*error);
				}
				continue;
			case WaitResult::Signaled:
				return SocketResult::Interrupted;
			case WaitResult::TimedOut:
				return SocketResult::Timeout;
			case WaitResult::Failed:
				return SocketResult::Failed;
			}
		}
	}

	// Signaled means Interrupt, or a Wake while reading; a Wake during another
	// call is left in WakePending for the next Receive.
	[[nodiscard]] WaitResult Await(Readiness readiness, std::optional<TimePoint> deadline)
	{
		for (;;)
		{
			const WaitResult result = Wait(readiness, deadline);
			if (result != WaitResult::Signaled)
			{
				return result;
			}
			// The flags are set before raising, so reading them after clearing
			// cannot miss a raise that the clear discarded.
			Wakeup.Clear();
			if (Interrupted.load() || (readiness == Readiness::Readable && WakePending.load()))
			{
				return WaitResult::Signaled;
			}
		}
	}

#ifdef _WIN32
	[[nodiscard]] int Open(int family) noexcept
	{
		Handle = WSASocketW(family, SOCK_STREAM, IPPROTO_TCP, nullptr, 0,
							WSA_FLAG_OVERLAPPED | WSA_FLAG_NO_HANDLE_INHERIT);
		if (Handle == kInvalidSocket)
		{
			return WSAGetLastError();
		}
		const BOOL enabled = TRUE;
		if (setsockopt(Handle, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&enabled),
					   sizeof(enabled)) != 0 ||
			WSAEventSelect(Handle, NetworkEvent, FD_CONNECT | FD_READ | FD_WRITE | FD_CLOSE) != 0)
		{
			return WSAGetLastError();
		}
		return 0;
	}

	// WSAEventSelect records every network event; callers retry their call.
	[[nodiscard]] WaitResult Wait(Readiness, std::optional<TimePoint> deadline)
	{
		const WSAEVENT events[] = {NetworkEvent, Wakeup.Handle()};
		const int timeout = RemainingMilliseconds(deadline);
		const DWORD waited = WSAWaitForMultipleEvents(
			2, events, FALSE, timeout < 0 ? WSA_INFINITE : static_cast<DWORD>(timeout), FALSE);
		if (waited == WSA_WAIT_TIMEOUT)
		{
			return WaitResult::TimedOut;
		}
		if (waited == WSA_WAIT_EVENT_0 + 1)
		{
			return WaitResult::Signaled;
		}
		WSANETWORKEVENTS network{};
		if (waited != WSA_WAIT_EVENT_0 || WSAEnumNetworkEvents(Handle, NetworkEvent, &network) != 0)
		{
			Error = WSAGetLastError();
			return WaitResult::Failed;
		}
		if ((network.lNetworkEvents & FD_CONNECT) != 0)
		{
			PendingConnectError = network.iErrorCode[FD_CONNECT_BIT];
		}
		return WaitResult::Ready;
	}

	[[nodiscard]] std::optional<int> ConnectResult() noexcept
	{
		return std::exchange(PendingConnectError, std::nullopt);
	}
#else
	[[nodiscard]] int Open(int family) noexcept
	{
		Handle = socket(family, SOCK_STREAM, IPPROTO_TCP);
		if (Handle == kInvalidSocket || !ConfigureDescriptor(Handle))
		{
			return errno;
		}
		const int enabled = 1;
		if (setsockopt(Handle, IPPROTO_TCP, TCP_NODELAY, &enabled, sizeof(enabled)) != 0)
		{
			return errno;
		}
#ifdef SO_NOSIGPIPE
		if (setsockopt(Handle, SOL_SOCKET, SO_NOSIGPIPE, &enabled, sizeof(enabled)) != 0)
		{
			return errno;
		}
#endif
		return 0;
	}

	[[nodiscard]] WaitResult Wait(Readiness readiness, std::optional<TimePoint> deadline)
	{
		pollfd descriptors[] = {
			{Handle, static_cast<short>(readiness == Readiness::Readable ? POLLIN : POLLOUT), 0},
			{Wakeup.Handle(), POLLIN, 0},
		};
		for (;;)
		{
			const int ready = poll(descriptors, 2, RemainingMilliseconds(deadline));
			if (ready > 0)
			{
				return descriptors[1].revents != 0 ? WaitResult::Signaled : WaitResult::Ready;
			}
			if (ready == 0)
			{
				return WaitResult::TimedOut;
			}
			if (errno != EINTR)
			{
				Error = errno;
				return WaitResult::Failed;
			}
		}
	}

	[[nodiscard]] std::optional<int> ConnectResult() noexcept
	{
		int error = 0;
		socklen_t length = sizeof(error);
		if (getsockopt(Handle, SOL_SOCKET, SO_ERROR, &error, &length) != 0)
		{
			return errno;
		}
		return error;
	}
#endif

	void CloseNativeSocket() noexcept
	{
		if (Handle == kInvalidSocket)
		{
			return;
		}
		shutdown(Handle, kShutdownBoth);
		CloseNative(Handle);
		Handle = kInvalidSocket;
#ifdef _WIN32
		PendingConnectError.reset();
		WSAResetEvent(NetworkEvent);
#endif
	}

#ifdef _WIN32
	const WSAEVENT NetworkEvent;
	std::optional<int> PendingConnectError;
#endif
	Signal Wakeup;
	NativeSocket Handle = kInvalidSocket;
	int Error = 0;
	std::atomic<bool> Interrupted{false};
	std::atomic<bool> WakePending{false};
};

} // namespace

std::string DescribeSystemError(int system_error)
{
	return std::system_category().message(system_error);
}

std::string Describe(const TransportFailure& failure)
{
	return failure.Result == SocketResult::Timeout ? std::string("timed out")
												   : DescribeSystemError(failure.SystemError);
}

TcpSocketFactory::TcpSocketFactory()
{
#ifdef _WIN32
	// Winsock stays initialized for the life of the process.
	static const int startup = []
	{
		WSADATA data{};
		return WSAStartup(MAKEWORD(2, 2), &data);
	}();
	static_cast<void>(startup);
#endif
}

std::unique_ptr<Socket> TcpSocketFactory::Create()
{
	return std::make_unique<BsdSocket>();
}

} // namespace openusdconnect::client
