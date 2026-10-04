#include "openusdconnect/client/driver/scripted_socket.h"
#include "openusdconnect/client/driver/threaded_receiver_driver.h"

#include "receiver_frames.h"
#include "test_check.h"

#include <atomic>
#include <chrono>
#include <future>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

using namespace receiver_test;
using namespace std::chrono_literals;

namespace
{

constexpr std::chrono::milliseconds kPatience = 5s;
constexpr int kRefused = 10061;

[[nodiscard]] ReceiverConfig FastConfig()
{
	ReceiverConfig config = TestConfig();
	config.ReconnectBaseDelay = 10ms;
	config.ReconnectMaxDelay = 40ms;
	return config;
}

// Records what the driver thread reports.
class Recorder final
{
public:
	void Add(Notification notification)
	{
		std::lock_guard lock(Mutex);
		if (const TokenIssued* issued = std::get_if<TokenIssued>(&notification))
		{
			Token = issued->Token;
		}
		Notices.push_back(std::move(notification));
	}

	void Log(const std::string& message)
	{
		std::lock_guard lock(Mutex);
		Logs.push_back(message);
	}

	[[nodiscard]] std::string IssuedToken() const
	{
		std::lock_guard lock(Mutex);
		return Token;
	}

	[[nodiscard]] bool Logged(std::string_view text) const
	{
		std::lock_guard lock(Mutex);
		for (const std::string& log : Logs)
		{
			if (log.find(text) != std::string::npos)
			{
				return true;
			}
		}
		return false;
	}

	template <typename T>
	[[nodiscard]] std::size_t Count() const
	{
		std::lock_guard lock(Mutex);
		std::size_t count = 0;
		for (const Notification& notice : Notices)
		{
			count += std::holds_alternative<T>(notice) ? 1 : 0;
		}
		return count;
	}

private:
	mutable std::mutex Mutex;
	std::vector<Notification> Notices;
	std::vector<std::string> Logs;
	std::string Token;
};

// One endpoint driven by the reference driver over scripted sockets.
class Harness final
{
public:
	explicit Harness(const ReceiverConfig& config = FastConfig(), DriverCallbacks callbacks = {})
		: Endpoint(config, Notifications)
		, Sockets(std::make_shared<ScriptedSocketFactory>())
		, Driver(std::make_unique<ThreadedReceiverDriver>(Endpoint, Notifications, Sockets,
														  WithRecorder(std::move(callbacks))))
	{
	}

	~Harness()
	{
		if (Driver)
		{
			Driver->Stop();
			CHECK(Driver->Join(kPatience));
		}
	}

	Harness(const Harness&) = delete;
	Harness& operator=(const Harness&) = delete;

	[[nodiscard]] std::shared_ptr<ScriptedConnection> Accept()
	{
		std::shared_ptr<ScriptedConnection> connection = Sockets->Accept(kPatience);
		CHECK(connection != nullptr);
		CHECK(connection->WaitIdle(kPatience));
		return connection;
	}

	void Deliver(ScriptedConnection& connection, const Bytes& frame)
	{
		CHECK(connection.Deliver(frame));
		CHECK(connection.WaitIdle(kPatience));
	}

	[[nodiscard]] std::shared_ptr<ScriptedConnection> Handshake(const server::Hello& hello = {})
	{
		CHECK(Driver->Start());
		std::shared_ptr<ScriptedConnection> connection = Accept();
		Deliver(*connection, server::HelloOk(hello));
		CHECK(Driver->WaitConnected(kPatience));
		return connection;
	}

	NotificationQueue Notifications;
	ReceiverEndpoint Endpoint;
	Recorder Record;
	const std::shared_ptr<ScriptedSocketFactory> Sockets;
	std::unique_ptr<ThreadedReceiverDriver> Driver;

private:
	DriverCallbacks WithRecorder(DriverCallbacks callbacks)
	{
		if (!callbacks.Notifications)
		{
			callbacks.Notifications = [this](Notification notification)
			{
				Record.Add(std::move(notification));
			};
		}
		callbacks.Log = [this](LogLevel, const std::string& message)
		{
			Record.Log(message);
		};
		return callbacks;
	}
};

void TestConnectSendsTheHelloWithTheProvidedToken()
{
	DriverCallbacks callbacks;
	callbacks.Token = []
	{
		return std::optional<std::string>("token-1");
	};
	Harness harness(FastConfig(), std::move(callbacks));
	CHECK(!harness.Driver->Running() && !harness.Driver->Stopped());
	CHECK(harness.Driver->Start());
	CHECK(!harness.Driver->Start());
	CHECK(harness.Driver->Running());
	CHECK(harness.Driver->ThreadId().has_value());
	const std::shared_ptr<ScriptedConnection> connection = harness.Accept();
	const SentHello hello = DecodeHello(connection->Sent());
	CHECK(hello.Role == "receiver" && hello.SyncFrom == 1);
	CHECK(hello.Token == "token-1");
	CHECK(!harness.Driver->WaitConnected(1ms));
	CHECK(harness.Record.Logged("connecting to 127.0.0.1:7200"));
}

void TestFramesReachTheEndpoint()
{
	Harness harness;
	const std::shared_ptr<ScriptedConnection> connection = harness.Handshake();
	Bytes batch = server::Event(1);
	const Bytes second = server::Event(2);
	batch.insert(batch.end(), second.begin(), second.end());
	harness.Deliver(*connection, batch);
	harness.Deliver(*connection, server::Ping());
	harness.Deliver(*connection, server::ReplayComplete(2, 3));
	CHECK(harness.Endpoint.Status().LastSequence == 2);
	CHECK(!harness.Driver->WaitSynchronized(1ms));

	const std::uint64_t generation = harness.Endpoint.Generation();
	CHECK(Sequences(harness.Endpoint.DrainFrames()) == (std::vector<std::int32_t>{1, 2}));
	CHECK(harness.Endpoint.MarkAppliedThrough(generation, 2));
	CHECK(harness.Endpoint.MarkReplayApplied());
	harness.Driver->Wake();
	CHECK(harness.Driver->WaitSynchronized(kPatience));
	CHECK(harness.Endpoint.Status().ReplayEpoch == 3);
	CHECK(harness.Record.Count<Connected>() == 1);
}

// A token issued by one handshake is presented by the next.
void TestIssuedTokenReachesTheNextHandshake()
{
	DriverCallbacks callbacks;
	Recorder* record = nullptr;
	callbacks.Token = [&record]
	{
		return std::optional<std::string>(record->IssuedToken());
	};
	Harness harness(FastConfig(), std::move(callbacks));
	record = &harness.Record;
	server::Hello hello;
	hello.Token = "issued";
	const std::shared_ptr<ScriptedConnection> first = harness.Handshake(hello);
	CHECK(DecodeHello(first->Sent()).Token.empty());
	first->Close();
	CHECK(first->WaitClosed(kPatience));
	const std::shared_ptr<ScriptedConnection> second = harness.Accept();
	CHECK(DecodeHello(second->Sent()).Token == "issued");
	CHECK(harness.Record.Count<Disconnected>() == 1);
}

void TestAbandonedTokenRetries()
{
	std::atomic<int> calls = 0;
	DriverCallbacks callbacks;
	callbacks.Token = [&calls]
	{
		return ++calls == 1 ? std::nullopt : std::optional<std::string>("later");
	};
	Harness harness(FastConfig(), std::move(callbacks));
	CHECK(harness.Driver->Start());
	const std::shared_ptr<ScriptedConnection> abandoned = harness.Sockets->Accept(kPatience);
	CHECK(abandoned != nullptr && abandoned->WaitClosed(kPatience));
	CHECK(abandoned->Sent().empty());
	CHECK(DecodeHello(harness.Accept()->Sent()).Token == "later");
}

void TestReadTimeoutsReconnect()
{
	ReceiverConfig config = FastConfig();
	config.MaxConsecutiveTimeouts = 2;
	Harness harness(config);
	const std::shared_ptr<ScriptedConnection> connection = harness.Handshake();
	CHECK(connection->DeliverTimeout());
	CHECK(connection->WaitIdle(kPatience));
	CHECK(harness.Endpoint.Status().Connected);
	CHECK(connection->DeliverTimeout());
	CHECK(connection->WaitClosed(kPatience));
	CHECK(harness.Record.Logged("2 consecutive read timeouts"));
	CHECK(DecodeHello(harness.Accept()->Sent()).SyncFrom == 1);
}

void TestFailedConnectIsRecordedAndRetried()
{
	Harness harness;
	CHECK(harness.Driver->Start());
	CHECK(harness.Sockets->Refuse(kPatience, kRefused));
	const std::shared_ptr<ScriptedConnection> connection = harness.Accept();
	CHECK(harness.Sockets->Attempts() == 2);
	CHECK(harness.Record.Logged("could not connect to 127.0.0.1:7200: "));
	CHECK(!harness.Driver->LastFailure());

	ReceiverConfig config = FastConfig();
	config.Reconnect = false;
	Harness once(config);
	CHECK(once.Driver->Start());
	CHECK(once.Sockets->Refuse(kPatience, kRefused));
	CHECK(once.Driver->Join(kPatience));
	CHECK(once.Driver->Stopped() && !once.Driver->Running());
	CHECK(!once.Driver->WaitConnected(kPatience));
	const std::optional<TransportFailure> failure = once.Driver->LastFailure();
	CHECK(failure && failure->Operation == SocketOperation::Connect);
	CHECK(failure->Result == SocketResult::Failed && failure->SystemError == kRefused);
	CHECK(Describe(*failure) == DescribeSystemError(kRefused));
	CHECK(once.Endpoint.Status().Stopped);
}

void TestStopInterruptsBlockingCalls()
{
	{
		Harness harness;
		const std::shared_ptr<ScriptedConnection> connection = harness.Handshake();
		harness.Driver->Stop();
		CHECK(harness.Driver->Join(kPatience));
		CHECK(connection->ClosedByClient());
		CHECK(harness.Driver->Stopped());
		CHECK(harness.Endpoint.Status().Stopped);
	}
	{
		Harness harness;
		CHECK(harness.Driver->Start());
		harness.Driver->Stop();
		CHECK(harness.Driver->Join(kPatience));
		CHECK(!harness.Driver->WaitConnected(std::nullopt));
	}
	{
		// A host may stop the endpoint before the driver starts.
		Harness harness;
		harness.Driver->Stop();
		CHECK(harness.Driver->Start());
		CHECK(harness.Driver->Join(kPatience));
		CHECK(harness.Sockets->Attempts() == 0);
	}
}

void TestWakeAppliesAReplayRequest()
{
	Harness harness;
	const std::shared_ptr<ScriptedConnection> connection = harness.Handshake();
	harness.Deliver(*connection, server::Event(1));
	CHECK(harness.Endpoint.RequestReplayFrom(1));
	harness.Driver->Wake();
	CHECK(connection->WaitClosed(kPatience));
	CHECK(DecodeHello(harness.Accept()->Sent()).SyncFrom == 1);
}

// Callbacks may call back into the endpoint and the driver.
void TestCallbacksReenterTheEndpointAndDriver()
{
	ThreadedReceiverDriver* driver = nullptr;
	ReceiverEndpoint* endpoint = nullptr;
	std::atomic<bool> joined_self = true;
	DriverCallbacks callbacks;
	callbacks.Notifications = [&](Notification notification)
	{
		if (std::holds_alternative<TokenIssued>(notification))
		{
			CHECK(endpoint->Status().Connected);
			CHECK(endpoint->RequestReplayFrom(2));
			driver->Wake();
			joined_self = driver->Join(1ms);
		}
		else if (std::holds_alternative<PlaybackClaimed>(notification))
		{
			driver->Stop();
		}
	};
	Harness harness(FastConfig(), std::move(callbacks));
	driver = harness.Driver.get();
	endpoint = &harness.Endpoint;
	server::Hello hello;
	hello.Token = "issued";
	CHECK(harness.Driver->Start());
	const std::shared_ptr<ScriptedConnection> first = harness.Accept();
	CHECK(first->Deliver(server::HelloOk(hello)));
	CHECK(first->WaitClosed(kPatience));
	CHECK(!joined_self);
	const std::shared_ptr<ScriptedConnection> second = harness.Accept();
	CHECK(DecodeHello(second->Sent()).SyncFrom == 2);
	CHECK(second->Deliver(server::HelloOk()));
	CHECK(second->Deliver(server::Claimed("me")));
	CHECK(harness.Driver->Join(kPatience));
}

void TestExitedMayDestroyTheDriver()
{
	ReceiverConfig config = FastConfig();
	config.Reconnect = false;
	std::promise<void> destroyed;
	std::unique_ptr<ThreadedReceiverDriver>* owner = nullptr;
	DriverCallbacks callbacks;
	callbacks.Exited = [&]
	{
		owner->reset();
		destroyed.set_value();
	};
	Harness harness(config, std::move(callbacks));
	owner = &harness.Driver;
	CHECK(harness.Driver->Start());
	CHECK(harness.Sockets->Refuse(kPatience, kRefused));
	CHECK(destroyed.get_future().wait_for(kPatience) == std::future_status::ready);
	CHECK(harness.Driver == nullptr);
}

} // namespace

int main()
{
	TestConnectSendsTheHelloWithTheProvidedToken();
	TestFramesReachTheEndpoint();
	TestIssuedTokenReachesTheNextHandshake();
	TestAbandonedTokenRetries();
	TestReadTimeoutsReconnect();
	TestFailedConnectIsRecordedAndRetried();
	TestStopInterruptsBlockingCalls();
	TestWakeAppliesAReplayRequest();
	TestCallbacksReenterTheEndpointAndDriver();
	TestExitedMayDestroyTheDriver();
	return 0;
}
