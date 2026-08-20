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

namespace
{
constexpr double FrameReadTimeoutSeconds = 10.0;
constexpr size_t MaxPendingTransactions = 10'000;

bool IsPeerClosed(FSocket* Socket)
{
	if (!Socket)
	{
		return true;
	}

	uint32 PendingBytes = 0;
	if (Socket->HasPendingData(PendingBytes))
	{
		return false;
	}

	// An orderly TCP close becomes readable with no payload. HasPendingData()
	// alone cannot distinguish that state from an idle connection on Windows.
	if (!Socket->Wait(ESocketWaitConditions::WaitForRead, FTimespan::Zero()))
	{
		return false;
	}

	uint8 Probe = 0;
	int32 BytesRead = 0;
	return !Socket->Recv(&Probe, 1, BytesRead, ESocketReceiveFlags::Peek) || BytesRead == 0;
}
} // namespace

// ---------------------------------------------------------------------------
FProducerEndpointState::FProducerEndpointState(const FString& InHost, int32 InPort,
											   const FString& InDepartment,
											   const FString& InSessionId)
	: Host(InHost)
	, Port(InPort)
	, Department(InDepartment)
	, SessionId(InSessionId)
	, Session(MaxPendingTransactions)
{
}

bool FProducerEndpointState::MatchesEndpoint(const FString& InHost, int32 InPort,
											 const FString& InDepartment) const
{
	return Host == InHost && Port == InPort && Department == InDepartment;
}

uint64 FProducerEndpointState::GetNextTransactionId() const
{
	return Session.NextTransactionId();
}

bool FProducerEndpointState::BeginConnection(uint64& OutGeneration)
{
	const std::optional<openusdconnect::client::ProducerConnectionStart> Start =
		Session.BeginConnection();
	if (!Start.has_value())
	{
		return false;
	}
	OutGeneration = Start->Generation;
	return true;
}

void FProducerEndpointState::Disconnect(uint64 Generation)
{
	Session.Disconnect(Generation);
}

bool FProducerEndpointState::EnqueueFrame(uint64 Generation, uint64 TxnId, FWireFrame&& Frame)
{
	if (Frame.IsEmpty())
	{
		return false;
	}
	FProducerFrame SharedFrame = MakeShared<FWireFrame, ESPMode::ThreadSafe>(MoveTemp(Frame));
	return Session.Append(Generation, TxnId, MoveTemp(SharedFrame), 1, {}) ==
		   openusdconnect::client::ProducerResult::Accepted;
}

bool FProducerEndpointState::ClaimNextUnsent(uint64 Generation, FQueuedProducerTxn& OutTxn)
{
	FProducerSession::Entry Entry;
	if (Session.ClaimNextUnsent(Generation, Entry) !=
		openusdconnect::client::ProducerResult::Accepted)
	{
		return false;
	}
	OutTxn.TxnId = Entry.TransactionId;
	OutTxn.Frame = std::move(Entry.Payload);
	return true;
}

bool FProducerEndpointState::AcceptServerHighwater(uint64 Generation, uint64 CommittedThrough,
												   FString& OutError)
{
	const openusdconnect::client::ProducerResult Result =
		Session.AcceptHello(Generation, CommittedThrough);
	if (Result == openusdconnect::client::ProducerResult::Accepted)
	{
		return true;
	}

	if (Result == openusdconnect::client::ProducerResult::HighwaterAhead)
	{
		OutError = FString::Printf(TEXT("Server reports transaction %llu for producer session "
										"whose local highwater is %llu"),
								   CommittedThrough, Session.NextTransactionId() - 1);
	}
	else if (Result == openusdconnect::client::ProducerResult::HighwaterRegressed)
	{
		OutError = FString::Printf(TEXT("Server producer highwater regressed from %llu to %llu"),
								   Session.LastAcknowledgedTransactionId(), CommittedThrough);
	}
	else
	{
		OutError = TEXT("Producer session rejected HELLO_OK in its current state");
	}
	FScopeLock Lock(&RecoveryCS);
	RecoveryReason = OutError;
	return false;
}

void FProducerEndpointState::RetireThrough(uint64 Generation, uint64 AckId)
{
	const openusdconnect::client::ProducerResult Result =
		Session.AcknowledgeThrough(Generation, AckId);
	if (Result != openusdconnect::client::ProducerResult::Accepted &&
		Result != openusdconnect::client::ProducerResult::StaleGeneration)
	{
		FScopeLock Lock(&RecoveryCS);
		RecoveryReason = FString::Printf(
			TEXT("Invalid cumulative acknowledgement through transaction %llu"), AckId);
	}
}

void FProducerEndpointState::MarkRejected(uint64 Generation, uint64 TxnId, uint8 RejectionCode,
										  const FString& Reason)
{
	openusdconnect::client::ProducerRecoveryDisposition Disposition;
	if (RejectionCode ==
		static_cast<uint8>(OpenUSDConnect::TransactionRejectionCode::StaleLayerGraph))
	{
		Disposition = openusdconnect::client::ProducerRecoveryDisposition::RecoverableConflict;
	}
	else if (RejectionCode ==
			 static_cast<uint8>(OpenUSDConnect::TransactionRejectionCode::InvalidTransaction))
	{
		Disposition = openusdconnect::client::ProducerRecoveryDisposition::InvalidOperation;
	}
	else
	{
		Disposition = openusdconnect::client::ProducerRecoveryDisposition::SessionFatal;
	}
	Session.Reject(Generation, TxnId, Disposition);
	FScopeLock Lock(&RecoveryCS);
	RecoveryReason = Reason.IsEmpty()
						 ? FString::Printf(TEXT("Transaction %llu rejected"), TxnId)
						 : FString::Printf(TEXT("Transaction %llu rejected: %s"), TxnId, *Reason);
}

uint64 FProducerEndpointState::GetSubmittedTransactionCount() const
{
	return Session.SubmittedTransactionCount();
}

uint64 FProducerEndpointState::GetAcknowledgedTransactionCount() const
{
	return Session.AcknowledgedTransactionCount();
}

uint64 FProducerEndpointState::GetPendingTransactionCount() const
{
	return static_cast<uint64>(Session.PendingTransactionCount());
}

bool FProducerEndpointState::IsRecoveryRequired() const
{
	return Session.RecoveryRequired();
}

FString FProducerEndpointState::GetRecoveryReason() const
{
	FScopeLock Lock(&RecoveryCS);
	return RecoveryReason;
}

EUSDConnectRecoveryDisposition FProducerEndpointState::GetRecoveryDisposition() const
{
	switch (Session.RecoveryDisposition())
	{
	case openusdconnect::client::ProducerRecoveryDisposition::RecoverableConflict:
		return EUSDConnectRecoveryDisposition::RecoverableConflict;
	case openusdconnect::client::ProducerRecoveryDisposition::InvalidOperation:
		return EUSDConnectRecoveryDisposition::InvalidOperation;
	case openusdconnect::client::ProducerRecoveryDisposition::SessionFatal:
		return EUSDConnectRecoveryDisposition::SessionFatal;
	default:
		return EUSDConnectRecoveryDisposition::None;
	}
}

// ---------------------------------------------------------------------------
FEmitClient::FEmitClient(UUSDConnectSubsystem* InOwner, const FString& InClientId,
						 const TSharedRef<FProducerEndpointState>& InProducerState,
						 float InReconnectDelaySecs, const FString& InAuthToken)
	: Owner(InOwner)
	, ProducerState(InProducerState)
	, ClientId(InClientId)
	, AuthToken(InAuthToken)
	, ReconnectDelaySecs(InReconnectDelaySecs)
	, Socket(nullptr)
	, Thread(nullptr)
	, WorkEvent(FPlatformProcess::GetSynchEventFromPool(false))
	, bShouldStop(false)
	, bConnected(false)
	, ConnectionGeneration(0)
	, SessionGeneration(0)
{
}

FEmitClient::~FEmitClient()
{
	StopAndWait();
	if (WorkEvent)
	{
		FPlatformProcess::ReturnSynchEventToPool(WorkEvent);
		WorkEvent = nullptr;
	}
}

bool FEmitClient::Start()
{
	Thread = FRunnableThread::Create(this, TEXT("OpenUSDConnect_EmitClient"), 0, TPri_BelowNormal);
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
	while (ProducerState->GetPendingTransactionCount() > 0 &&
		   !ProducerState->IsRecoveryRequired() && FPlatformTime::Seconds() < Deadline)
	{
		FPlatformProcess::Sleep(0.005f);
	}
	return ProducerState->GetPendingTransactionCount() == 0 && !ProducerState->IsRecoveryRequired();
}

bool FEmitClient::Init()
{
	return true;
}

uint32 FEmitClient::Run()
{
	const FString& Host = ProducerState->GetHost();
	const int32 Port = ProducerState->GetPort();
	const FString& Department = ProducerState->GetDepartment();
	const FString& SessionOrigin = ProducerState->GetSessionId();
	while (!bShouldStop.load(std::memory_order_relaxed))
	{
		uint64 ActiveGeneration = 0;
		if (!ProducerState->BeginConnection(ActiveGeneration))
		{
			return 0;
		}

		// --- Connect ---
		ISocketSubsystem* SS = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
		if (!SS)
		{
			ProducerState->Disconnect(ActiveGeneration);
			FPlatformProcess::Sleep(ReconnectDelaySecs);
			continue;
		}

		FSocket* NewSocket = SS->CreateSocket(NAME_Stream, TEXT("USDConnectEmit"), false);
		if (!NewSocket)
		{
			ProducerState->Disconnect(ActiveGeneration);
			FPlatformProcess::Sleep(ReconnectDelaySecs);
			continue;
		}
		{
			FScopeLock Lock(&SocketCS);
			Socket = NewSocket;
		}
		if (bShouldStop.load(std::memory_order_relaxed))
		{
			CloseSocket();
			ProducerState->Disconnect(ActiveGeneration);
			break;
		}

		TSharedRef<FInternetAddr> Addr = SS->CreateInternetAddr();
		bool bValid = false;
		Addr->SetIp(*Host, bValid);
		Addr->SetPort(Port);
		if (!bValid || !Socket->Connect(*Addr))
		{
			CloseSocket();
			ProducerState->Disconnect(ActiveGeneration);
			FPlatformProcess::Sleep(ReconnectDelaySecs);
			continue;
		}
		UE_LOG(LogUSDEmit, Log, TEXT("Emitter connected to %s:%d"), *Host, Port);

		// --- Send HELLO as emitter ---
		FWireFrame HelloFrame;
		const openusdconnect::client::FrameResult HelloResult =
			OUC::BuildHelloFrame(TEXT("emitter"), 0, ClientId, SessionOrigin, Department,
								 HelloFrame, AuthToken, SessionOrigin);
		if (HelloResult != openusdconnect::client::FrameResult::Success ||
			!SendAll(HelloFrame.GetData(), HelloFrame.Num()))
		{
			CloseSocket();
			ProducerState->Disconnect(ActiveGeneration);
			FPlatformProcess::Sleep(ReconnectDelaySecs);
			continue;
		}

		// --- Read HELLO_OK (blocking) ---
		{
			TArray<uint8> Frame;
			if (!RecvFrame(Frame))
			{
				CloseSocket();
				ProducerState->Disconnect(ActiveGeneration);
				FPlatformProcess::Sleep(ReconnectDelaySecs);
				continue;
			}

			openusdconnect::client::EnvelopeView Envelope;
			if (openusdconnect::client::DecodeEnvelope(
					Frame.GetData(), static_cast<size_t>(Frame.Num()), Envelope) !=
				openusdconnect::client::ProtocolResult::Success)
			{
				CloseSocket();
				ProducerState->Disconnect(ActiveGeneration);
				FPlatformProcess::Sleep(ReconnectDelaySecs);
				continue;
			}
			const openusdconnect::client::HandshakeResponseView Response(Envelope);
			if (Response.Kind() ==
				openusdconnect::client::HandshakeResponseKind::AuthenticationRejected)
			{
				UE_LOG(LogUSDEmit, Error, TEXT("Emitter auth rejected"));
				if (Owner)
				{
					Owner->OnClientAuthRejected(TEXT("emitter"));
				}
				CloseSocket();
				ProducerState->Disconnect(ActiveGeneration);
				return 0;
			}
			if (Response.Kind() ==
				openusdconnect::client::HandshakeResponseKind::ConfigurationRejected)
			{
				const OpenUSDConnect::HelloRejected* Rejection = Response.ConfigurationRejection();
				const OpenUSDConnect::HelloRejectionCode Code = Rejection->code();
				const FString CodeName =
					Code == OpenUSDConnect::HelloRejectionCode::Unspecified
						? FString()
						: FString(UTF8_TO_TCHAR(OpenUSDConnect::EnumNameHelloRejectionCode(Code)));
				const FString Reason = ToFString(Rejection->reason());
				if (Owner)
				{
					Owner->OnClientHelloRejected(TEXT("emitter"), CodeName, Reason);
				}
				CloseSocket();
				ProducerState->Disconnect(ActiveGeneration);
				return 0;
			}
			if (Response.Kind() != openusdconnect::client::HandshakeResponseKind::Accepted)
			{
				CloseSocket();
				ProducerState->Disconnect(ActiveGeneration);
				FPlatformProcess::Sleep(ReconnectDelaySecs);
				continue;
			}
			const OpenUSDConnect::HelloOk* HelloOk = Response.Accepted();
			FString HighwaterError;
			if (!ProducerState->AcceptServerHighwater(ActiveGeneration,
													  HelloOk->committed_through(), HighwaterError))
			{
				UE_LOG(LogUSDEmit, Error, TEXT("Producer recovery required: %s"), *HighwaterError);
				if (Owner)
				{
					Owner->OnEmitterTransactionRejected(HelloOk->committed_through(),
														HighwaterError);
				}
				CloseSocket();
				ProducerState->Disconnect(ActiveGeneration);
				return 0;
			}
			const FString IssuedToken = ToFString(HelloOk->token());
			if (Owner)
			{
				Owner->OnClientHelloOk(TEXT("emitter"));
			}
			if (!IssuedToken.IsEmpty())
			{
				AuthToken = IssuedToken;
				if (Owner)
				{
					Owner->OnClientTokenIssued(IssuedToken);
				}
			}
			UE_LOG(LogUSDEmit, Log, TEXT("Emitter HELLO_OK ready to send"));
		}

		ConnectionGeneration.fetch_add(1, std::memory_order_relaxed);
		SessionGeneration.store(ActiveGeneration, std::memory_order_relaxed);
		bConnected.store(true, std::memory_order_release);

		// --- Main loop: drain send queue + poll for incoming messages ---
		bool bShouldDisconnect = false;
		float RateLimitDelay = 0.0f;
		while (!bShouldStop.load(std::memory_order_relaxed) && !bShouldDisconnect)
		{
			if (IsPeerClosed(Socket))
			{
				bShouldDisconnect = true;
				break;
			}

			// 1. Claim and send every endpoint-state transaction not yet written
			// on this connection. Items remain owned by ProducerState until a
			// committed/duplicate TransactionResult arrives.
			bool bDidWork = false;
			FQueuedProducerTxn Pending;
			while (ProducerState->ClaimNextUnsent(ActiveGeneration, Pending))
			{
				if (!SendAll(Pending.Frame->GetData(), Pending.Frame->Num()))
				{
					UE_LOG(LogUSDEmit, Warning, TEXT("Emitter send failed for txn %llu"),
						   Pending.TxnId);
					bShouldDisconnect = true;
					break;
				}
				bDidWork = true;
			}
			if (bShouldDisconnect)
				break;

			// 2. Drain a bounded batch of complete result/control frames. The
			// server may coalesce many individually framed acknowledgements into
			// one TCP write; consuming one frame per 5 ms loop would cap quiescent
			// acknowledgement progress at roughly 200 transactions/second.
			constexpr int32 MaxResultsPerIteration = 256;
			for (int32 ResultIndex = 0; ResultIndex < MaxResultsPerIteration && !bShouldDisconnect;
				 ++ResultIndex)
			{
				uint32 PendingBytes = 0;
				if (!Socket || !Socket->HasPendingData(PendingBytes) || PendingBytes == 0)
				{
					break;
				}
				// RecvFrame handles fragmented headers and bodies. Enter it as
				// soon as any byte is readable so a peer close after a partial
				// header is observed instead of leaving this connection stuck.
				TArray<uint8> InFrame;
				if (!RecvFrame(InFrame))
				{
					bShouldDisconnect = true;
					break;
				}
				bDidWork = true;
				openusdconnect::client::EnvelopeView Envelope;
				if (openusdconnect::client::DecodeEnvelope(
						InFrame.GetData(), static_cast<size_t>(InFrame.Num()), Envelope) !=
					openusdconnect::client::ProtocolResult::Success)
				{
					bShouldDisconnect = true;
					break;
				}
				const openusdconnect::client::ControlMessageView Message(Envelope);
				if (Message.Kind() == openusdconnect::client::ControlMessageKind::TransactionResult)
				{
					const OpenUSDConnect::TransactionResult* Result = Message.TransactionResult();
					const uint64 AckId = Result->txn_id();
					const OpenUSDConnect::TransactionStatus Status = Result->status();
					if (Status == OpenUSDConnect::TransactionStatus::Rejected)
					{
						const FString Reason = ToFString(Result->reason());
						UE_LOG(LogUSDEmit, Error, TEXT("Transaction %llu rejected: %s"), AckId,
							   *Reason);
						ProducerState->MarkRejected(ActiveGeneration, AckId,
													static_cast<uint8>(Result->rejection_code()),
													Reason);
						if (Owner)
						{
							Owner->OnEmitterTransactionRejected(AckId, Reason);
						}
						bShouldStop.store(true, std::memory_order_relaxed);
						bShouldDisconnect = true;
					}
					else
					{
						ProducerState->RetireThrough(ActiveGeneration, AckId);
					}
				}
				else if (Message.Kind() == openusdconnect::client::ControlMessageKind::RateLimited)
				{
					const float Retry = Message.RateLimit()->retry_after();
					UE_LOG(LogUSDEmit, Warning, TEXT("Emitter rate limited sleeping %.1fs"), Retry);
					RateLimitDelay = Retry;
					bShouldDisconnect = true;
				}
			}

			// 3. Sleep until a producer enqueue wakes us. While acknowledgements
			// are outstanding, retain a short bounded wait so socket results are
			// consumed promptly. With no in-flight work, only perform a low-rate
			// connection health check.
			if (!bDidWork)
			{
				const uint32 WaitMilliseconds =
					ProducerState->GetPendingTransactionCount() > 0 ? 5U : 1000U;
				WorkEvent->Wait(WaitMilliseconds);
			}
		}

		bConnected.store(false, std::memory_order_relaxed);
		CloseSocket();
		ProducerState->Disconnect(ActiveGeneration);
		if (!bShouldStop.load(std::memory_order_relaxed))
		{
			UE_LOG(LogUSDEmit, Log, TEXT("Emitter disconnected reconnecting in %.1fs"),
				   ReconnectDelaySecs);
			FPlatformProcess::Sleep(RateLimitDelay > 0.0f ? RateLimitDelay : ReconnectDelaySecs);
		}
	}
	return 0;
}

void FEmitClient::Stop()
{
	bShouldStop.store(true, std::memory_order_relaxed);
	if (WorkEvent)
	{
		WorkEvent->Trigger();
	}
	InterruptSocket();
}

void FEmitClient::Exit()
{
	bConnected.store(false, std::memory_order_relaxed);
	CloseSocket();
}

bool FEmitClient::EnqueueFrame(uint64 TxnId, FWireFrame&& Frame)
{
	const bool bAccepted = ProducerState->EnqueueFrame(
		SessionGeneration.load(std::memory_order_relaxed), TxnId, MoveTemp(Frame));
	if (bAccepted && WorkEvent)
	{
		WorkEvent->Trigger();
	}
	return bAccepted;
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
		if (bShouldStop.load(std::memory_order_relaxed) || !Socket)
			return false;
		if (!Socket->Wait(ESocketWaitConditions::WaitForRead,
						  FTimespan::FromSeconds(FrameReadTimeoutSeconds)))
		{
			UE_LOG(LogUSDEmit, Warning, TEXT("Timed out while receiving emitter frame"));
			return false;
		}
		int32 Read = 0;
		if (!Socket->Recv(Buf + Got, Needed - Got, Read) || Read <= 0)
			return false;
		Got += Read;
	}
	return true;
}

bool FEmitClient::RecvFrame(TArray<uint8>& OutFrame)
{
	uint8 LenBuf[4];
	if (!RecvExact(LenBuf, 4))
	{
		return false;
	}
	std::size_t PayloadLen = 0;
	if (!openusdconnect::client::TryReadFrameHeader(LenBuf, kMaxFrameSize, PayloadLen))
	{
		return false;
	}
	OutFrame.SetNumUninitialized(static_cast<int32>(PayloadLen));
	return RecvExact(OutFrame.GetData(), static_cast<int32>(PayloadLen));
}

bool FEmitClient::SendAll(const uint8* Data, int32 Len)
{
	int32 Sent = 0;
	while (Sent < Len)
	{
		if (!Socket)
			return false;
		int32 ThisSent = 0;
		if (!Socket->Send(Data + Sent, Len - Sent, ThisSent) || ThisSent <= 0)
		{
			return false;
		}
		Sent += ThisSent;
	}
	return true;
}
