#include "openusdconnect/client/driver/socket.h"

#include "test_check.h"

#include <asio/io_context.hpp>
#include <asio/ip/tcp.hpp>
#include <asio/read.hpp>
#include <asio/write.hpp>

#include <chrono>
#include <cstdint>
#include <memory>
#include <optional>
#include <thread>
#include <vector>

using namespace openusdconnect::client;
using namespace std::chrono_literals;

namespace
{

// Long enough that reaching it means an interrupt was missed.
constexpr std::chrono::seconds kPatience{5};

// The server side of the test connections, on the loopback interface.
struct Listener final
{
	asio::io_context Context;
	asio::ip::tcp::acceptor Acceptor{Context, {asio::ip::address_v4::loopback(), 0}};
	const std::uint16_t Port = Acceptor.local_endpoint().port();
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
	asio::ip::tcp::socket server = listener.Acceptor.accept();
	asio::write(server, asio::buffer("abc", 3));
	std::uint8_t buffer[16];
	std::size_t received = 0;
	CHECK(client->Receive(buffer, sizeof(buffer), Deadline(), received) == SocketResult::Success);
	CHECK(received == 3 && buffer[0] == 'a');
	// Larger than the socket buffers, so writes may complete partially.
	std::vector<std::uint8_t> reply(8 * 1024 * 1024);
	for (std::size_t index = 0; index < reply.size(); ++index)
	{
		reply[index] = static_cast<std::uint8_t>(index % 251);
	}
	std::vector<std::uint8_t> echoed(reply.size());
	std::thread reader(
		[&]
		{
			asio::read(server, asio::buffer(echoed));
		});
	CHECK(client->SendAll(reply.data(), reply.size(), Deadline()) == SocketResult::Success);
	reader.join();
	CHECK(echoed == reply);
	CHECK(client->Receive(buffer, sizeof(buffer), Deadline(50ms), received) ==
		  SocketResult::Timeout);
	server.close();
	CHECK(client->Receive(buffer, sizeof(buffer), Deadline(), received) == SocketResult::Closed);
}

// Wake ends one Receive and is consumed; Interrupt ends every later call too.
void TestWakeAndInterruptEndBlockedCalls()
{
	TcpSocketFactory factory;
	Listener listener;
	const std::unique_ptr<Socket> client = factory.Create();
	CHECK(client->Connect("127.0.0.1", listener.Port, Deadline()) == SocketResult::Success);
	const asio::ip::tcp::socket server = listener.Acceptor.accept();
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
}

// A peer that stops reading stalls the writer, which gives up at its deadline once
// the socket buffers are full.
void TestStalledWriteTimesOut()
{
	TcpSocketFactory factory;
	Listener listener;
	const std::unique_ptr<Socket> client = factory.Create();
	CHECK(client->Connect("127.0.0.1", listener.Port, Deadline()) == SocketResult::Success);
	const asio::ip::tcp::socket server = listener.Acceptor.accept();
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
}

// Winsock retries a refused loopback connect for seconds; Interrupt ends it.
void TestConnectEndsWhenRefusedOrInterrupted()
{
	TcpSocketFactory factory;
	Listener listener;
	const std::uint16_t port = listener.Port;
	listener.Acceptor.close();
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
