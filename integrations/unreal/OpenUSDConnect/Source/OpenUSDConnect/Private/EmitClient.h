// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "HAL/Runnable.h"
#include "HAL/RunnableThread.h"
#include "HAL/CriticalSection.h"
#include "Sockets.h"
#include "Containers/Queue.h"
#include <atomic>

class UUSDConnectSubsystem;

struct FQueuedProducerTxn
{
	uint64 TxnId = 0;
	TArray<uint8> Frame;
	bool bSent = false;
};

/**
 * Background TCP thread that connects to the OpenUSDConnect server as an **emitter**.
 *
 * Protocol:
 *  1. Connect TCP and send Envelope{Hello, role="emitter"}
 *  2. Await Envelope{HelloOk}
 *  3. Loop:
 *      - Drain SendQueue (pre-framed Txn frames produced by the subsystem) → send
 *      - HasPendingData() → if data available, read one frame and dispatch
 *        (RateLimited is handled; other control messages are ignored)
 *      - Sleep 5ms when idle to avoid busy-spinning
 *  4. On error: retain the in-flight frame, close socket, wait, and retry it
 *     before later queued frames on the next connection.
 *
 * Threading: the SendQueue is SPSC (single producer = game thread,
 * single consumer = this thread). Frames pushed via EnqueueFrame() include the
 * 4-byte big-endian length prefix.
 */
class FEmitClient : public FRunnable
{
public:
	FEmitClient(UUSDConnectSubsystem* InOwner,
	            const FString& InHost,
	            int32 InPort,
	            const FString& InDepartment,
	            const FString& InClientId,
	            const FString& InSessionOrigin,
	            float InReconnectDelaySecs,
	            const FString& InAuthToken = FString());

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

	bool IsConnected() const { return bConnected.load(std::memory_order_acquire); }
	uint64 GetSubmittedTransactionCount() const
	{
		return SubmittedTransactionCount.load(std::memory_order_relaxed);
	}
	uint64 GetAcknowledgedTransactionCount() const
	{
		return AcknowledgedTransactionCount.load(std::memory_order_relaxed);
	}
	uint64 GetPendingTransactionCount() const
	{
		return PendingTransactionCount.load(std::memory_order_relaxed);
	}
	bool IsRecoveryRequired() const
	{
		return bRecoveryRequired.load(std::memory_order_acquire);
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

	/** Push a complete pre-framed Envelope{Txn} into the acknowledged outbox. */
	void EnqueueFrame(uint64 TxnId, TArray<uint8>&& Frame);

private:
	bool RecvExact(uint8* Buf, int32 Needed);
	bool RecvFrame(TArray<uint8>& OutFrame);
	bool SendAll(const uint8* Data, int32 Len);
	void InterruptSocket();
	void CloseSocket();

	UUSDConnectSubsystem* Owner;
	FString Host;
	int32   Port;
	FString Department;
	FString ClientId;
	FString SessionOrigin;
	FString AuthToken;
	float   ReconnectDelaySecs;

	FSocket*         Socket;
	FCriticalSection SocketCS;
	FRunnableThread* Thread;
	std::atomic<bool> bShouldStop;
	std::atomic<bool> bConnected;
	std::atomic<uint64> ConnectionGeneration;
	std::atomic<uint64> SubmittedTransactionCount;
	std::atomic<uint64> AcknowledgedTransactionCount;
	std::atomic<uint64> PendingTransactionCount;
	std::atomic<bool> bRecoveryRequired;

	TQueue<FQueuedProducerTxn, EQueueMode::Spsc> SendQueue;
};
