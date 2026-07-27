// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "SyncClient.h"

#include "USDConnectProtocol.h"
#include "USDConnectSubsystem.h"
#include "USDWireFraming.h"

#include "Logging/LogMacros.h"
#include "Sockets.h"
#include "SocketSubsystem.h"
#include "HAL/PlatformProcess.h"

DEFINE_LOG_CATEGORY_STATIC(LogUSDConnect, Log, All);

using namespace OUC;

// ---------------------------------------------------------------------------
// FSyncClient
// ---------------------------------------------------------------------------

FSyncClient::FSyncClient(UUSDConnectSubsystem* InOwner,
                         const FString& InHost,
                         int32 InPort,
                         const FString& InDepartment,
                         const FString& InClientId,
                         const FString& InSessionOrigin,
                         float InReconnectDelaySecs,
                         int32 InInitialLastSeq,
                         const FString& InAuthToken)
	: Owner(InOwner)
	, Host(InHost)
	, Port(InPort)
	, Department(InDepartment)
	, ClientId(InClientId)
	, SessionOrigin(InSessionOrigin)
	, AuthToken(InAuthToken)
	, ReconnectDelaySecs(InReconnectDelaySecs)
	, LastSeq(InInitialLastSeq)
	, Socket(nullptr)
	, Thread(nullptr)
	, bShouldStop(false)
	, bConnected(false)
{
}

FSyncClient::~FSyncClient()
{
	StopAndWait();
}

bool FSyncClient::Start()
{
	Thread = FRunnableThread::Create(this, TEXT("OpenUSDConnect_SyncClient"),
	                                 0, TPri_BelowNormal);
	return Thread != nullptr;
}

void FSyncClient::StopAndWait()
{
	bShouldStop.store(true, std::memory_order_relaxed);
	CloseSocket();  // unblock any pending Recv/Send
	if (Thread)
	{
		Thread->WaitForCompletion();
		delete Thread;
		Thread = nullptr;
	}
}

bool FSyncClient::Init()
{
	return true;
}

uint32 FSyncClient::Run()
{
	// Only log connection failures on state transitions to avoid spamming the
	// log every ReconnectDelaySecs when the server is down. The first attempt
	// after construction logs at Log; subsequent retries log at Verbose until
	// we succeed once.
	bool bHasAnnouncedFailure = false;

	while (!bShouldStop.load(std::memory_order_relaxed))
	{
		// --- Create socket and connect ---
		ISocketSubsystem* SS = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
		if (!SS) { FPlatformProcess::Sleep(ReconnectDelaySecs); continue; }

		Socket = SS->CreateSocket(NAME_Stream, TEXT("USDConnectRecv"), false);
		if (!Socket)
		{
			UE_LOG(LogUSDConnect, Warning, TEXT("Failed to create socket"));
			FPlatformProcess::Sleep(ReconnectDelaySecs);
			continue;
		}

		int32 ActualRcvBuf = 0;
		Socket->SetReceiveBufferSize(256 * 1024, ActualRcvBuf);  // best-effort, ignore actual

		TSharedRef<FInternetAddr> Addr = SS->CreateInternetAddr();
		bool bValid = false;
		Addr->SetIp(*Host, bValid);
		if (!bValid)
		{
			UE_LOG(LogUSDConnect, Warning, TEXT("Invalid server address: %s"), *Host);
			CloseSocket();
			FPlatformProcess::Sleep(ReconnectDelaySecs);
			continue;
		}
		Addr->SetPort(Port);

		if (!Socket->Connect(*Addr))
		{
			if (bHasAnnouncedFailure)
			{
				UE_LOG(LogUSDConnect, Verbose,
					TEXT("Could not connect to %s:%d — retrying in %.1fs"),
					*Host, Port, ReconnectDelaySecs);
			}
			else
			{
				UE_LOG(LogUSDConnect, Log,
					TEXT("Could not connect to %s:%d — retrying in %.1fs"),
					*Host, Port, ReconnectDelaySecs);
				bHasAnnouncedFailure = true;
			}
			CloseSocket();
			FPlatformProcess::Sleep(ReconnectDelaySecs);
			continue;
		}
		bHasAnnouncedFailure = false;

		// --- Send HELLO ---
		const int32 SyncFrom = LastSeq > 0 ? LastSeq + 1 : 1;
		UE_LOG(LogUSDConnect, Log,
			TEXT("Connected to OpenUSDConnect server at %s:%d (receiver, sync_from=%d)"),
			*Host, Port, SyncFrom);
		TArray<uint8> HelloFrame =
			OUC::BuildHelloFrame(TEXT("receiver"), SyncFrom, ClientId, SessionOrigin, Department, AuthToken);
		if (!SendAll(HelloFrame.GetData(), HelloFrame.Num()))
		{
			UE_LOG(LogUSDConnect, Warning, TEXT("Failed to send HELLO"));
			CloseSocket();
			FPlatformProcess::Sleep(ReconnectDelaySecs);
			continue;
		}

		// --- Read HELLO_OK ---
		{
			TArray<uint8> Frame;
			if (!RecvFrame(Frame))
			{
				CloseSocket();
				FPlatformProcess::Sleep(ReconnectDelaySecs);
				continue;
			}

			const OpenUSDConnect::Payload PType = GetEnvelopePayloadType(Frame);
			if (PType == OpenUSDConnect::Payload::AuthRejected)
			{
				UE_LOG(LogUSDConnect, Error, TEXT("OpenUSDConnect: auth rejected by server"));
				if (Owner) { Owner->OnClientAuthRejected(TEXT("receiver")); }
				CloseSocket();
				return 0;
			}
			if (PType != OpenUSDConnect::Payload::HelloOk)
			{
				UE_LOG(LogUSDConnect, Warning,
					TEXT("Unexpected response to HELLO (type=%u)"), static_cast<uint8>(PType));
				CloseSocket();
				FPlatformProcess::Sleep(ReconnectDelaySecs);
				continue;
			}
			const OpenUSDConnect::Envelope* Env = GetEnvelopeFromFrame(Frame);
			const OpenUSDConnect::HelloOk* HelloOk = Env ? Env->payload_as_HelloOk() : nullptr;
			const FString IssuedToken = HelloOk ? ToFString(HelloOk->token()) : FString();
			if (Owner) { Owner->OnClientHelloOk(TEXT("receiver")); }
			if (!IssuedToken.IsEmpty())
			{
				AuthToken = IssuedToken;
				if (Owner) { Owner->OnClientTokenIssued(IssuedToken); }
			}
			UE_LOG(LogUSDConnect, Log, TEXT("HELLO_OK received — entering receive loop"));
		}

		bConnected.store(true, std::memory_order_relaxed);

		// --- Receive loop ---
		while (!bShouldStop.load(std::memory_order_relaxed))
		{
			TArray<uint8> Frame;
			if (!RecvFrame(Frame)) break;
			HandleFrame(Frame);
		}

		bConnected.store(false, std::memory_order_relaxed);
		CloseSocket();

		if (!bShouldStop.load(std::memory_order_relaxed))
		{
			UE_LOG(LogUSDConnect, Log,
				TEXT("Receiver disconnected — reconnecting in %.1fs"),
				ReconnectDelaySecs);
			FPlatformProcess::Sleep(ReconnectDelaySecs);
		}
	}
	return 0;
}

void FSyncClient::Stop()
{
	bShouldStop.store(true, std::memory_order_relaxed);
	CloseSocket();
}

void FSyncClient::Exit()
{
	bConnected.store(false, std::memory_order_relaxed);
	CloseSocket();
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

void FSyncClient::CloseSocket()
{
	if (Socket)
	{
		Socket->Close();
		if (ISocketSubsystem* SS = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM))
		{
			SS->DestroySocket(Socket);
		}
		Socket = nullptr;
	}
}

bool FSyncClient::RecvExact(uint8* Buf, int32 Needed)
{
	int32 Got = 0;
	while (Got < Needed)
	{
		if (bShouldStop.load(std::memory_order_relaxed) || !Socket) return false;

		int32 Read = 0;
		const bool bOK = Socket->Recv(Buf + Got, Needed - Got, Read);
		if (!bOK || Read <= 0) return false;
		Got += Read;
	}
	return true;
}

bool FSyncClient::RecvFrame(TArray<uint8>& OutFrame)
{
	uint8 LenBuf[4];
	if (!RecvExact(LenBuf, 4)) return false;

	const uint32 PayloadLen =
		((uint32)LenBuf[0] << 24) | ((uint32)LenBuf[1] << 16) |
		((uint32)LenBuf[2] << 8)  |  (uint32)LenBuf[3];

	if (PayloadLen == 0 || PayloadLen > kMaxFrameSize)
	{
		UE_LOG(LogUSDConnect, Warning,
			TEXT("Bad frame length %u — disconnecting"), PayloadLen);
		return false;
	}

	OutFrame.SetNumUninitialized(static_cast<int32>(PayloadLen));
	return RecvExact(OutFrame.GetData(), static_cast<int32>(PayloadLen));
}

bool FSyncClient::SendAll(const uint8* Data, int32 Len)
{
	int32 Sent = 0;
	while (Sent < Len)
	{
		if (!Socket) return false;
		int32 ThisSent = 0;
		if (!Socket->Send(Data + Sent, Len - Sent, ThisSent) || ThisSent <= 0)
		{
			return false;
		}
		Sent += ThisSent;
	}
	return true;
}

void FSyncClient::HandleFrame(const TArray<uint8>& Frame)
{
	const OpenUSDConnect::Envelope* Env = GetEnvelopeFromFrame(Frame);
	const OpenUSDConnect::Payload PType =
		Env ? Env->payload_type() : OpenUSDConnect::Payload::NONE;

	if (PType == OpenUSDConnect::Payload::BroadcastEvent)
	{
		if (const OpenUSDConnect::BroadcastEvent* BcEvent = Env->payload_as_BroadcastEvent())
		{
			const int32 Seq = BcEvent->seq();
			if (Seq > LastSeq) LastSeq = Seq;

			// Echo suppression: skip events we originated (server broadcasts them back).
			const FString Origin = ToFString(BcEvent->origin());
			if (!Origin.IsEmpty() && Origin == SessionOrigin)
			{
				return;
			}

			UE_LOG(LogUSDConnect, Verbose,
				TEXT("Received BroadcastEvent seq=%d origin='%s'"), Seq, *Origin);
		}

		TArray<uint8> Copy = Frame;
		if (Owner)
		{
			Owner->EnqueueEvent(MoveTemp(Copy));
		}
	}
	else if (PType == OpenUSDConnect::Payload::Ping)
	{
		// Heartbeat — receivers ignore (server is just checking the connection).
	}
	else if (PType == OpenUSDConnect::Payload::RateLimited)
	{
		const OpenUSDConnect::RateLimited* RL = Env->payload_as_RateLimited();
		const float Retry = RL ? RL->retry_after() : 1.0f;
		UE_LOG(LogUSDConnect, Warning,
			TEXT("Rate limited — sleeping %.1fs"), Retry);
		FPlatformProcess::Sleep(Retry);
	}
	else if (PType == OpenUSDConnect::Payload::Resync)
	{
		UE_LOG(LogUSDConnect, Log, TEXT("Resync received — resetting seq counter"));
		LastSeq = 0;
	}
	// Other types (HelloOk, AuthRejected) only appear during handshake, not in the loop.
}

