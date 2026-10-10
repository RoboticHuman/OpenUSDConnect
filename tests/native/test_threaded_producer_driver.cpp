#include "openusdconnect/client/driver/testing/scripted_socket.h"
#include "openusdconnect/client/driver/threaded_producer_driver.h"

#include "driver_recorder.h"
#include "producer_frames.h"
#include "test_check.h"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <future>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

using namespace driver_test;
using namespace producer_test;
using namespace std::chrono_literals;

namespace
{

[[nodiscard]] TimePoint Now()
{
	return std::chrono::steady_clock::now();
}

class Harness final : public DriverHarness<ThreadedProducerDriver>
{
public:
	explicit Harness(const ProducerConfig& config = TestConfig(), DriverCallbacks callbacks = {})
		: DriverHarness(config, std::move(callbacks))
	{
	}

	[[nodiscard]] std::future<bool> ConnectAsync()
	{
		return std::async(std::launch::async,
						  [this]
						  {
							  return Driver->Connect(kPatience);
						  });
	}

	[[nodiscard]] std::future<FlushResult> FlushAsync()
	{
		return std::async(std::launch::async,
						  [this]
						  {
							  return Driver->Flush(kPatience);
						  });
	}

	// Starts the loop if needed and connects; returns the accepted connection.
	[[nodiscard]] std::shared_ptr<ScriptedConnection> Handshake(const server::Hello& hello = {})
	{
		if (!Driver->Running())
		{
			CHECK(Driver->Start());
		}
		std::future<bool> connected = ConnectAsync();
		std::shared_ptr<ScriptedConnection> connection = DriverHarness::Handshake(hello);
		CHECK(connected.get());
		return connection;
	}

	// Appends the next transaction as a host thread does, and returns its frame.
	Bytes Submit(std::string_view prim)
	{
		const std::uint64_t id = Endpoint.NextTransactionId();
		Bytes frame = TransactionFrame(id, prim);
		CHECK(Endpoint.Append(id, frame, 1, "") == ProducerResult::Accepted);
		Driver->Wake();
		return frame;
	}
};

// The frames a connection carried after its Hello.
[[nodiscard]] std::vector<Bytes> AfterHello(const ScriptedConnection& connection)
{
	std::vector<Bytes> frames = SplitFrames(connection.Sent());
	CHECK(!frames.empty() && DecodeSent(frames.front()).payload_type() == Payload::Hello);
	frames.erase(frames.begin());
	return frames;
}

void TestRequestConnectThenHandshake()
{
	DriverCallbacks callbacks;
	callbacks.Token = []
	{
		return std::optional<std::string>("token-1");
	};
	Harness harness(TestConfig(), std::move(callbacks));
	CHECK(!harness.Driver->Running() && !harness.Driver->Stopped());
	CHECK(harness.Driver->Start());
	CHECK(harness.Endpoint.RequestConnect(Now(), Now() + kPatience));
	harness.Driver->Wake();

	const std::shared_ptr<ScriptedConnection> connection = harness.Accept();
	const SentHello hello = DecodeHello(connection->Sent());
	CHECK(hello.Role == "emitter" && hello.Token == "token-1");
	CHECK(hello.ProducerSessionId == "session");
	server::Hello accepted;
	accepted.Token = "issued";
	CHECK(connection->Deliver(server::HelloOk(accepted)));
	// Connect waits for the requested attempt instead of starting its own.
	CHECK(harness.Driver->Connect(kPatience));
	CHECK(harness.Sockets->Attempts() == 1);
	CHECK(Eventually(
		[&harness]
		{
			return harness.Record.Count<Connected>() == 1;
		}));
	CHECK(harness.Record.IssuedToken() == "issued");
}

void TestConnectReturnsOnceTheAttemptEnds()
{
	{
		Harness harness;
		// Without a running loop nothing would apply the attempt.
		CHECK(!harness.Driver->Connect(kPatience));
		CHECK(harness.Driver->Start());
		std::future<bool> refused = harness.ConnectAsync();
		CHECK(harness.Sockets->Refuse(kPatience, kRefused));
		CHECK(!refused.get());
		const std::optional<TransportFailure> failure = harness.Driver->LastFailure();
		CHECK(failure && failure->Operation == SocketOperation::Connect);
		CHECK(failure->SystemError == kRefused);

		CHECK(!harness.Driver->Connect(0ms));
		static_cast<void>(harness.Handshake());
		CHECK(harness.Driver->Connect(0ms));
	}
	{
		// A server that never answers ends the attempt at the handshake timeout.
		ProducerConfig config = TestConfig();
		config.HandshakeTimeout = 50ms;
		Harness harness(config);
		CHECK(harness.Driver->Start());
		const TimePoint started = Now();
		std::future<bool> silent = harness.ConnectAsync();
		const std::shared_ptr<ScriptedConnection> connection = harness.Accept();
		CHECK(!silent.get());
		CHECK(Now() - started >= 50ms && Now() - started < kPatience);
		CHECK(connection->WaitClosed(kPatience));
		CHECK(!harness.Endpoint.Status().Connected);
	}
}

void TestFlushReconnectsAndReplays()
{
	Harness harness;
	const std::shared_ptr<ScriptedConnection> first = harness.Handshake();
	const std::size_t hello_size = first->Sent().size();
	const Bytes frame = harness.Submit("/A");
	CHECK(first->WaitSent(hello_size + frame.size(), kPatience));
	first->Close();
	CHECK(first->WaitClosed(kPatience));
	// A zero timeout only reports.
	CHECK(harness.Driver->Flush(0ms) == FlushResult::Unfinished);
	CHECK(harness.Sockets->Attempts() == 1);

	std::future<FlushResult> flushed = harness.FlushAsync();
	const std::shared_ptr<ScriptedConnection> second = harness.Accept();
	CHECK(second->Deliver(server::HelloOk()));
	CHECK(second->WaitSent(hello_size + frame.size(), kPatience));
	CHECK(AfterHello(*second) == std::vector<Bytes>{frame});
	CHECK(second->Deliver(server::Acknowledged(1)));
	CHECK(flushed.get() == FlushResult::Flushed);
}

void TestFlushWaitsOutTheRateLimit()
{
	Harness harness;
	const std::shared_ptr<ScriptedConnection> first = harness.Handshake();
	const std::size_t hello_size = first->Sent().size();
	const Bytes frame = harness.Submit("/A");
	CHECK(first->Deliver(server::RateLimited(0.2F)));
	CHECK(first->WaitClosed(kPatience));
	const TimePoint closed = Now();

	std::future<FlushResult> flushed = harness.FlushAsync();
	const std::shared_ptr<ScriptedConnection> second = harness.Accept();
	CHECK(Now() - closed >= 150ms);
	CHECK(second->Deliver(server::HelloOk()));
	CHECK(second->WaitSent(hello_size + frame.size(), kPatience));
	CHECK(AfterHello(*second) == std::vector<Bytes>{frame});
	CHECK(second->Deliver(server::Acknowledged(1)));
	CHECK(flushed.get() == FlushResult::Flushed);
}

void TestStalledWriteClosesAtTheSendDeadline()
{
	ProducerConfig config = TestConfig();
	config.HandshakeTimeout = 250ms;
	Harness harness(config);
	const std::shared_ptr<ScriptedConnection> connection = harness.Handshake();
	connection->StallSends();
	const TimePoint stalled = Now();
	static_cast<void>(harness.Submit("/A"));
	CHECK(connection->WaitClosed(kPatience));
	CHECK(Now() - stalled >= 250ms);
	CHECK(Eventually(
		[&harness]
		{
			return harness.Record.Count<Disconnected>() == 1;
		}));
	const std::optional<TransportFailure> failure = harness.Driver->LastFailure();
	CHECK(failure && failure->Operation == SocketOperation::Send);
	CHECK(failure->Result == SocketResult::Timeout);
	CHECK(harness.Record.All<Disconnected>().front().Reason == DisconnectReason::TransportError);
	const ProducerStatus status = harness.Endpoint.Status();
	CHECK(!status.Connected && status.PendingTransactions == 1);
}

void TestStopInterruptsBlockingCalls()
{
	{
		Harness harness;
		static_cast<void>(harness.Handshake());
		static_cast<void>(harness.Submit("/A"));
		harness.Driver->Stop();
		CHECK(harness.Driver->Join(kPatience));
		CHECK(!harness.Driver->Connect(kPatience));
		CHECK(harness.Driver->Flush(kPatience) == FlushResult::Unfinished);
	}
	{
		// A connect the server never completes.
		Harness harness;
		CHECK(harness.Driver->Start());
		std::future<bool> pending = harness.ConnectAsync();
		CHECK(Eventually(
			[&harness]
			{
				return harness.Sockets->Attempts() == 1;
			}));
		harness.Driver->Stop();
		CHECK(harness.Driver->Join(kPatience));
		CHECK(!pending.get());
	}
}

void TestCloseWritesWhatWasQueued()
{
	{
		Harness harness;
		const std::shared_ptr<ScriptedConnection> connection = harness.Handshake();
		const Bytes frame = harness.Submit("/A");
		CHECK(harness.Driver->Close(kPatience));
		CHECK(connection->ClosedByClient());
		const std::vector<Bytes> frames = AfterHello(*connection);
		CHECK(frames.size() == 2 && frames.front() == frame);
		CHECK(DecodeSent(frames.back()).payload_type() == Payload::Quit);
	}
	{
		// A peer that stopped reading cannot hold Close past its bound.
		ProducerConfig config = TestConfig();
		config.HandshakeTimeout = kPatience;
		Harness harness(config);
		const std::shared_ptr<ScriptedConnection> connection = harness.Handshake();
		connection->StallSends();
		static_cast<void>(harness.Submit("/A"));
		const TimePoint closing = Now();
		static_cast<void>(harness.Driver->Close(100ms));
		CHECK(Now() - closing < kPatience / 5);
		CHECK(harness.Driver->Join(kPatience));
		CHECK(connection->ClosedByClient());
	}
}

void TestRejectedTransactionSurfacesThroughFailure()
{
	Harness harness;
	const std::shared_ptr<ScriptedConnection> connection = harness.Handshake();
	static_cast<void>(harness.Submit("/A"));
	std::future<FlushResult> flushed = harness.FlushAsync();
	CHECK(connection->Deliver(
		server::Rejected(1, TransactionRejectionCode::InvalidTransaction, "bad edit")));
	CHECK(flushed.get() == FlushResult::RecoveryRequired);
	CHECK(connection->WaitClosed(kPatience));
	const std::optional<TransactionFailure> failure = harness.Endpoint.Failure();
	CHECK(failure && failure->TransactionId == 1 && failure->Reason == "bad edit");
	// Recovery comes first, so nothing reconnects.
	CHECK(!harness.Driver->Connect(kPatience));
	CHECK(harness.Driver->Flush(kPatience) == FlushResult::RecoveryRequired);
	CHECK(harness.Sockets->Attempts() == 1);
}

// The loop thread cannot wait for itself, so its blocking calls only report.
void TestCallbacksCannotBlockOnTheLoop()
{
	ThreadedProducerDriver* driver = nullptr;
	std::promise<std::pair<bool, FlushResult>> reported;
	DriverCallbacks callbacks;
	callbacks.Notifications = [&](Notification notification)
	{
		if (std::holds_alternative<Disconnected>(notification))
		{
			reported.set_value({driver->Connect(kPatience), driver->Flush(kPatience)});
		}
	};
	Harness harness(TestConfig(), std::move(callbacks));
	driver = harness.Driver.get();
	const std::shared_ptr<ScriptedConnection> connection = harness.Handshake();
	static_cast<void>(harness.Submit("/A"));
	connection->Close();
	std::future<std::pair<bool, FlushResult>> result = reported.get_future();
	CHECK(result.wait_for(kPatience / 2) == std::future_status::ready);
	const auto [connected, flushed] = result.get();
	CHECK(!connected && flushed == FlushResult::Unfinished);
}

} // namespace

int main()
{
	TestRequestConnectThenHandshake();
	TestConnectReturnsOnceTheAttemptEnds();
	TestFlushReconnectsAndReplays();
	TestFlushWaitsOutTheRateLimit();
	TestStalledWriteClosesAtTheSendDeadline();
	TestStopInterruptsBlockingCalls();
	TestCloseWritesWhatWasQueued();
	TestRejectedTransactionSurfacesThroughFailure();
	TestCallbacksCannotBlockOnTheLoop();
	return 0;
}
