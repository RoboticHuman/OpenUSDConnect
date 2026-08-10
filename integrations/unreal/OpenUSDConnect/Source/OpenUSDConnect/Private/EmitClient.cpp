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
FProducerEndpointState::FProducerEndpointState(
	const FString& InHost,
	int32 InPort,
	const FString& InDepartment,
	const FString& InSessionId)
	: Host(InHost)
	, Port(InPort)
	, Department(InDepartment)
	, SessionId(InSessionId)
{
}

bool FProducerEndpointState::MatchesEndpoint(
	const FString& InHost,
	int32 InPort,
	const FString& InDepartment) const
{
	return Host == InHost && Port == InPort && Department == InDepartment;
}

uint64 FProducerEndpointState::GetNextTransactionId() const
{
	FScopeLock Lock(&StateCS);
	return NextTransactionId;
}

bool FProducerEndpointState::EnqueueFrame(uint64 TxnId, TArray<uint8>&& Frame)
{
	if (Frame.Num() == 0) return false;
	FScopeLock Lock(&StateCS);
	if (bRecoveryRequired || TxnId != NextTransactionId) return false;

	FQueuedProducerTxn Queued;
	Queued.TxnId = TxnId;
	Queued.Frame = MakeShared<TArray<uint8>>(MoveTemp(Frame));
	PendingTxns.Add(MoveTemp(Queued));
	++NextTransactionId;
	++SubmittedTransactionCount;
	return true;
}

bool FProducerEndpointState::ClaimNextUnsent(FQueuedProducerTxn& OutTxn)
{
	FScopeLock Lock(&StateCS);
	if (bRecoveryRequired || NextUnsentIndex >= PendingTxns.Num()) return false;
	OutTxn = PendingTxns[NextUnsentIndex++];
	return true;
}

void FProducerEndpointState::MarkAllUnsent()
{
	FScopeLock Lock(&StateCS);
	NextUnsentIndex = PendingHead;
}

bool FProducerEndpointState::AcceptServerHighwater(
	uint64 CommittedThrough,
	FString& OutError)
{
	FScopeLock Lock(&StateCS);
	if (bRecoveryRequired)
	{
		OutError = RecoveryReason;
		return false;
	}

	const uint64 HighestSubmitted = NextTransactionId - 1;
	if (CommittedThrough > HighestSubmitted)
	{
		OutError = FString::Printf(
			TEXT("Server reports transaction %llu for producer session whose local highwater is %llu"),
			CommittedThrough,
			HighestSubmitted);
		bRecoveryRequired = true;
		RecoveryReason = OutError;
		RecoveryDisposition = EUSDConnectRecoveryDisposition::SessionFatal;
		return false;
	}
	if (CommittedThrough < LastAcknowledgedTxnId)
	{
		OutError = FString::Printf(
			TEXT("Server producer highwater regressed from %llu to %llu"),
			LastAcknowledgedTxnId,
			CommittedThrough);
		bRecoveryRequired = true;
		RecoveryReason = OutError;
		RecoveryDisposition = EUSDConnectRecoveryDisposition::SessionFatal;
		return false;
	}

	uint64 Removed = 0;
	while (PendingHead < PendingTxns.Num()
		&& PendingTxns[PendingHead].TxnId <= CommittedThrough)
	{
		++PendingHead;
		++Removed;
	}
	AcknowledgedTransactionCount += Removed;
	LastAcknowledgedTxnId = FMath::Max(LastAcknowledgedTxnId, CommittedThrough);
	NextUnsentIndex = FMath::Max(NextUnsentIndex, PendingHead);
	CompactLocked();
	return true;
}

void FProducerEndpointState::RetireThrough(uint64 AckId)
{
	FScopeLock Lock(&StateCS);
	uint64 Removed = 0;
	while (PendingHead < PendingTxns.Num() && PendingTxns[PendingHead].TxnId <= AckId)
	{
		++PendingHead;
		++Removed;
	}
	AcknowledgedTransactionCount += Removed;
	LastAcknowledgedTxnId = FMath::Max(LastAcknowledgedTxnId, AckId);
	NextUnsentIndex = FMath::Max(NextUnsentIndex, PendingHead);
	CompactLocked();
}

void FProducerEndpointState::MarkRejected(
	uint64 TxnId,
	uint8 RejectionCode,
	const FString& Reason)
{
	FScopeLock Lock(&StateCS);
	bRecoveryRequired = true;
	RecoveryReason = Reason.IsEmpty()
		? FString::Printf(TEXT("Transaction %llu rejected"), TxnId)
		: FString::Printf(TEXT("Transaction %llu rejected: %s"), TxnId, *Reason);
	if (RejectionCode == static_cast<uint8>(OpenUSDConnect::TransactionRejectionCode::StaleLayerGraph))
	{
		RecoveryDisposition = EUSDConnectRecoveryDisposition::RecoverableConflict;
	}
	else if (RejectionCode == static_cast<uint8>(OpenUSDConnect::TransactionRejectionCode::InvalidTransaction))
	{
		RecoveryDisposition = EUSDConnectRecoveryDisposition::InvalidOperation;
	}
	else
	{
		RecoveryDisposition = EUSDConnectRecoveryDisposition::SessionFatal;
	}
}

uint64 FProducerEndpointState::GetSubmittedTransactionCount() const
{
	FScopeLock Lock(&StateCS);
	return SubmittedTransactionCount;
}

uint64 FProducerEndpointState::GetAcknowledgedTransactionCount() const
{
	FScopeLock Lock(&StateCS);
	return AcknowledgedTransactionCount;
}

uint64 FProducerEndpointState::GetPendingTransactionCount() const
{
	FScopeLock Lock(&StateCS);
	return static_cast<uint64>(PendingTxns.Num() - PendingHead);
}

bool FProducerEndpointState::IsRecoveryRequired() const
{
	FScopeLock Lock(&StateCS);
	return bRecoveryRequired;
}

FString FProducerEndpointState::GetRecoveryReason() const
{
	FScopeLock Lock(&StateCS);
	return RecoveryReason;
}

EUSDConnectRecoveryDisposition FProducerEndpointState::GetRecoveryDisposition() const
{
	FScopeLock Lock(&StateCS);
	return RecoveryDisposition;
}

void FProducerEndpointState::CompactLocked()
{
	if (PendingHead < 1024 || PendingHead * 2 < PendingTxns.Num()) return;
	PendingTxns.RemoveAt(0, PendingHead, EAllowShrinking::No);
	NextUnsentIndex = FMath::Max(0, NextUnsentIndex - PendingHead);
	PendingHead = 0;
}

// ---------------------------------------------------------------------------
FEmitClient::FEmitClient(UUSDConnectSubsystem* InOwner,
                         const FString& InClientId,
                         const TSharedRef<FProducerEndpointState>& InProducerState,
                         float InReconnectDelaySecs,
	const FString& InAuthToken)
	: Owner(InOwner), ProducerState(InProducerState)
	, ClientId(InClientId)
	, AuthToken(InAuthToken)
	, ReconnectDelaySecs(InReconnectDelaySecs)
	, Socket(nullptr), Thread(nullptr)
	, bShouldStop(false), bConnected(false), ConnectionGeneration(0)
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
	while (ProducerState->GetPendingTransactionCount() > 0
		&& !ProducerState->IsRecoveryRequired()
		&& FPlatformTime::Seconds() < Deadline)
	{
		FPlatformProcess::Sleep(0.005f);
	}
	return ProducerState->GetPendingTransactionCount() == 0
		&& !ProducerState->IsRecoveryRequired();
}

bool FEmitClient::Init() { return true; }

uint32 FEmitClient::Run()
{
	const FString& Host = ProducerState->GetHost();
	const int32 Port = ProducerState->GetPort();
	const FString& Department = ProducerState->GetDepartment();
	const FString& SessionOrigin = ProducerState->GetSessionId();
	ProducerState->MarkAllUnsent();
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
				FString HighwaterError;
				if (!ProducerState->AcceptServerHighwater(
					HelloOk->committed_through(), HighwaterError))
				{
					UE_LOG(LogUSDEmit, Error, TEXT("Producer recovery required: %s"), *HighwaterError);
					if (Owner)
					{
						Owner->OnEmitterTransactionRejected(
							HelloOk->committed_through(), HighwaterError);
					}
					CloseSocket();
					return 0;
				}
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
			// 1. Claim and send every endpoint-state transaction not yet written
			// on this connection. Items remain owned by ProducerState until a
			// committed/duplicate TransactionResult arrives.
			bool bDidWork = false;
			FQueuedProducerTxn Pending;
			while (ProducerState->ClaimNextUnsent(Pending))
			{
				if (!Pending.Frame.IsValid()
					|| !SendAll(Pending.Frame->GetData(), Pending.Frame->Num()))
				{
					UE_LOG(LogUSDEmit, Warning,
						TEXT("Emitter send failed for txn %llu"), Pending.TxnId);
					bShouldDisconnect = true;
					break;
				}
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
							ProducerState->MarkRejected(
								AckId,
								static_cast<uint8>(Result->rejection_code()),
								Reason);
							if (Owner) { Owner->OnEmitterTransactionRejected(AckId, Reason); }
							bShouldStop.store(true, std::memory_order_relaxed);
							bShouldDisconnect = true;
						}
						else
						{
							ProducerState->RetireThrough(AckId);
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
		ProducerState->MarkAllUnsent();
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

bool FEmitClient::EnqueueFrame(uint64 TxnId, TArray<uint8>&& Frame)
{
	return ProducerState->EnqueueFrame(TxnId, MoveTemp(Frame));
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
