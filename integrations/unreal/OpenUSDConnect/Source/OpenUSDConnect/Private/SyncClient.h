// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "HAL/Runnable.h"
#include "HAL/RunnableThread.h"
#include "HAL/CriticalSection.h"
#include "Sockets.h"
#include "USDConnectProtocol.h"
#include "openusdconnect/client/receiver_session.h"
#include <atomic>

class UUSDConnectSubsystem;

struct FValidatedReceiverFrame
{
	TArray<uint8> Bytes;
	int32 Sequence = 0;
	OpenUSDConnect::EventPayload EventKind = OpenUSDConnect::EventPayload::NONE;
	bool bResync = false;
	bool bUsesChangeBlock = false;
};

using FReceiverSession = openusdconnect::client::OrderedReceiverSession<FValidatedReceiverFrame>;

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
	FSyncClient(UUSDConnectSubsystem* InOwner, const FString& InHost, int32 InPort,
				const FString& InDepartment, const FString& InClientId,
				const FString& InSessionOrigin, float InReconnectDelaySecs,
				int32 InInitialLastSeq = 0, const FString& InAuthToken = FString());

	virtual ~FSyncClient();

	// FRunnable
	virtual bool Init() override;
	virtual uint32 Run() override;
	virtual void Stop() override;
	virtual void Exit() override;

	bool Start();
	void StopAndWait();

	bool IsConnected() const
	{
		return bConnected.load(std::memory_order_relaxed);
	}
	int32 GetLastAppliedSeq() const
	{
		return ReceiverSession.LastAppliedSequence();
	}
	int32 GetReplayHeadSeq() const
	{
		return ReceiverSession.ReplayHeadSequence();
	}
	uint64 GetReplayEpoch() const
	{
		return ReceiverSession.ReplayEpoch();
	}
	uint64 GetGeneration() const
	{
		return ReceiverSession.Generation();
	}
	int32 GetPendingFrameCount() const
	{
		return static_cast<int32>(ReceiverSession.Size());
	}
	bool TryPopFrame(FValidatedReceiverFrame& OutFrame);
	bool MarkReplayApplied();
	bool MarkAppliedThrough(int32 Seq);
	void ResetAppliedProgress();
	void RequestReplayFromApplied();

private:
	bool RecvExact(uint8* Buf, int32 Needed);
	bool RecvFrame(TArray<uint8>& OutFrame);
	bool SendAll(const uint8* Data, int32 Len);
	bool HandleFrame(uint64 Generation, TArray<uint8>&& Frame);
	void InterruptSocket();
	void CloseSocket();

	UUSDConnectSubsystem* Owner;

	FString Host;
	int32 Port;
	FString Department;
	FString ClientId;	   // shared with FEmitClient
	FString SessionOrigin; // shared with FEmitClient for attribution/reconciliation
	FString AuthToken;
	float ReconnectDelaySecs;
	FReceiverSession ReceiverSession;
	std::atomic<uint64> ActiveGeneration;

	FSocket* Socket;
	FCriticalSection SocketCS;
	FRunnableThread* Thread;
	std::atomic<bool> bShouldStop;
	std::atomic<bool> bConnected;
};
