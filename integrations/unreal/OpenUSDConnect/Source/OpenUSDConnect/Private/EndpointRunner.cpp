// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "EndpointRunner.h"

#include "Logging/LogMacros.h"
#include "SocketSubsystem.h"
#include "Sockets.h"

DEFINE_LOG_CATEGORY_STATIC(LogUSDConnect, Log, All);

namespace OUC::Runner
{
namespace
{
// A receiver blocks on its socket so data is read at once and a Wake waits up to a slice; a
// producer blocks on its event so a submit is sent at once, and polls its socket for results.
constexpr uint32 ReceiveSliceMilliseconds = 100;
constexpr uint32 PollMilliseconds = 20;

ISocketSubsystem& Sockets()
{
	return *ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
}

uint32 MillisecondsUntil(TimePoint Until, uint32 Limit)
{
	const int64 Remaining =
		std::chrono::ceil<std::chrono::milliseconds>(Until - std::chrono::steady_clock::now())
			.count();
	return static_cast<uint32>(FMath::Clamp<int64>(Remaining, 0, Limit));
}

FTimespan PollSlice(TimePoint Until)
{
	return FTimespan::FromMilliseconds(MillisecondsUntil(Until, PollMilliseconds));
}
} // namespace

FSocket* Connect(const openusdconnect::client::ConnectAction& Action,
				 const std::atomic<bool>& bStopping)
{
	const FAddressInfoResult Resolved =
		Sockets().GetAddressInfo(*ToFString(Action.Host), nullptr, EAddressInfoFlags::Default,
								 NAME_None, SOCKTYPE_Streaming);
	if (Resolved.ReturnCode != SE_NO_ERROR || Resolved.Results.IsEmpty())
	{
		return nullptr;
	}
	const TSharedRef<FInternetAddr> Address = Resolved.Results[0].Address;
	Address->SetPort(Action.Port);
	FSocket* Socket =
		Sockets().CreateSocket(NAME_Stream, TEXT("OpenUSDConnect"), Address->GetProtocolType());
	if (Socket && Socket->SetNonBlocking(true) && Socket->Connect(*Address))
	{
		for (;;)
		{
			const ESocketConnectionState State = Socket->GetConnectionState();
			if (State == SCS_Connected)
			{
				return Socket;
			}
			if (State == SCS_ConnectionError || bStopping ||
				std::chrono::steady_clock::now() >= Action.Deadline)
			{
				break;
			}
			Socket->Wait(ESocketWaitConditions::WaitForWrite, PollSlice(Action.Deadline));
		}
	}
	Destroy(Socket);
	return nullptr;
}

bool SendAll(FSocket& Socket, const std::vector<uint8>& Bytes, TimePoint Deadline,
			 const std::atomic<bool>& bStopping)
{
	size_t Sent = 0;
	while (Sent < Bytes.size())
	{
		int32 Count = 0;
		const int32 Size = static_cast<int32>(FMath::Min<size_t>(Bytes.size() - Sent, MAX_int32));
		if (Socket.Send(Bytes.data() + Sent, Size, Count))
		{
			Sent += static_cast<size_t>(Count);
			continue;
		}
		if (Sockets().GetLastErrorCode() != SE_EWOULDBLOCK || bStopping ||
			std::chrono::steady_clock::now() >= Deadline)
		{
			return false;
		}
		Socket.Wait(ESocketWaitConditions::WaitForWrite, PollSlice(Deadline));
	}
	return true;
}

bool Receive(FSocket& Socket, TArray<uint8>& Buffer, FTimespan WaitTime, int32& OutReceived)
{
	OutReceived = 0;
	return !Socket.Wait(ESocketWaitConditions::WaitForRead, WaitTime) ||
		   Socket.Recv(Buffer.GetData(), Buffer.Num(), OutReceived);
}

FTimespan ReceiveSlice(std::optional<TimePoint> Until)
{
	return FTimespan::FromMilliseconds(Until ? MillisecondsUntil(*Until, ReceiveSliceMilliseconds)
											 : ReceiveSliceMilliseconds);
}

void Destroy(FSocket* Socket)
{
	if (Socket)
	{
		Socket->Close();
		Sockets().DestroySocket(Socket);
	}
}

void Wait(FEvent& Event, std::optional<TimePoint> Until, bool bSocketOpen)
{
	const uint32 Limit = bSocketOpen ? PollMilliseconds : MAX_uint32;
	Event.Wait(Until ? MillisecondsUntil(*Until, Limit) : Limit);
}

void Log(const TCHAR* Role, const openusdconnect::client::LogAction& Action)
{
	const FString Message = ToFString(Action.Message);
	switch (Action.Level)
	{
	case openusdconnect::client::LogLevel::Debug:
		UE_LOG(LogUSDConnect, Verbose, TEXT("%s: %s"), Role, *Message);
		break;
	case openusdconnect::client::LogLevel::Info:
		UE_LOG(LogUSDConnect, Log, TEXT("%s: %s"), Role, *Message);
		break;
	case openusdconnect::client::LogLevel::Warning:
		UE_LOG(LogUSDConnect, Warning, TEXT("%s: %s"), Role, *Message);
		break;
	case openusdconnect::client::LogLevel::Error:
		UE_LOG(LogUSDConnect, Error, TEXT("%s: %s"), Role, *Message);
		break;
	}
}
} // namespace OUC::Runner
