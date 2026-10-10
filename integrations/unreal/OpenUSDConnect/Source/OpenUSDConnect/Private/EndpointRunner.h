// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"
#include "HAL/Event.h"
#include "HAL/PlatformProcess.h"
#include "HAL/Runnable.h"
#include "HAL/RunnableThread.h"
#include "USDConnectProtocol.h"

THIRD_PARTY_INCLUDES_START
#include "openusdconnect/client/engine/producer_endpoint.h"
#include "openusdconnect/client/engine/receiver_endpoint.h"
THIRD_PARTY_INCLUDES_END

#include <atomic>
#include <chrono>
#include <optional>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

class FSocket;

// The socket plumbing both roles share, implemented in EndpointRunner.cpp.
namespace OUC::Runner
{
using openusdconnect::client::TimePoint;

// A nonblocking socket connected before Action.Deadline, or nullptr.
FSocket* Connect(const openusdconnect::client::ConnectAction& Action,
				 const std::atomic<bool>& bStopping);
bool SendAll(FSocket& Socket, const std::vector<uint8>& Bytes, TimePoint Deadline,
			 const std::atomic<bool>& bStopping);
// Waits up to WaitTime for data. False once the peer closed or the socket failed;
// OutReceived is zero when nothing arrived.
bool Receive(FSocket& Socket, TArray<uint8>& Buffer, FTimespan WaitTime, int32& OutReceived);
// How long a receiver blocks on its socket before it applies a Wake.
FTimespan ReceiveSlice(std::optional<TimePoint> Until);
void Destroy(FSocket* Socket);
// Waits for the event until Until, and while a socket is open at most until its next poll.
void Wait(FEvent& Event, std::optional<TimePoint> Until, bool bSocketOpen);
void Log(const TCHAR* Role, const openusdconnect::client::LogAction& Action);
} // namespace OUC::Runner

/**
 * Drives one sans-IO client endpoint on its own thread: applies the endpoint's
 * actions with an FSocket, then reports bytes, read timeouts, and time. Only
 * ReadToken calls back into the owner; everything else reaches the game thread
 * through the endpoint's notification queue and Status().
 */
template <typename Endpoint>
class FEndpointRunner final : public FRunnable
{
public:
	FEndpointRunner(Endpoint& InTarget, const TCHAR* InRole, TFunction<FString()> InReadToken)
		: Target(InTarget)
		, Role(InRole)
		, ReadToken(MoveTemp(InReadToken))
		, SocketTimeout(SocketTimeoutOf(InTarget))
		, WakeEvent(FPlatformProcess::GetSynchEventFromPool(false))
	{
		Buffer.SetNumUninitialized(64 * 1024);
	}

	virtual ~FEndpointRunner() override
	{
		StopAndWait();
		FPlatformProcess::ReturnSynchEventToPool(WakeEvent);
	}

	bool Start()
	{
		Thread = FRunnableThread::Create(this, *FString::Printf(TEXT("OpenUSDConnect%s"), Role), 0,
										 TPri_BelowNormal);
		return Thread != nullptr;
	}

	// After another thread queues actions through the endpoint.
	void Wake()
	{
		WakeEvent->Trigger();
	}

	virtual void Stop() override
	{
		Target.Stop();
		bStopping = true;
		WakeEvent->Trigger();
	}

	void StopAndWait()
	{
		Stop();
		if (Thread)
		{
			Thread->WaitForCompletion();
			delete Thread;
			Thread = nullptr;
		}
	}

	virtual uint32 Run() override
	{
		for (;;)
		{
			ApplyActions();
			if (Socket)
			{
				Read();
			}
			else if (Target.Status().Stopped)
			{
				return 0;
			}
			else
			{
				const std::optional<TimePoint> Due = Target.NextWake();
				OUC::Runner::Wait(*WakeEvent, Due, false);
				TickIfDue(Due);
			}
		}
	}

private:
	using TimePoint = openusdconnect::client::TimePoint;
	using DisconnectReason = openusdconnect::client::DisconnectReason;
	static constexpr bool bReadsTimeOut =
		std::is_same_v<Endpoint, openusdconnect::client::ReceiverEndpoint>;

	static std::chrono::milliseconds SocketTimeoutOf(const Endpoint& InTarget)
	{
		if constexpr (bReadsTimeOut)
		{
			return InTarget.Configuration().SocketTimeout;
		}
		else
		{
			return InTarget.Configuration().HandshakeTimeout;
		}
	}

	static TimePoint Now()
	{
		return std::chrono::steady_clock::now();
	}

	void ApplyActions()
	{
		for (std::vector<openusdconnect::client::Action> Actions = Target.TakeActions();
			 !Actions.empty(); Actions = Target.TakeActions())
		{
			for (const openusdconnect::client::Action& Action : Actions)
			{
				std::visit(
					[this](const auto& Value)
					{
						Apply(Value);
					},
					Action);
			}
		}
	}

	void Apply(const openusdconnect::client::ConnectAction& Action)
	{
		Socket = OUC::Runner::Connect(Action, bStopping);
		if (!Socket)
		{
			Target.OnDisconnected(DisconnectReason::ConnectFailed, Now());
			return;
		}
		LastReceived = Now();
		Target.OnConnected(OUC::ToUtf8(ReadToken()));
	}

	void Apply(const openusdconnect::client::SendAction& Action)
	{
		if (Socket &&
			!OUC::Runner::SendAll(*Socket, *Action.Bytes, Now() + SocketTimeout, bStopping))
		{
			Close(DisconnectReason::TransportError);
		}
	}

	void Apply(const openusdconnect::client::CloseAction& Action)
	{
		Close(Action.Reason);
	}

	void Apply(const openusdconnect::client::LogAction& Action)
	{
		OUC::Runner::Log(Role, Action);
	}

	void Close(DisconnectReason Reason)
	{
		OUC::Runner::Destroy(std::exchange(Socket, nullptr));
		Target.OnDisconnected(Reason, Now());
	}

	void Read()
	{
		const std::optional<TimePoint> Due = Target.NextWake();
		int32 Received = 0;
		const FTimespan WaitTime =
			bReadsTimeOut ? OUC::Runner::ReceiveSlice(Due) : FTimespan::Zero();
		if (!OUC::Runner::Receive(*Socket, Buffer, WaitTime, Received))
		{
			Close(DisconnectReason::PeerClosed);
			return;
		}
		if (Received > 0)
		{
			LastReceived = Now();
			Target.OnBytes(Buffer.GetData(), static_cast<size_t>(Received));
			return;
		}
		if constexpr (bReadsTimeOut)
		{
			if (Now() - LastReceived >= SocketTimeout)
			{
				LastReceived = Now();
				Target.OnReadTimeout();
				return;
			}
		}
		else
		{
			OUC::Runner::Wait(*WakeEvent, Due, true);
		}
		TickIfDue(Due);
	}

	void TickIfDue(std::optional<TimePoint> Due)
	{
		if (const TimePoint Current = Now(); Due && Current >= *Due)
		{
			Target.OnTick(Current);
		}
	}

	Endpoint& Target;
	const TCHAR* const Role;
	const TFunction<FString()> ReadToken;
	const std::chrono::milliseconds SocketTimeout;
	FEvent* const WakeEvent;
	FRunnableThread* Thread = nullptr;
	std::atomic<bool> bStopping = false;
	// Touched only by the runner thread.
	FSocket* Socket = nullptr;
	TimePoint LastReceived;
	TArray<uint8> Buffer;
};
