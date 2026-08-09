// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "EmitClient.h"

#include "USDConnectProtocol.h"
#include "USDConnectSubsystem.h"
#include "USDWireFraming.h"

#include "Logging/LogMacros.h"
#include "Sockets.h"
#include "SocketSubsystem.h"
#include "HAL/PlatformProcess.h"
#include "HAL/PlatformTime.h"
#include "Misc/ScopeLock.h"

#include "flatbuffers/flatbuffer_builder.h"

DEFINE_LOG_CATEGORY_STATIC(LogUSDEmit, Log, All);

using namespace OUC;

// ---------------------------------------------------------------------------
FEmitClient::FEmitClient(UUSDConnectSubsystem* InOwner,
                         const FString& InHost, int32 InPort,
                         const FString& InDepartment,
                         const FString& InClientId,
                         const FString& InSessionOrigin,
                         float InReconnectDelaySecs,
                         const FString& InAuthToken)
	: Owner(InOwner), Host(InHost), Port(InPort), Department(InDepartment)
	, ClientId(InClientId), SessionOrigin(InSessionOrigin), AuthToken(InAuthToken)
	, ReconnectDelaySecs(InReconnectDelaySecs)
	, Socket(nullptr), Thread(nullptr)
	, bShouldStop(false), bConnected(false), ConnectionGeneration(0)
	, SubmittedTransactionCount(0), AcknowledgedTransactionCount(0)
	, PendingTransactionCount(0), bRecoveryRequired(false)
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
	Stop();
	if (Thread)
	{
		Thread->WaitForCompletion();
		delete Thread;
		Thread = nullptr;
	}
	CloseSocket();
}

bool FEmitClient::FlushPending(double TimeoutSeconds) const
{
	const double Deadline = FPlatformTime::Seconds() + FMath::Max(0.0, TimeoutSeconds);
	while (PendingTransactionCount.load(std::memory_order_acquire) > 0
		&& !bRecoveryRequired.load(std::memory_order_acquire)
		&& FPlatformTime::Seconds() < Deadline)
	{
		FPlatformProcess::Sleep(0.005f);
	}
	return PendingTransactionCount.load(std::memory_order_acquire) == 0;
}

bool FEmitClient::Init() { return true; }

uint32 FEmitClient::Run()
{
	TArray<FQueuedProducerTxn> PendingTxns;
	int32 PendingHead = 0;
	auto RetireThrough = [this, &PendingTxns, &PendingHead](uint64 AckId)
	{
		uint64 Removed = 0;
		while (PendingHead < PendingTxns.Num()
			&& PendingTxns[PendingHead].TxnId <= AckId)
		{
			++PendingHead;
			++Removed;
		}
		if (Removed > 0)
		{
			PendingTransactionCount.fetch_sub(Removed, std::memory_order_relaxed);
			AcknowledgedTransactionCount.fetch_add(Removed, std::memory_order_relaxed);
		}
		if (PendingHead >= 1024 && PendingHead * 2 >= PendingTxns.Num())
		{
			PendingTxns.RemoveAt(0, PendingHead, EAllowShrinking::No);
			PendingHead = 0;
		}
	};
	while (!bShouldStop.load(std::memory_order_relaxed))
	{
		// --- Connect ---
		ISocketSubsystem* SS = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
		if (!SS) { FPlatformProcess::Sleep(ReconnectDelaySecs); continue; }

		FSocket* NewSocket = SS->CreateSocket(NAME_Stream, TEXT("USDConnectEmit"), false);
		if (!NewSocket) { FPlatformProcess::Sleep(ReconnectDelaySecs); continue; }
		{
			FScopeLock Lock(&SocketCS);
			Socket = NewSocket;
		}
		if (bShouldStop.load(std::memory_order_relaxed))
		{
			CloseSocket();
			break;
		}

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
		TArray<uint8> HelloFrame =
			OUC::BuildHelloFrame(
				TEXT("emitter"), 0, ClientId, SessionOrigin, Department, AuthToken, SessionOrigin);
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

			const OpenUSDConnect::Payload PType = GetEnvelopePayloadType(Frame);
			if (PType == OpenUSDConnect::Payload::AuthRejected)
			{
				UE_LOG(LogUSDEmit, Error, TEXT("Emitter auth rejected"));
				if (Owner) { Owner->OnClientAuthRejected(TEXT("emitter")); }
				CloseSocket();
				return 0;
			}
			if (PType != OpenUSDConnect::Payload::HelloOk)
			{
				CloseSocket();
				FPlatformProcess::Sleep(ReconnectDelaySecs);
				continue;
			}
			const OpenUSDConnect::Envelope* Env = GetEnvelopeFromFrame(Frame);
			const OpenUSDConnect::HelloOk* HelloOk = Env ? Env->payload_as_HelloOk() : nullptr;
			if (HelloOk)
			{
				RetireThrough(HelloOk->committed_through());
			}
			const FString IssuedToken = HelloOk ? ToFString(HelloOk->token()) : FString();
			if (Owner) { Owner->OnClientHelloOk(TEXT("emitter")); }
			if (!IssuedToken.IsEmpty())
			{
				AuthToken = IssuedToken;
				if (Owner) { Owner->OnClientTokenIssued(IssuedToken); }
			}
			UE_LOG(LogUSDEmit, Log, TEXT("Emitter HELLO_OK — ready to send"));
		}

		ConnectionGeneration.fetch_add(1, std::memory_order_relaxed);
		bConnected.store(true, std::memory_order_release);

		// --- Main loop: drain send queue + poll for incoming messages ---
		bool bShouldDisconnect = false;
		float RateLimitDelay = 0.0f;
		while (!bShouldStop.load(std::memory_order_relaxed) && !bShouldDisconnect)
		{
			// 1. Adopt queued frames into the thread-owned outbox, then send
			// every item not yet written on this connection. Items remain until
			// a committed/duplicate TransactionResult arrives.
			bool bDidWork = false;
			FQueuedProducerTxn Queued;
			while (SendQueue.Dequeue(Queued))
			{
				PendingTxns.Add(MoveTemp(Queued));
				bDidWork = true;
			}
			for (int32 PendingIndex = PendingHead;
				PendingIndex < PendingTxns.Num(); ++PendingIndex)
			{
				FQueuedProducerTxn& Pending = PendingTxns[PendingIndex];
				if (Pending.bSent) continue;
				if (!SendAll(Pending.Frame.GetData(), Pending.Frame.Num()))
				{
					UE_LOG(LogUSDEmit, Warning,
						TEXT("Emitter send failed for txn %llu"), Pending.TxnId);
					bShouldDisconnect = true;
					break;
				}
				Pending.bSent = true;
				bDidWork = true;
			}
			if (bShouldDisconnect) break;

			// 2. Drain a bounded batch of complete result/control frames. The
			// server may coalesce many individually framed acknowledgements into
			// one TCP write; consuming one frame per 5 ms loop would cap quiescent
			// acknowledgement progress at roughly 200 transactions/second.
			constexpr int32 MaxResultsPerIteration = 256;
			for (int32 ResultIndex = 0;
				ResultIndex < MaxResultsPerIteration && !bShouldDisconnect;
				++ResultIndex)
			{
				uint32 PendingBytes = 0;
				if (!Socket || !Socket->HasPendingData(PendingBytes) || PendingBytes < 4)
				{
					break;
				}
				TArray<uint8> InFrame;
				if (!RecvFrame(InFrame))
				{
					bShouldDisconnect = true;
					break;
				}
				bDidWork = true;
				const OpenUSDConnect::Envelope* Env = GetEnvelopeFromFrame(InFrame);
				if (Env && Env->payload_type() == OpenUSDConnect::Payload::TransactionResult)
				{
					const OpenUSDConnect::TransactionResult* Result =
						Env->payload_as_TransactionResult();
					if (Result)
					{
						const uint64 AckId = Result->txn_id();
						const OpenUSDConnect::TransactionStatus Status = Result->status();
						if (Status == OpenUSDConnect::TransactionStatus::Rejected)
						{
							const FString Reason = ToFString(Result->reason());
							UE_LOG(LogUSDEmit, Error,
								TEXT("Transaction %llu rejected: %s"),
								AckId, *Reason);
							bRecoveryRequired.store(true, std::memory_order_release);
							if (Owner) { Owner->OnEmitterTransactionRejected(AckId, Reason); }
							bShouldStop.store(true, std::memory_order_relaxed);
							bShouldDisconnect = true;
						}
						else
						{
							RetireThrough(AckId);
						}
					}
				}
				else if (Env && Env->payload_type() == OpenUSDConnect::Payload::RateLimited)
				{
					const OpenUSDConnect::RateLimited* RL = Env->payload_as_RateLimited();
					const float Retry = RL ? RL->retry_after() : 1.0f;
					UE_LOG(LogUSDEmit, Warning,
						TEXT("Emitter rate limited — sleeping %.1fs"), Retry);
					RateLimitDelay = Retry;
					bShouldDisconnect = true;
				}
			}

			// 3. Yield briefly if idle
			if (!bDidWork)
			{
				FPlatformProcess::Sleep(0.005f);  // 5ms
			}
		}

		bConnected.store(false, std::memory_order_relaxed);
		CloseSocket();
		for (int32 PendingIndex = PendingHead;
			PendingIndex < PendingTxns.Num(); ++PendingIndex)
		{
			PendingTxns[PendingIndex].bSent = false;
		}
		if (!bShouldStop.load(std::memory_order_relaxed))
		{
			UE_LOG(LogUSDEmit, Log,
				TEXT("Emitter disconnected — reconnecting in %.1fs"),
				ReconnectDelaySecs);
			FPlatformProcess::Sleep(
				RateLimitDelay > 0.0f ? RateLimitDelay : ReconnectDelaySecs);
		}
	}
	return 0;
}

void FEmitClient::Stop()
{
	bShouldStop.store(true, std::memory_order_relaxed);
	InterruptSocket();
}

void FEmitClient::Exit()
{
	bConnected.store(false, std::memory_order_relaxed);
	CloseSocket();
}

void FEmitClient::EnqueueFrame(uint64 TxnId, TArray<uint8>&& Frame)
{
	FQueuedProducerTxn Queued;
	Queued.TxnId = TxnId;
	Queued.Frame = MoveTemp(Frame);
	SubmittedTransactionCount.fetch_add(1, std::memory_order_relaxed);
	PendingTransactionCount.fetch_add(1, std::memory_order_relaxed);
	SendQueue.Enqueue(MoveTemp(Queued));
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------
void FEmitClient::InterruptSocket()
{
	FScopeLock Lock(&SocketCS);
	if (Socket)
	{
		Socket->Shutdown(ESocketShutdownMode::ReadWrite);
	}
}

void FEmitClient::CloseSocket()
{
	FSocket* SocketToDestroy = nullptr;
	{
		FScopeLock Lock(&SocketCS);
		SocketToDestroy = Socket;
		Socket = nullptr;
	}
	if (SocketToDestroy)
	{
		SocketToDestroy->Close();
		if (ISocketSubsystem* SS = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM))
		{
			SS->DestroySocket(SocketToDestroy);
		}
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
