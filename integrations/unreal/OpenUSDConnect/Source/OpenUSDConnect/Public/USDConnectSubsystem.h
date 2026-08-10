// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "Subsystems/WorldSubsystem.h"
#include "HAL/CriticalSection.h"
#include "Containers/Set.h"
#include "Delegates/IDelegateInstance.h"
#include "USDConnectRecovery.h"
#include <atomic>
#include "USDConnectSubsystem.generated.h"

class FSyncClient;
class FEmitClient;
class FProducerEndpointState;
class AUsdStageActor;

USTRUCT(BlueprintType)
struct OPENUSDCONNECT_API FUSDConnectStatus
{
	GENERATED_BODY()

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	FString EndpointHost;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	int32 EndpointPort = 0;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	bool bUsingLiveMetadata = false;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	int32 SnapshotSeq = 0;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	bool bReceiverStarted = false;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	bool bReceiverConnected = false;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	bool bReceiverSynchronized = false;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	bool bEmitterStarted = false;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	bool bEmitterConnected = false;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	int64 SubmittedTransactions = 0;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	int64 AcknowledgedTransactions = 0;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	int32 PendingTransactions = 0;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	bool bRecoveryRequired = false;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	EUSDConnectRecoveryDisposition RecoveryDisposition =
		EUSDConnectRecoveryDisposition::None;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	FString AuthState;

	UPROPERTY(BlueprintReadOnly, Category="OpenUSD Connect")
	FString LastMessage;
};

/**
 * World subsystem that manages the OpenUSD Connect two-way sync.
 *
 * Receiver side (server → Unreal):
 *   FSyncClient background thread receives BroadcastEvent frames and
 *   pushes raw bytes to the event queue. Tick() drains the queue and
 *   applies each event to the open AUsdStageActor stage via FUSDEventApplier.
 *
 * Emitter side (Unreal → server):
 *   The stage listener fires whenever the USD stage changes locally (viewport
 *   transforms, USD Stage panel property edits). The subsystem reads the
 *   current TRS, visibility, and changed shader inputs from the pxr stage and
 *   sends SetXformTrs/SetVisibility/SetConnectableInput events via
 *   FEmitClient. A feedback loop guard (bSuppressEmit) prevents echoing
 *   events received from the server back out.
 *
 * Usage:
 *  1. Place an AUsdStageActor in the level; set its RootLayer to the USD file
 *     the server is managing.
 *  2. Configure host/port in Edit > Project Settings > Plugins > OpenUSD Connect.
 *  3. Press Play — the subsystem auto-connects both receiver and emitter.
 */
UCLASS()
class OPENUSDCONNECT_API UUSDConnectSubsystem : public UTickableWorldSubsystem
{
	GENERATED_BODY()

public:
	// UWorldSubsystem
	virtual void Initialize(FSubsystemCollectionBase& Collection) override;
	virtual void Deinitialize() override;

	// UTickableWorldSubsystem
	virtual void Tick(float DeltaTime) override;
	virtual TStatId GetStatId() const override;
	virtual bool IsTickable() const override { return true; }

	// Live sync must run in the editor (without PIE), not just during Play.
	// UTickableWorldSubsystem defaults this to false — we override to true.
	virtual bool IsTickableInEditor() const override { return true; }

	// Keep ticking when the game is paused so sync stays responsive.
	virtual bool IsTickableWhenPaused() const override { return true; }

	/** Manually connect receiver and emitter */
	UFUNCTION(BlueprintCallable, Category="OpenUSD Connect")
	void Connect();

	/** Disconnect both receiver and emitter */
	UFUNCTION(BlueprintCallable, Category="OpenUSD Connect")
	void Disconnect();

	/** Wait until all submitted emitter transactions are durably acknowledged. */
	UFUNCTION(BlueprintCallable, Category="OpenUSD Connect")
	bool Flush(float TimeoutSeconds = 5.0f) const;

	/** True while the receiver TCP connection is established */
	UFUNCTION(BlueprintPure, Category="OpenUSD Connect")
	bool IsConnected() const;

	/** Runtime connection/auth status for UI panels or debugging widgets. */
	UFUNCTION(BlueprintPure, Category="OpenUSD Connect")
	FUSDConnectStatus GetStatus() const;

	/** Called from FSyncClient background thread — pushes one replay-generation frame. */
	void EnqueueEvent(uint64 ReplayGeneration, TArray<uint8>&& RawBytes);

	/** Called from client background threads when the server issues a TOFU token. */
	void OnClientTokenIssued(const FString& Token);

	/** Called from client background threads after HELLO_OK. */
	void OnClientHelloOk(const FString& Role);

	/** Select a receiver replay generation and discard queued frames from older streams. */
	void OnReceiverReplayGenerationChanged(uint64 ReplayGeneration);

	/** Called when the server deterministically rejects a producer transaction. */
	void OnEmitterTransactionRejected(uint64 TxnId, const FString& Reason);

	/** Called from client background threads when auth is rejected. */
	void OnClientAuthRejected(const FString& Role);

	/** Called from client background threads when the requested mode is rejected. */
	void OnClientHelloRejected(
		const FString& Role,
		const FString& Code,
		const FString& Reason);

private:
	struct FQueuedReceiverFrame
	{
		uint64 ReplayGeneration = 0;
		TArray<uint8> Bytes;
	};

	AUsdStageActor* FindStageActor() const;
	void AttachToStageActor(AUsdStageActor* Actor);
	void DetachFromStageActor();
	void StopClients();
	void ConnectResolved(bool bRespectLiveMetadataAutoStart);
	void RefreshLiveMetadataFromStage(AUsdStageActor* Actor);
	void TryStartDeferredEmitter();
	void QueueInitialMaterializations(AUsdStageActor* Actor);
	FString LoadAuthToken(const FString& Host, int32 Port, const FString& Department) const;
	void SaveAuthToken(const FString& Host, int32 Port, const FString& Department, const FString& Token) const;
	void SetStatusMessage(const FString& AuthState, const FString& Message);
	void RequestReceiverReplay(const FString& Reason);

	void DrainAndApply();

	/** Drain accumulated SdfPaths from the stage listener and emit one frame per unique path */
	void DrainAndEmit();

	/** Build and send a Txn event for a changed prim (emitter side) */
	void EmitPrimChange(AUsdStageActor* StageActor, const FString& PrimPath);

	/** Build and send a SetConnectableInput Txn for changed shader inputs on one prim */
	void EmitConnectableInputs(AUsdStageActor* StageActor, const FString& PrimPath, const TSet<FString>& InputAttrNames);

	/** Refresh local .mtlx documents for materials dirtied this tick */
	void ProcessPendingMaterializations();

	// --- Receiver ---
	TSharedPtr<FSyncClient> SyncClient;
	FCriticalSection EventQueueCS;
	TArray<FQueuedReceiverFrame> EventQueue;

	// --- Emitter ---
	TSharedPtr<FEmitClient> EmitClient;
	TSharedPtr<FProducerEndpointState> ProducerState;

	/**
	 * Transform prims whose structural xform-op prerequisite has been sent on
	 * the current emitter connection. Game-thread only.
	 */
	TSet<FString> EmittedXformPrims;
	uint64 LastEmitConnectionGeneration = 0;

	/** Stable client ID shared by both receiver and emitter connections */
	FString ClientId;
	/**
	 * When true, Tick() will perform the auto-connect on the first tick that runs
	 * AFTER the world is fully initialized. This avoids spawning TCP threads from
	 * within UWorldSubsystem::Initialize() (which runs mid-world-load and can
	 * destabilise editor startup).
	 */
	bool bPendingAutoConnect = false;

	/**
	 * Set to true while DrainAndApply() is applying received events.
	 * Prevents OnPrimChanged from echoing those changes back to the server.
	 *
	 * Uses default seq_cst ordering. The other socket-thread atomics in this
	 * module use relaxed because they only flag a state for polling; this one
	 * fences a code region around stage mutation, so the stronger barrier is
	 * the safer default and not on a hot path.
	 */
	std::atomic<bool> bSuppressEmit;

	/** New native transactions remain gated until replay is applied on the game thread. */
	std::atomic<bool> bReplaySynchronized;
	std::atomic<uint64> ActiveReplayGeneration;
	int32 ReplayHeadSeq = 0;
	uint64 ReplayEpoch = 0;

	/** Cached weak reference to the currently attached stage actor */
	TWeakObjectPtr<AUsdStageActor> CachedStageActor;

	/**
	 * Delegate handle for the engine FUsdListener::OnObjectsChanged subscription.
	 * We use the engine's wrapper instead of registering our own pxr::TfNotice
	 * because the wrapper avoids a `dynamic_cast` macro collision between UE's
	 * CoreUObject and pxr's notice templates.
	 *
	 * The delegate gives us actual SdfPaths of changed prims (the engine's
	 * OnPrimChanged delegate rolls sub-prim paths up to the nearest
	 * KindsToCollapse ancestor, which is useless for emitting).
	 */
	FDelegateHandle ObjectsChangedHandle;

	/** Prim paths accumulated by the OnObjectsChanged callback, drained each tick. */
	FCriticalSection PendingEmitPathsCS;
	TSet<FString> PendingEmitPaths;

	/**
	 * Changed "inputs:*" property names per prim (same lock as PendingEmitPaths).
	 * Keeping the property names lets the drain read and emit only the edited
	 * shader inputs instead of the whole network.
	 */
	TMap<FString, TSet<FString>> PendingEmitInputs;

	/** Active TCP endpoint for the currently running clients. */
	FString ActiveServerHost;
	int32 ActiveServerPort = 0;
	bool bActiveReceiverStarted = false;
	bool bActiveEmitterStarted = false;
	bool bActiveUsingLiveMetadata = false;
	bool bDeferredEmitterForToken = false;
	int32 ActiveSnapshotSeq = 0;
	FString ActiveAuthToken;

	/** Last live metadata key seen on the attached stage root layer. */
	FString LastLiveMetadataKey;

	/** Root layer identifier last scanned for initial MaterialX materialization. */
	FString LastMaterializedRootLayerIdentifier;

	mutable FCriticalSection StatusCS;
	FString LastAuthState = TEXT("not_connected");
	FString LastStatusMessage;

	/**
	 * Prims whose material networks changed this tick (received events and
	 * local edits), resolved to owning materials and materialized to local
	 * .mtlx documents at the end of Tick. Game thread only — both producers
	 * (DrainAndApply, DrainAndEmit) and the consumer run there.
	 */
	TSet<FString> PendingMaterializePrims;
};
