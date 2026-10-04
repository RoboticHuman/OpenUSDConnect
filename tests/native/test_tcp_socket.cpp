#include "openusdconnect/client/driver/tcp_socket.h"

#include "test_check.h"

#include <chrono>
#include <cstdint>
#include <memory>
#include <thread>

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

[[nodiscard]] TimePoint Deadline()
{
	return std::chrono::steady_clock::now() + kPatience;
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
	CHECK(client->Receive(buffer, sizeof(buffer), kPatience, received) == SocketResult::Success);
	CHECK(received == 3 && buffer[0] == 'a');
	const std::uint8_t reply[] = {'h', 'i'};
	CHECK(client->SendAll(reply, sizeof(reply)) == SocketResult::Success);
	char echoed[2];
	CHECK(recv(server, echoed, 2, 0) == 2);
	CHECK(client->Receive(buffer, sizeof(buffer), 50ms, received) == SocketResult::Timeout);
	CloseNative(server);
	CHECK(client->Receive(buffer, sizeof(buffer), kPatience, received) == SocketResult::Closed);
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
	CHECK(client->Receive(buffer, sizeof(buffer), kPatience, received) ==
		  SocketResult::Interrupted);
	waker.join();
	CHECK(client->Receive(buffer, sizeof(buffer), 50ms, received) == SocketResult::Timeout);
	client->Wake();
	CHECK(client->Receive(buffer, sizeof(buffer), kPatience, received) ==
		  SocketResult::Interrupted);

	std::thread interrupter(
		[&]
		{
			std::this_thread::sleep_for(50ms);
			client->Interrupt();
		});
	CHECK(client->Receive(buffer, sizeof(buffer), kPatience, received) ==
		  SocketResult::Interrupted);
	interrupter.join();
	CHECK(client->Receive(buffer, sizeof(buffer), kPatience, received) ==
		  SocketResult::Interrupted);
	const std::uint8_t byte = 0;
	CHECK(client->SendAll(&byte, 1) == SocketResult::Interrupted);
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
	TestConnectEndsWhenRefusedOrInterrupted();
	return 0;
}
