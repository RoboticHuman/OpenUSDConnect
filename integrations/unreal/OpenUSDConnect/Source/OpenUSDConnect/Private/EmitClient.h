// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "HAL/Runnable.h"
#include "HAL/RunnableThread.h"
#include "Sockets.h"
#include "Containers/Queue.h"
#include <atomic>

/**
 * Background TCP thread that connects to the OpenUSDConnect server as an **emitter**.
 *
 * Protocol:
 *  1. Connect TCP and send Envelope{Hello, role="emitter"}
 *  2. Await Envelope{HelloOk}
 *  3. Loop:
 *      - Drain SendQueue (pre-framed Txn frames produced by the subsystem) → send
 *      - HasPendingData() → if data available, read one frame and dispatch
 *        (RateLimited / corrections; Ping/BroadcastEvent corrections ignored)
 *      - Sleep 5ms when idle to avoid busy-spinning
 *  4. On error: close socket, wait, retry.
 *
 * Threading: the SendQueue is SPSC (single producer = game thread,
 * single consumer = this thread). Frames pushed via EnqueueFrame() include the
 * 4-byte big-endian length prefix.
 */
class FEmitClient : public FRunnable
{
public:
	FEmitClient(const FString& InHost,
	            int32 InPort,
	            const FString& InDepartment,
	            const FString& InClientId,
	            const FString& InSessionOrigin,
	            float InReconnectDelaySecs);

	virtual ~FEmitClient();

	// FRunnable
	virtual bool Init() override;
	virtual uint32 Run() override;
	virtual void Stop() override;
	virtual void Exit() override;

	bool Start();
	void StopAndWait();

	bool IsConnected() const { return bConnected.load(std::memory_order_relaxed); }

	/** Push a complete pre-framed Envelope{Txn} message onto the send queue. */
	void EnqueueFrame(TArray<uint8>&& Frame);

private:
	bool RecvExact(uint8* Buf, int32 Needed);
	bool RecvFrame(TArray<uint8>& OutFrame);
	bool SendAll(const uint8* Data, int32 Len);
	void CloseSocket();

	FString Host;
	int32   Port;
	FString Department;
	FString ClientId;
	FString SessionOrigin;
	float   ReconnectDelaySecs;

	FSocket*         Socket;
	FRunnableThread* Thread;
	std::atomic<bool> bShouldStop;
	std::atomic<bool> bConnected;

	TQueue<TArray<uint8>, EQueueMode::Spsc> SendQueue;
};
