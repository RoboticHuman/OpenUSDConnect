#include "openusdconnect/client/driver/threaded_receiver_driver.h"

#include "driver_loop.h"
#include "openusdconnect/client/engine/receiver_endpoint.h"

#include <chrono>
#include <utility>

namespace openusdconnect::client
{

ThreadedReceiverDriver::ThreadedReceiverDriver(ReceiverEndpoint& endpoint,
											   NotificationQueue& notifications,
											   std::shared_ptr<SocketFactory> sockets,
											   DriverCallbacks callbacks)
	: Endpoint(endpoint)
	, Loop(std::make_unique<detail::DriverLoop<ReceiverEndpoint>>(
		  endpoint,
		  detail::LoopRole<ReceiverEndpoint>{endpoint.Configuration().SocketTimeout,
											 &ReceiverEndpoint::OnReadTimeout,
											 endpoint.Configuration().SocketTimeout},
		  notifications, std::move(sockets), std::move(callbacks)))
{
}

ThreadedReceiverDriver::~ThreadedReceiverDriver() = default;

bool ThreadedReceiverDriver::Start()
{
	static_cast<void>(Endpoint.Start(std::chrono::steady_clock::now()));
	return Loop->Start();
}

void ThreadedReceiverDriver::Stop()
{
	Loop->Stop();
}

void ThreadedReceiverDriver::Wake()
{
	Loop->Wake();
}

bool ThreadedReceiverDriver::Join(std::optional<std::chrono::milliseconds> timeout)
{
	return Loop->Join(timeout);
}

bool ThreadedReceiverDriver::WaitConnected(std::optional<std::chrono::milliseconds> timeout)
{
	return Loop->Wait(
		[this]
		{
			return Endpoint.Status().Connected;
		},
		timeout);
}

bool ThreadedReceiverDriver::WaitSynchronized(std::optional<std::chrono::milliseconds> timeout)
{
	return Loop->Wait(
		[this]
		{
			return Endpoint.Status().Synchronized;
		},
		timeout);
}

bool ThreadedReceiverDriver::Running() const
{
	return Loop->Running();
}

bool ThreadedReceiverDriver::Stopped() const
{
	return Loop->Stopped();
}

std::optional<std::thread::id> ThreadedReceiverDriver::ThreadId() const
{
	return Loop->ThreadId();
}

std::optional<TransportFailure> ThreadedReceiverDriver::LastFailure() const
{
	return Loop->LastFailure();
}

} // namespace openusdconnect::client
