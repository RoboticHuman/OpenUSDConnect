// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "SyncClient.h"

#include "USDConnectProtocol.h"
#include "USDConnectSubsystem.h"

#include "Logging/LogMacros.h"
#include "Sockets.h"
#include "SocketSubsystem.h"
#include "Interfaces/IPv4/IPv4Address.h"
#include "HAL/PlatformProcess.h"

// FlatBuffers builder for HELLO encoding (read-only side uses OUC::FB::*)
#include "flatbuffers/flatbuffer_builder.h"

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
                         float InReconnectDelaySecs)
	: Owner(InOwner)
	, Host(InHost)
	, Port(InPort)
	, Department(InDepartment)
	, ClientId(InClientId)
	, SessionOrigin(InSessionOrigin)
	, ReconnectDelaySecs(InReconnectDelaySecs)
	, LastSeq(0)
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
			UE_LOG(LogUSDConnect, Log,
				TEXT("Could not connect to %s:%d — retrying in %.1fs"),
				*Host, Port, ReconnectDelaySecs);
			CloseSocket();
			FPlatformProcess::Sleep(ReconnectDelaySecs);
			continue;
		}
		UE_LOG(LogUSDConnect, Log,
			TEXT("Connected to OpenUSDConnect server at %s:%d (receiver)"), *Host, Port);

		// --- Send HELLO ---
		TArray<uint8> HelloFrame = BuildHelloFrame();
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

			const uint8 PType = GetPayloadType(Frame);
			if (PType == kPayloadAuthRejected)
			{
				UE_LOG(LogUSDConnect, Error, TEXT("OpenUSDConnect: auth rejected by server"));
				CloseSocket();
				return 0;
			}
			if (PType != kPayloadHelloOk)
			{
				UE_LOG(LogUSDConnect, Warning,
					TEXT("Unexpected response to HELLO (type=%u)"), PType);
				CloseSocket();
				FPlatformProcess::Sleep(ReconnectDelaySecs);
				continue;
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

uint8 FSyncClient::GetPayloadType(const TArray<uint8>& Bytes) const
{
	if (Bytes.Num() < 8) return 0;
	const uint8* Root = FB::GetRoot(Bytes);
	if (!Root) return 0;
	return FB::GetField<uint8>(Root, VT::Envelope_PayloadType, 0);
}

void FSyncClient::HandleFrame(const TArray<uint8>& Frame)
{
	const uint8 PType = GetPayloadType(Frame);

	if (PType == kPayloadBroadcastEvent)
	{
		const uint8* Env     = FB::GetRoot(Frame);
		const uint8* BcEvent = Env ? FB::GetPtr(Env, VT::Envelope_Payload) : nullptr;
		if (BcEvent)
		{
			const int32 Seq = FB::GetField<int32>(BcEvent, VT::BroadcastEvent_Seq, 0);
			if (Seq > LastSeq) LastSeq = Seq;

			// Echo suppression: skip events we originated (server broadcasts them back).
			const FString Origin = FB::GetStr(BcEvent, VT::BroadcastEvent_Origin);
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
	else if (PType == kPayloadPing)
	{
		// Heartbeat — receivers ignore (server is just checking the connection).
	}
	else if (PType == kPayloadRateLimited)
	{
		const uint8* Env = FB::GetRoot(Frame);
		const uint8* RL  = Env ? FB::GetPtr(Env, VT::Envelope_Payload) : nullptr;
		const float Retry = RL ? FB::GetField<float>(RL, VT::RateLimited_RetryAfter, 1.0f) : 1.0f;
		UE_LOG(LogUSDConnect, Warning,
			TEXT("Rate limited — sleeping %.1fs"), Retry);
		FPlatformProcess::Sleep(Retry);
	}
	else if (PType == kPayloadResync)
	{
		UE_LOG(LogUSDConnect, Log, TEXT("Resync received — resetting seq counter"));
		LastSeq = 0;
	}
	// Other types (HelloOk, AuthRejected) only appear during handshake, not in the loop.
}

// ---------------------------------------------------------------------------
// HELLO message builder
//
// Wire layout produced:
//   Envelope { payload_type=1 (Hello); payload=Hello{...} }
// ---------------------------------------------------------------------------
TArray<uint8> FSyncClient::BuildHelloFrame() const
{
	flatbuffers::FlatBufferBuilder Builder(512);

	// Strings (built bottom-up before the tables that reference them)
	auto RoleOff       = Builder.CreateString("receiver");
	auto ClientIdOff   = Builder.CreateString(TCHAR_TO_UTF8(*ClientId));
	auto OriginOff     = Builder.CreateString(TCHAR_TO_UTF8(*SessionOrigin));
	auto DepartmentOff = Builder.CreateString(TCHAR_TO_UTF8(*Department));
	auto TokenOff      = Builder.CreateString("");  // empty = first-connect TOFU

	// Hello table
	const flatbuffers::uoffset_t HelloStart = Builder.StartTable();
	Builder.AddOffset(VT::Hello_Role,        RoleOff);
	Builder.AddElement<int32_t>(VT::Hello_ProtocolVersion, 1,        0);
	Builder.AddElement<int32_t>(VT::Hello_SyncFrom, LastSeq,  0);
	Builder.AddOffset(VT::Hello_ClientId,    ClientIdOff);
	Builder.AddOffset(VT::Hello_Origin,      OriginOff);
	Builder.AddOffset(VT::Hello_Department,  DepartmentOff);
	Builder.AddOffset(VT::Hello_Token,       TokenOff);
	const flatbuffers::uoffset_t HelloOff = Builder.EndTable(HelloStart);

	// Envelope table (schema_version defaults to 1 — omit to use default)
	const flatbuffers::uoffset_t EnvStart = Builder.StartTable();
	Builder.AddElement<uint8_t>(VT::Envelope_PayloadType, kPayloadHello, 0);
	Builder.AddOffset(VT::Envelope_Payload, flatbuffers::Offset<void>(HelloOff));
	const flatbuffers::uoffset_t EnvOff = Builder.EndTable(EnvStart);

	Builder.Finish(flatbuffers::Offset<void>(EnvOff));

	const uint8_t* FBData = Builder.GetBufferPointer();
	const size_t   FBSize = Builder.GetSize();

	// Prepend 4-byte big-endian length prefix (matches Python server.framing)
	TArray<uint8> Frame;
	Frame.SetNumUninitialized(4 + static_cast<int32>(FBSize));
	Frame[0] = static_cast<uint8>((FBSize >> 24) & 0xFF);
	Frame[1] = static_cast<uint8>((FBSize >> 16) & 0xFF);
	Frame[2] = static_cast<uint8>((FBSize >>  8) & 0xFF);
	Frame[3] = static_cast<uint8>( FBSize        & 0xFF);
	FMemory::Memcpy(Frame.GetData() + 4, FBData, FBSize);
	return Frame;
}
