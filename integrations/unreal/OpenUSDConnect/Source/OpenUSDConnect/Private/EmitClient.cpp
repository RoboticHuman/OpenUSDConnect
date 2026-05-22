// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "EmitClient.h"

#include "USDConnectProtocol.h"

#include "Logging/LogMacros.h"
#include "Sockets.h"
#include "SocketSubsystem.h"
#include "HAL/PlatformProcess.h"

#include "flatbuffers/flatbuffer_builder.h"

DEFINE_LOG_CATEGORY_STATIC(LogUSDEmit, Log, All);

using namespace OUC;

// ---------------------------------------------------------------------------
FEmitClient::FEmitClient(const FString& InHost, int32 InPort,
                         const FString& InDepartment,
                         const FString& InClientId,
                         const FString& InSessionOrigin,
                         float InReconnectDelaySecs)
	: Host(InHost), Port(InPort), Department(InDepartment)
	, ClientId(InClientId), SessionOrigin(InSessionOrigin)
	, ReconnectDelaySecs(InReconnectDelaySecs)
	, Socket(nullptr), Thread(nullptr)
	, bShouldStop(false), bConnected(false)
{
}

FEmitClient::~FEmitClient() { StopAndWait(); }

bool FEmitClient::Start()
{
	Thread = FRunnableThread::Create(this, TEXT("OpenUSDConnect_EmitClient"),
	                                 0, TPri_BelowNormal);
	return Thread != nullptr;
}

void FEmitClient::StopAndWait()
{
	bShouldStop.store(true, std::memory_order_relaxed);
	CloseSocket();
	if (Thread)
	{
		Thread->WaitForCompletion();
		delete Thread;
		Thread = nullptr;
	}
}

bool FEmitClient::Init() { return true; }

uint32 FEmitClient::Run()
{
	while (!bShouldStop.load(std::memory_order_relaxed))
	{
		// --- Connect ---
		ISocketSubsystem* SS = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
		if (!SS) { FPlatformProcess::Sleep(ReconnectDelaySecs); continue; }

		Socket = SS->CreateSocket(NAME_Stream, TEXT("USDConnectEmit"), false);
		if (!Socket) { FPlatformProcess::Sleep(ReconnectDelaySecs); continue; }

		TSharedRef<FInternetAddr> Addr = SS->CreateInternetAddr();
		bool bValid = false;
		Addr->SetIp(*Host, bValid);
		Addr->SetPort(Port);
		if (!bValid || !Socket->Connect(*Addr))
		{
			CloseSocket();
			FPlatformProcess::Sleep(ReconnectDelaySecs);
			continue;
		}
		UE_LOG(LogUSDEmit, Log, TEXT("Emitter connected to %s:%d"), *Host, Port);

		// --- Send HELLO as emitter ---
		TArray<uint8> HelloFrame = BuildHelloFrame();
		if (!SendAll(HelloFrame.GetData(), HelloFrame.Num()))
		{
			CloseSocket();
			FPlatformProcess::Sleep(ReconnectDelaySecs);
			continue;
		}

		// --- Read HELLO_OK (blocking) ---
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
				UE_LOG(LogUSDEmit, Error, TEXT("Emitter auth rejected"));
				CloseSocket();
				return 0;
			}
			if (PType != kPayloadHelloOk)
			{
				CloseSocket();
				FPlatformProcess::Sleep(ReconnectDelaySecs);
				continue;
			}
			UE_LOG(LogUSDEmit, Log, TEXT("Emitter HELLO_OK — ready to send"));
		}

		bConnected.store(true, std::memory_order_relaxed);

		// --- Main loop: drain send queue + poll for incoming messages ---
		bool bShouldDisconnect = false;
		while (!bShouldStop.load(std::memory_order_relaxed) && !bShouldDisconnect)
		{
			// 1. Drain outbound queue
			bool bSentAny = false;
			TArray<uint8> SendFrame;
			while (SendQueue.Dequeue(SendFrame))
			{
				if (!SendAll(SendFrame.GetData(), SendFrame.Num()))
				{
					UE_LOG(LogUSDEmit, Warning, TEXT("Emitter send failed"));
					bShouldDisconnect = true;
					break;
				}
				bSentAny = true;
			}
			if (bShouldDisconnect) break;

			// 2. Poll for incoming data with HasPendingData (non-destructive peek).
			//    Socket stays blocking — we only read when at least 4 bytes are ready.
			uint32 Pending = 0;
			if (Socket && Socket->HasPendingData(Pending) && Pending >= 4)
			{
				TArray<uint8> InFrame;
				if (!RecvFrame(InFrame))
				{
					bShouldDisconnect = true;
					break;
				}
				const uint8 PType = GetPayloadType(InFrame);
				if (PType == kPayloadRateLimited)
				{
					const uint8* Env = FB::GetRoot(InFrame);
					const uint8* RL  = Env ? FB::GetPtr(Env, VT::EnvPayload) : nullptr;
					const float Retry = RL ? FB::GetField<float>(RL, VT::RLRetryAfter, 1.0f) : 1.0f;
					UE_LOG(LogUSDEmit, Warning,
						TEXT("Emitter rate limited — sleeping %.1fs"), Retry);
					FPlatformProcess::Sleep(Retry);
				}
				// Other inbound (Ping, BroadcastEvent corrections) — ignore for MVP.
			}

			// 3. Yield briefly if idle
			if (!bSentAny)
			{
				FPlatformProcess::Sleep(0.005f);  // 5ms
			}
		}

		bConnected.store(false, std::memory_order_relaxed);
		CloseSocket();
		if (!bShouldStop.load(std::memory_order_relaxed))
		{
			UE_LOG(LogUSDEmit, Log,
				TEXT("Emitter disconnected — reconnecting in %.1fs"),
				ReconnectDelaySecs);
			FPlatformProcess::Sleep(ReconnectDelaySecs);
		}
	}
	return 0;
}

void FEmitClient::Stop()
{
	bShouldStop.store(true, std::memory_order_relaxed);
	CloseSocket();
}

void FEmitClient::Exit()
{
	bConnected.store(false, std::memory_order_relaxed);
	CloseSocket();
}

void FEmitClient::EnqueueFrame(TArray<uint8>&& Frame)
{
	SendQueue.Enqueue(MoveTemp(Frame));
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------
void FEmitClient::CloseSocket()
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

bool FEmitClient::RecvExact(uint8* Buf, int32 Needed)
{
	int32 Got = 0;
	while (Got < Needed)
	{
		if (bShouldStop.load(std::memory_order_relaxed) || !Socket) return false;
		int32 Read = 0;
		if (!Socket->Recv(Buf + Got, Needed - Got, Read) || Read <= 0) return false;
		Got += Read;
	}
	return true;
}

bool FEmitClient::RecvFrame(TArray<uint8>& OutFrame)
{
	uint8 LenBuf[4];
	if (!RecvExact(LenBuf, 4)) return false;
	const uint32 PayloadLen =
		((uint32)LenBuf[0] << 24) | ((uint32)LenBuf[1] << 16) |
		((uint32)LenBuf[2] << 8)  |  (uint32)LenBuf[3];
	if (PayloadLen == 0 || PayloadLen > kMaxFrameSize) return false;
	OutFrame.SetNumUninitialized(static_cast<int32>(PayloadLen));
	return RecvExact(OutFrame.GetData(), static_cast<int32>(PayloadLen));
}

bool FEmitClient::SendAll(const uint8* Data, int32 Len)
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

uint8 FEmitClient::GetPayloadType(const TArray<uint8>& Bytes) const
{
	if (Bytes.Num() < 8) return 0;
	const uint8* Root = FB::GetRoot(Bytes);
	return Root ? FB::GetField<uint8>(Root, VT::EnvPayloadType, 0) : 0;
}

TArray<uint8> FEmitClient::BuildHelloFrame() const
{
	flatbuffers::FlatBufferBuilder Builder(512);

	auto RoleOff       = Builder.CreateString("emitter");
	auto ClientIdOff   = Builder.CreateString(TCHAR_TO_UTF8(*ClientId));
	auto OriginOff     = Builder.CreateString(TCHAR_TO_UTF8(*SessionOrigin));
	auto DepartmentOff = Builder.CreateString(TCHAR_TO_UTF8(*Department));
	auto TokenOff      = Builder.CreateString("");

	const flatbuffers::uoffset_t HelloStart = Builder.StartTable();
	Builder.AddOffset(VT::HelloRole,        RoleOff);
	Builder.AddElement<int32_t>(VT::HelloProtoVer, 1, 0);
	Builder.AddElement<int32_t>(VT::HelloSyncFrom, 0, 0);
	Builder.AddOffset(VT::HelloClientId,    ClientIdOff);
	Builder.AddOffset(VT::HelloOrigin,      OriginOff);
	Builder.AddOffset(VT::HelloDepartment,  DepartmentOff);
	Builder.AddOffset(VT::HelloToken,       TokenOff);
	const flatbuffers::uoffset_t HelloOff = Builder.EndTable(HelloStart);

	const flatbuffers::uoffset_t EnvStart = Builder.StartTable();
	Builder.AddElement<uint8_t>(VT::EnvPayloadType, kPayloadHello, 0);
	Builder.AddOffset(VT::EnvPayload, flatbuffers::Offset<void>(HelloOff));
	const flatbuffers::uoffset_t EnvOff = Builder.EndTable(EnvStart);
	Builder.Finish(flatbuffers::Offset<void>(EnvOff));

	const uint8_t* FBData = Builder.GetBufferPointer();
	const size_t   FBSize = Builder.GetSize();

	TArray<uint8> Frame;
	Frame.SetNumUninitialized(4 + static_cast<int32>(FBSize));
	Frame[0] = static_cast<uint8>((FBSize >> 24) & 0xFF);
	Frame[1] = static_cast<uint8>((FBSize >> 16) & 0xFF);
	Frame[2] = static_cast<uint8>((FBSize >>  8) & 0xFF);
	Frame[3] = static_cast<uint8>( FBSize        & 0xFF);
	FMemory::Memcpy(Frame.GetData() + 4, FBData, FBSize);
	return Frame;
}
