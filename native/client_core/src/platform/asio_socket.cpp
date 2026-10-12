#include "openusdconnect/client/driver/socket.h"

#include <asio/buffer.hpp>
#include <asio/error.hpp>
#include <asio/io_context.hpp>
#include <asio/ip/tcp.hpp>
#include <asio/post.hpp>

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <climits>
#include <optional>
#include <string>
#include <system_error>

#ifdef _WIN32
#include <windows.h>
#else
#include <fcntl.h>
#endif

namespace openusdconnect::client
{
namespace
{

// Windows takes 32-bit buffer lengths.
constexpr std::size_t kMaxWrite = INT_MAX;

// Each call starts one operation on the socket's own io_context and runs it to its end.
// Interrupt and Wake post a cancellation that runs inside the blocked call.
class AsioSocket final : public Socket
{
public:
	SocketResult Connect(const std::string& host, std::uint16_t port, TimePoint deadline) override
	{
		asio::error_code error;
		const asio::ip::tcp::resolver::results_type endpoints =
			asio::ip::tcp::resolver(Context).resolve(host, std::to_string(port), error);
		if (error)
		{
			return Fail(error);
		}
		SocketResult result = SocketResult::Failed;
		for (const asio::ip::tcp::resolver::results_type::value_type& entry : endpoints)
		{
			if (Interrupted.load())
			{
				return SocketResult::Interrupted;
			}
			error = Open(entry.endpoint().protocol());
			if (error)
			{
				result = Fail(error);
				continue;
			}
			Stream.async_connect(entry.endpoint(), Completion{this});
			result = Await(deadline);
			if (result != SocketResult::Failed)
			{
				break;
			}
		}
		if (result == SocketResult::Success)
		{
			Stream.set_option(asio::ip::tcp::no_delay(true), error);
			if (error)
			{
				return Fail(error);
			}
		}
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
			Stream.async_write_some(asio::buffer(data, std::min(size, kMaxWrite)),
									Completion{this});
			if (const SocketResult result = Await(deadline); result != SocketResult::Success)
			{
				return result;
			}
			data += Transferred;
			size -= Transferred;
		}
		return SocketResult::Success;
	}

	SocketResult Receive(std::uint8_t* buffer, std::size_t capacity,
						 std::optional<TimePoint> deadline, std::size_t& received) override
	{
		received = 0;
		if (Interrupted.load() || WakePending.exchange(false))
		{
			return SocketResult::Interrupted;
		}
		Reading = true;
		Stream.async_read_some(asio::buffer(buffer, capacity), Completion{this});
		const SocketResult result = Await(deadline);
		Reading = false;
		if (result == SocketResult::Success)
		{
			received = Transferred;
		}
		else if (result == SocketResult::Interrupted)
		{
			WakePending.store(false);
		}
		return result;
	}

	int SystemError() const noexcept override
	{
		return Error;
	}

	void Interrupt() noexcept override
	{
		if (!Interrupted.exchange(true))
		{
			asio::post(Context,
					   [this]
					   {
						   Cancel();
					   });
		}
	}

	void Wake() noexcept override
	{
		if (!WakePending.exchange(true))
		{
			// The handler runs in whichever call comes next, which can be a send or a read
			// that already consumed this wake.
			asio::post(Context,
					   [this]
					   {
						   if (Reading && WakePending.load())
						   {
							   Cancel();
						   }
					   });
		}
	}

private:
	// The completion handler of every operation; a connect reports no byte count.
	struct Completion final
	{
		AsioSocket* Owner;

		void operator()(const asio::error_code& error, std::size_t transferred = 0) const
		{
			Owner->Outcome = error;
			Owner->Transferred = transferred;
		}
	};

	[[nodiscard]] asio::error_code Open(const asio::ip::tcp& protocol)
	{
		// A failed attempt leaves the socket open; the next address may be another family.
		asio::error_code error;
		Stream.close(error);
		Stream.open(protocol, error);
		if (error)
		{
			return error;
		}
		// Child processes of the host must not inherit the connection and hold it open.
#ifdef _WIN32
		const SOCKET handle = Stream.native_handle();
		if (!SetHandleInformation(reinterpret_cast<HANDLE>(handle), HANDLE_FLAG_INHERIT, 0))
		{
			return {static_cast<int>(GetLastError()), asio::system_category()};
		}
#else
		if (fcntl(Stream.native_handle(), F_SETFD, FD_CLOEXEC) != 0)
		{
			return {errno, asio::system_category()};
		}
#endif
		return error;
	}

	// Runs the started operation until its handler runs, cancelling it at the deadline.
	[[nodiscard]] SocketResult Await(std::optional<TimePoint> deadline)
	{
		Outcome.reset();
		Context.restart();
		if (deadline)
		{
			Context.run_until(*deadline);
		}
		else
		{
			Context.run();
		}
		const bool expired = !Outcome;
		if (expired)
		{
			Cancel();
			Context.run();
		}
		if (!*Outcome)
		{
			return SocketResult::Success;
		}
		if (*Outcome == asio::error::operation_aborted)
		{
			return expired ? SocketResult::Timeout : SocketResult::Interrupted;
		}
		if (*Outcome == asio::error::eof)
		{
			return SocketResult::Closed;
		}
		return Fail(*Outcome);
	}

	void Cancel() noexcept
	{
		// Only a socket that never opened fails to cancel, and it has nothing pending.
		asio::error_code unopened;
		Stream.cancel(unopened);
	}

	// Resolver errors outside the system category have no operating system code.
	[[nodiscard]] SocketResult Fail(const asio::error_code& error) noexcept
	{
		Error = error.category() == asio::system_category()
					? error.value()
					: static_cast<int>(asio::error::host_unreachable);
		return SocketResult::Failed;
	}

	asio::io_context Context;
	asio::ip::tcp::socket Stream{Context};
	std::optional<asio::error_code> Outcome;
	std::size_t Transferred = 0;
	bool Reading = false;
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

TcpSocketFactory::TcpSocketFactory() = default;

std::unique_ptr<Socket> TcpSocketFactory::Create()
{
	return std::make_unique<AsioSocket>();
}

} // namespace openusdconnect::client
