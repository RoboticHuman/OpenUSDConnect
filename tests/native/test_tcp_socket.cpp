#include "openusdconnect/client/driver/socket.h"

#include "test_check.h"

#include <chrono>
#include <cstdint>
#include <memory>
#include <optional>
#include <thread>
#include <vector>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

using namespace openusdconnect::client;
using namespace std::chrono_literals;

namespace
{

// Long enough that reaching it means an interrupt was missed.
constexpr std::chrono::seconds kPatience{5};

#ifdef _WIN32
using NativeSocket = SOCKET;

void CloseNative(NativeSocket socket)
{
	closesocket(socket);
}
#else
using NativeSocket = int;

void CloseNative(NativeSocket socket)
{
	close(socket);
}
#endif

// The server side of the test connections, on the loopback interface. Create a
// TcpSocketFactory first, which initializes Winsock.
class Listener final
{
public:
	Listener()
		: Handle(socket(AF_INET, SOCK_STREAM, 0))
	{
		sockaddr_in address{};
		address.sin_family = AF_INET;
		address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
		CHECK(bind(Handle, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0);
		CHECK(listen(Handle, 4) == 0);
		socklen_t length = sizeof(address);
		CHECK(getsockname(Handle, reinterpret_cast<sockaddr*>(&address), &length) == 0);
		Port = ntohs(address.sin_port);
	}

	~Listener()
	{
		Close();
	}

	Listener(const Listener&) = delete;
	Listener& operator=(const Listener&) = delete;

	[[nodiscard]] NativeSocket Accept() const
	{
		return accept(Handle, nullptr, nullptr);
	}

	void Close()
	{
		if (Open)
		{
			CloseNative(Handle);
			Open = false;
		}
	}

	std::uint16_t Port = 0;

private:
	NativeSocket Handle;
	bool Open = true;
};

[[nodiscard]] TimePoint Deadline(std::chrono::milliseconds wait = kPatience)
{
	return std::chrono::steady_clock::now() + wait;
}

void TestSendReceiveAndTimeout()
{
	TcpSocketFactory factory;
	Listener listener;
	const std::unique_ptr<Socket> client = factory.Create();
	CHECK(client->Connect("127.0.0.1", listener.Port, Deadline()) == SocketResult::Success);
	const NativeSocket server = listener.Accept();
	CHECK(send(server, "abc", 3, 0) == 3);
	std::uint8_t buffer[16];
	std::size_t received = 0;
	CHECK(client->Receive(buffer, sizeof(buffer), Deadline(), received) == SocketResult::Success);
	CHECK(received == 3 && buffer[0] == 'a');
	const std::uint8_t reply[] = {'h', 'i'};
	CHECK(client->SendAll(reply, sizeof(reply), Deadline()) == SocketResult::Success);
	char echoed[2];
	CHECK(recv(server, echoed, 2, 0) == 2);
	CHECK(client->Receive(buffer, sizeof(buffer), Deadline(50ms), received) ==
		  SocketResult::Timeout);
	CloseNative(server);
	CHECK(client->Receive(buffer, sizeof(buffer), Deadline(), received) == SocketResult::Closed);
}

// Wake ends one Receive and is consumed; Interrupt ends every later call too.
void TestWakeAndInterruptEndBlockedCalls()
{
	TcpSocketFactory factory;
	Listener listener;
	const std::unique_ptr<Socket> client = factory.Create();
	CHECK(client->Connect("127.0.0.1", listener.Port, Deadline()) == SocketResult::Success);
	const NativeSocket server = listener.Accept();
	std::uint8_t buffer[16];
	std::size_t received = 0;

	std::thread waker(
		[&]
		{
			std::this_thread::sleep_for(50ms);
			client->Wake();
		});
	CHECK(client->Receive(buffer, sizeof(buffer), Deadline(), received) ==
		  SocketResult::Interrupted);
	waker.join();
	CHECK(client->Receive(buffer, sizeof(buffer), Deadline(50ms), received) ==
		  SocketResult::Timeout);
	client->Wake();
	CHECK(client->Receive(buffer, sizeof(buffer), Deadline(), received) ==
		  SocketResult::Interrupted);

	std::thread interrupter(
		[&]
		{
			std::this_thread::sleep_for(50ms);
			client->Interrupt();
		});
	CHECK(client->Receive(buffer, sizeof(buffer), std::nullopt, received) ==
		  SocketResult::Interrupted);
	interrupter.join();
	CHECK(client->Receive(buffer, sizeof(buffer), Deadline(), received) ==
		  SocketResult::Interrupted);
	const std::uint8_t byte = 0;
	CHECK(client->SendAll(&byte, 1, Deadline()) == SocketResult::Interrupted);
	CloseNative(server);
}

// A peer that stops reading stalls the writer, which gives up at its deadline.
// Winsock accepts a whole send while its buffer has room, so the writer keeps sending.
void TestStalledWriteTimesOut()
{
	TcpSocketFactory factory;
	Listener listener;
	const std::unique_ptr<Socket> client = factory.Create();
	CHECK(client->Connect("127.0.0.1", listener.Port, Deadline()) == SocketResult::Success);
	const NativeSocket server = listener.Accept();
	const std::vector<std::uint8_t> chunk(1024 * 1024);
	SocketResult result = SocketResult::Success;
	auto started = std::chrono::steady_clock::now();
	for (int chunks = 0; chunks < 1024 && result == SocketResult::Success; ++chunks)
	{
		started = std::chrono::steady_clock::now();
		result = client->SendAll(chunk.data(), chunk.size(), Deadline(100ms));
	}
	CHECK(result == SocketResult::Timeout);
	CHECK(std::chrono::steady_clock::now() - started < kPatience);
	CloseNative(server);
}

// Winsock retries a refused loopback connect for seconds; Interrupt ends it.
void TestConnectEndsWhenRefusedOrInterrupted()
{
	TcpSocketFactory factory;
	Listener listener;
	const std::uint16_t port = listener.Port;
	listener.Close();
	const std::unique_ptr<Socket> client = factory.Create();
	std::thread interrupter(
		[&]
		{
			std::this_thread::sleep_for(100ms);
			client->Interrupt();
		});
	const SocketResult result = client->Connect("127.0.0.1", port, Deadline());
	interrupter.join();
	CHECK(result == SocketResult::Interrupted ||
		  (result == SocketResult::Failed && client->SystemError() != 0));
}

} // namespace

int main()
{
	TestSendReceiveAndTimeout();
	TestWakeAndInterruptEndBlockedCalls();
	TestStalledWriteTimesOut();
	TestConnectEndsWhenRefusedOrInterrupted();
	return 0;
}
