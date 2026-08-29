// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "HAL/Runnable.h"
#include "HAL/RunnableThread.h"
#include "HAL/Event.h"
#include "HAL/CriticalSection.h"
#include "Sockets.h"
#include "USDConnectRecovery.h"
#include "USDWireFraming.h"
#include "openusdconnect/client/producer_session.h"
#include <atomic>

class UUSDConnectSubsystem;

using FProducerFrame = TSharedPtr<const OUC::FWireFrame, ESPMode::ThreadSafe>;
using FProducerSession = openusdconnect::client::OrderedProducerSession<FProducerFrame>;

struct FQueuedProducerTxn
{
	uint64 TxnId = 0;
	FProducerFrame Frame;
};

/**
 * Endpoint-scoped producer identity and durable in-memory outbox.
 *
 * The subsystem owns this object, not an individual socket thread. Replacing
 * FEmitClient for the same endpoint therefore preserves transaction identity,
 * encoded bytes, acknowledgements, and recovery state. A different endpoint
 * must use a different state object whose transaction sequence starts at one.
 */
class FProducerEndpointState
{
public:
	FProducerEndpointState(const FString& InHost, int32 InPort, const FString& InDepartment,
						   const FString& InSessionId);

	bool MatchesEndpoint(const FString& InHost, int32 InPort, const FString& InDepartment) const;
	const FString& GetHost() const
	{
		return Host;
	}
	int32 GetPort() const
	{
		return Port;
	}
	const FString& GetDepartment() const
	{
		return Department;
	}
	const FString& GetSessionId() const
	{
		return SessionId;
	}

	uint64 GetNextTransactionId() const;
	bool BeginConnection(uint64& OutGeneration);
	void Disconnect(uint64 Generation);
	bool EnqueueFrame(uint64 Generation, uint64 TxnId, OUC::FWireFrame&& Frame);
	bool ClaimNextUnsent(uint64 Generation, FQueuedProducerTxn& OutTxn);
	bool AcceptServerHighwater(uint64 Generation, uint64 CommittedThrough, FString& OutError);
	void RetireThrough(uint64 Generation, uint64 AckId);
	void MarkRejected(uint64 Generation, uint64 TxnId, uint8 RejectionCode, const FString& Reason);

	uint64 GetSubmittedTransactionCount() const;
	uint64 GetAcknowledgedTransactionCount() const;
	uint64 GetPendingTransactionCount() const;
	bool IsRecoveryRequired() const;
	FString GetRecoveryReason() const;
	EUSDConnectRecoveryDisposition GetRecoveryDisposition() const;

private:
	FString Host;
	int32 Port;
	FString Department;
	FString SessionId;

	FProducerSession Session;
	mutable FCriticalSection RecoveryCS;
	FString RecoveryReason;
};

/**
 * Background TCP thread that connects to the OpenUSDConnect server as an **emitter**.
 *
 * Protocol:
 *  1. Connect TCP and send Envelope{Hello, role="emitter"}
 *  2. Await Envelope{HelloOk}
 *  3. Loop:
 *      - Claim unsent frames from the endpoint-scoped producer state → send
 *      - HasPendingData() → if data available, read one frame and dispatch
 *        (RateLimited is handled; other control messages are ignored)
 *      - Wait on an enqueue event while idle with a bounded socket health check
 *  4. On error:
 * retain the in-flight frame, close socket, wait, and retry it before later queued frames on the
 * next connection.
 *
 * Frames pushed via EnqueueFrame() include the 4-byte big-endian length prefix.
 * FProducerEndpointState serializes game-thread submission with socket-thread
 * acknowledgement and survives replacement of this client object.
 */
class FEmitClient : public FRunnable
{
public:
	FEmitClient(UUSDConnectSubsystem* InOwner, const FString& InClientId,
				const TSharedRef<FProducerEndpointState>& InProducerState,
				float InReconnectDelaySecs, const FString& InAuthToken = FString());

	virtual ~FEmitClient();

	// FRunnable
	virtual bool Init() override;
	virtual uint32 Run() override;
	virtual void Stop() override;
	virtual void Exit() override;

	bool Start();
	void StopAndWait();
	/** Wait for the server's cumulative acknowledgement without blocking normal emits. */
	bool FlushPending(double TimeoutSeconds) const;

	bool IsConnected() const
	{
		return bConnected.load(std::memory_order_acquire);
	}

	/**
	 * Monotonically increasing identifier for each successful HELLO handshake.
	 * The game thread uses this to replay per-connection structural prerequisites
	 * before sending value-only transform events to a fresh server session.
	 */
	uint64 GetConnectionGeneration() const
	{
		return ConnectionGeneration.load(std::memory_order_relaxed);
	}

	/** Push a complete pre-framed Envelope{Txn} into the endpoint outbox. */
	bool EnqueueFrame(uint64 TxnId, OUC::FWireFrame&& Frame);

private:
	bool RecvExact(uint8* Buf, int32 Needed);
	bool RecvFrame(TArray<uint8>& OutFrame);
	bool SendAll(const uint8* Data, int32 Len);
	void InterruptSocket();
	void CloseSocket();

	UUSDConnectSubsystem* Owner;
	TSharedRef<FProducerEndpointState> ProducerState;
	FString ClientId;
	FString AuthToken;
	float ReconnectDelaySecs;

	FSocket* Socket;
	FCriticalSection SocketCS;
	FRunnableThread* Thread;
	FEvent* WorkEvent;
	std::atomic<bool> bShouldStop;
	std::atomic<bool> bConnected;
	std::atomic<uint64> ConnectionGeneration;
	std::atomic<uint64> SessionGeneration;
};
