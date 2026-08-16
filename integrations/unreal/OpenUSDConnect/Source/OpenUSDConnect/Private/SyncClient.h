// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "HAL/Runnable.h"
#include "HAL/RunnableThread.h"
#include "HAL/CriticalSection.h"
#include "Sockets.h"
#include <atomic>

class UUSDConnectSubsystem;

/**
 * Background TCP thread that connects to the OpenUSDConnect server as a **receiver**.
 *
 * Protocol:
 *  1. Connect TCP to host:port
 *  2. Send Envelope{Hello, role="receiver"} with shared ClientId + SessionOrigin
 *  3. Read Envelope{HelloOk} auth handshake
 *  4. Loop: read 4-byte big-endian length + FlatBuffers payload
 *       - BroadcastEvent → validate contiguous receipt and enqueue raw bytes
 *       - Ping → ignore
 *       - RateLimited → sleep retry_after seconds
 *       - Resync → begin a new replay generation from sequence zero
 *  5. On error: close socket, wait ReconnectDelaySecs, goto 1.
 *
 * Stop signalling: setting bShouldStop + closing the socket unblocks any pending Recv.
 */
class FSyncClient : public FRunnable
{
public:
	FSyncClient(UUSDConnectSubsystem* InOwner,
	            const FString& InHost,
	            int32 InPort,
	            const FString& InDepartment,
	            const FString& InClientId,
	            const FString& InSessionOrigin,
	            float InReconnectDelaySecs,
	            int32 InInitialLastSeq = 0,
	            const FString& InAuthToken = FString());

	virtual ~FSyncClient();

	// FRunnable
	virtual bool Init() override;
	virtual uint32 Run() override;
	virtual void Stop() override;
	virtual void Exit() override;

	bool Start();
	void StopAndWait();

	bool IsConnected() const { return bConnected.load(std::memory_order_relaxed); }
	int32 GetLastAppliedSeq() const { return LastAppliedSeq.load(std::memory_order_acquire); }
	void MarkAppliedThrough(int32 Seq);
	void ResetAppliedProgress();
	void RequestReplayFromApplied();

private:
	bool RecvExact(uint8* Buf, int32 Needed);
	bool RecvFrame(TArray<uint8>& OutFrame);
	bool SendAll(const uint8* Data, int32 Len);
	bool HandleFrame(const TArray<uint8>& Frame);
	void InterruptSocket();
	void CloseSocket();

	UUSDConnectSubsystem* Owner;

	FString Host;
	int32   Port;
	FString Department;
	FString ClientId;       // shared with FEmitClient
	FString SessionOrigin;  // shared with FEmitClient for attribution/reconciliation
	FString AuthToken;
	float   ReconnectDelaySecs;
	int32   LastReceivedSeq; // recv-thread only; validates the current socket stream
	std::atomic<int32> LastAppliedSeq; // game-thread durable replay cursor
	uint64 ReplayGeneration = 0; // recv-thread only

	FSocket*         Socket;
	FCriticalSection SocketCS;
	FRunnableThread* Thread;
	std::atomic<bool> bShouldStop;
	std::atomic<bool> bConnected;
};
