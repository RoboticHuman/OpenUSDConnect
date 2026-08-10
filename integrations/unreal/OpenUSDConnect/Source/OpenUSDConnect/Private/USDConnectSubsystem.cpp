// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "USDConnectSubsystem.h"

#include "SyncClient.h"
#include "EmitClient.h"
#include "USDConnectSettings.h"
#include "USDEventApplier.h"
#include "USDMaterialXMaterializer.h"
#include "USDStageBridge.h"
#include "USDConnectProtocol.h"
#include "TxnBuilder.h"

#include "USDStageActor.h"
#include "USDListener.h"
#include "EngineUtils.h"
#include "Logging/LogMacros.h"
#include "Stats/Stats.h"
#include "Async/Async.h"
#include "Misc/Guid.h"
#include "Misc/App.h"
#include "Misc/ConfigCacheIni.h"
#include "Misc/Crc.h"
#include "HAL/PlatformProcess.h"

#if WITH_EDITOR
#include "ScopedTransaction.h"
#endif

DEFINE_LOG_CATEGORY_STATIC(LogUSDConnectSubsystem, Log, All);

DECLARE_STATS_GROUP(TEXT("OpenUSDConnect"), STATGROUP_OpenUSDConnect, STATCAT_Advanced);
DECLARE_CYCLE_STAT(TEXT("USDConnect Tick"), STAT_USDConnectTick, STATGROUP_OpenUSDConnect);

// ---------------------------------------------------------------------------
// Authentication helpers
// ---------------------------------------------------------------------------
static const TCHAR* OUCAuthConfigSection = TEXT("OpenUSDConnect.Tokens");

static FString MakeAuthConfigKey(const FString& Host, int32 Port, const FString& Department)
{
	FString Key = FString::Printf(TEXT("%s:%d:%s"), *Host, Port, *Department);
	Key.ReplaceInline(TEXT("\\"), TEXT("_"));
	Key.ReplaceInline(TEXT("/"), TEXT("_"));
	Key.ReplaceInline(TEXT(" "), TEXT("_"));
	return Key;
}

// ---------------------------------------------------------------------------
// UWorldSubsystem lifecycle
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::Initialize(FSubsystemCollectionBase& Collection)
{
	Super::Initialize(Collection);
	bSuppressEmit.store(false);
	bReplaySynchronized.store(false);
	ActiveReplayGeneration.store(0);

	// Generate the stable client ID. Producer session identity is endpoint-scoped
	// and is created lazily when ConnectResolved selects an endpoint.
	{
		const FString MachineId = FPlatformProcess::ComputerName();
		const FString ProjectId = FApp::GetProjectName();
		const FString Combined  = MachineId + ProjectId;
		const uint32  Hash      = FCrc::MemCrc32(TCHAR_TO_UTF8(*Combined), Combined.Len());
		ClientId      = FString::Printf(TEXT("unreal-%08x-%s"), Hash, *MachineId);
	}

	// IMPORTANT: don't call Connect() here. Spawning FRunnableThreads from inside
	// a UWorldSubsystem::Initialize() runs mid-world-load and can stall editor
	// startup. Instead defer to the first Tick() that runs after the world is
	// fully initialized.
	const UUSDConnectSettings* Settings = GetDefault<UUSDConnectSettings>();
	bPendingAutoConnect = (Settings && Settings->bAutoConnect);
}

void UUSDConnectSubsystem::Deinitialize()
{
	Disconnect();
	Super::Deinitialize();
}

// ---------------------------------------------------------------------------
// Connect / Disconnect
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::Connect()
{
	ConnectResolved(false);
}

void UUSDConnectSubsystem::StopClients()
{
	if (SyncClient) { SyncClient->StopAndWait(); SyncClient.Reset(); }
	if (EmitClient)
	{
		const uint64 Pending = ProducerState
			? ProducerState->GetPendingTransactionCount()
			: 0;
		if (Pending > 0 && !EmitClient->FlushPending(2.0))
		{
			UE_LOG(LogUSDConnectSubsystem, Warning,
				TEXT("Disconnecting with %llu unacknowledged producer transactions"),
				ProducerState->GetPendingTransactionCount());
		}
		EmitClient->StopAndWait();
		EmitClient.Reset();
	}
	EmittedXformPrims.Reset();
	LastEmitConnectionGeneration = 0;
	bReplaySynchronized.store(false);
	ReplayHeadSeq = 0;
	ReplayEpoch = 0;

	{
		FScopeLock Lock(&EventQueueCS);
		EventQueue.Reset();
	}

	ActiveServerHost.Empty();
	ActiveServerPort = 0;
	bActiveReceiverStarted = false;
	bActiveEmitterStarted = false;
	bActiveUsingLiveMetadata = false;
	bDeferredEmitterForToken = false;
	ActiveSnapshotSeq = 0;
	ActiveAuthToken.Empty();
}

void UUSDConnectSubsystem::ConnectResolved(bool bRespectLiveMetadataAutoStart)
{
	const UUSDConnectSettings* Settings = GetDefault<UUSDConnectSettings>();
	if (!Settings) return;

	FString TargetHost = Settings->ServerHost;
	int32 TargetPort = Settings->ServerPort;
	int32 ReceiverInitialLastSeq = 0;
	bool bStartReceiver = true;
	bool bStartEmitter = true;
	bool bUsingLiveMetadata = false;
	bool bTargetRequiresToken = false;

	if (Settings->bUseLiveMetadataFromStage)
	{
		if (AUsdStageActor* StageActor = CachedStageActor.Get())
		{
			FUSDLiveOpenMetadata Metadata;
			if (FUSDStageBridge::ReadLiveOpenMetadata(StageActor, Metadata))
			{
				TargetHost = Metadata.Host;
				TargetPort = Metadata.Port;
				ReceiverInitialLastSeq = FMath::Max(0, Metadata.SnapshotSeq);
				bUsingLiveMetadata = true;
				bTargetRequiresToken = Metadata.bRequiresToken;
				if (bRespectLiveMetadataAutoStart)
				{
					bStartReceiver = Settings->bAutoStartReceiverFromLiveMetadata;
					bStartEmitter = Settings->bAutoStartEmitterFromLiveMetadata;
				}
			}
		}
	}

	if (bStartReceiver && !Settings->Department.IsEmpty())
	{
		StopClients();
		SetStatusMessage(
			TEXT("unsupported_configuration"),
			TEXT("Department receive mode requires managed layered replay, which the native Unreal plugin does not implement"));
		UE_LOG(LogUSDConnectSubsystem, Error,
			TEXT("Department '%s' rejected: native Unreal receivers do not support managed layered replay"),
			*Settings->Department);
		return;
	}

	const bool bSameProducerEndpoint = ProducerState
		&& ProducerState->MatchesEndpoint(TargetHost, TargetPort, Settings->Department);
	if (ProducerState && !bSameProducerEndpoint
		&& ProducerState->GetPendingTransactionCount() > 0)
	{
		SetStatusMessage(
			TEXT("pending_transactions"),
			FString::Printf(
				TEXT("Cannot switch endpoints with %llu unacknowledged transaction(s); reconnect to %s:%d and flush first"),
				ProducerState->GetPendingTransactionCount(),
				*ProducerState->GetHost(),
				ProducerState->GetPort()));
		return;
	}
	if (bSameProducerEndpoint && ProducerState->IsRecoveryRequired())
	{
		SetStatusMessage(TEXT("recovery_required"), ProducerState->GetRecoveryReason());
		return;
	}

	const FString TargetToken = Settings->bPersistAuthTokens
		? LoadAuthToken(TargetHost, TargetPort, Settings->Department)
		: FString();
	const bool bDelayEmitterForToken =
		bTargetRequiresToken && TargetToken.IsEmpty() && bStartReceiver && bStartEmitter;

	if (!bStartReceiver && !bStartEmitter)
	{
		StopClients();
		ActiveServerHost = TargetHost;
		ActiveServerPort = TargetPort;
		bActiveUsingLiveMetadata = bUsingLiveMetadata;
		ActiveSnapshotSeq = ReceiverInitialLastSeq;
		ActiveAuthToken = TargetToken;
		SetStatusMessage(
			bTargetRequiresToken ? TEXT("token_required") : TEXT("not_connected"),
			TEXT("Live metadata configured; auto-start disabled"));
		UE_LOG(LogUSDConnectSubsystem, Log,
			TEXT("OpenUSDConnect live metadata configured %s:%d, auto-start disabled"),
			*TargetHost, TargetPort);
		return;
	}

	if ((SyncClient || EmitClient) &&
		ActiveServerHost == TargetHost &&
		ActiveServerPort == TargetPort &&
		bActiveReceiverStarted == bStartReceiver &&
		bActiveEmitterStarted == bStartEmitter)
	{
		UE_LOG(LogUSDConnectSubsystem, Warning, TEXT("Already connected"));
		return;
	}

	StopClients();
	if (!bSameProducerEndpoint)
	{
		ProducerState = MakeShared<FProducerEndpointState>(
			TargetHost,
			TargetPort,
			Settings->Department,
			FGuid::NewGuid().ToString(EGuidFormats::Digits));
	}
	UE_LOG(LogUSDConnectSubsystem, Log, TEXT("Connecting to %s:%d (client_id=%s)"),
		*TargetHost, TargetPort, *ClientId);
	if (bUsingLiveMetadata)
	{
		UE_LOG(LogUSDConnectSubsystem, Log,
			TEXT("Using USD live metadata; receiver will sync from seq=%d"),
			ReceiverInitialLastSeq + 1);
	}

	ActiveServerHost = TargetHost;
	ActiveServerPort = TargetPort;
	bActiveReceiverStarted = false;
	bActiveEmitterStarted = false;
	bActiveUsingLiveMetadata = bUsingLiveMetadata;
	bDeferredEmitterForToken = bDelayEmitterForToken;
	ActiveSnapshotSeq = ReceiverInitialLastSeq;
	ActiveAuthToken = TargetToken;
	SetStatusMessage(
		bTargetRequiresToken && TargetToken.IsEmpty() ? TEXT("token_required") : TEXT("connecting"),
		bDelayEmitterForToken
			? TEXT("Starting receiver first to obtain auth token")
			: TEXT("Connecting receiver/emitter"));

	if (bStartReceiver)
	{
		SyncClient = MakeShared<FSyncClient>(
			this, TargetHost, TargetPort,
			Settings->Department, ClientId, ProducerState->GetSessionId(),
			Settings->ReconnectDelaySecs, ReceiverInitialLastSeq, TargetToken);
		if (SyncClient->Start()) { bActiveReceiverStarted = true; }
		else { SyncClient.Reset(); }
	}

	if (bStartEmitter && !bDelayEmitterForToken)
	{
		EmitClient = MakeShared<FEmitClient>(
			this, ClientId, ProducerState.ToSharedRef(),
			Settings->ReconnectDelaySecs, TargetToken);
		if (EmitClient->Start()) { bActiveEmitterStarted = true; }
		else { EmitClient.Reset(); }
	}
}

void UUSDConnectSubsystem::TryStartDeferredEmitter()
{
	if (!bDeferredEmitterForToken || EmitClient || !ProducerState
		|| ActiveServerHost.IsEmpty() || ActiveServerPort <= 0)
	{
		return;
	}

	const UUSDConnectSettings* Settings = GetDefault<UUSDConnectSettings>();
	if (!Settings) return;

	FString Token = ActiveAuthToken;
	if (Token.IsEmpty() && Settings->bPersistAuthTokens)
	{
		Token = LoadAuthToken(ActiveServerHost, ActiveServerPort, Settings->Department);
		ActiveAuthToken = Token;
	}
	if (Token.IsEmpty()) return;

	bDeferredEmitterForToken = false;
	EmitClient = MakeShared<FEmitClient>(
		this, ClientId, ProducerState.ToSharedRef(),
		Settings->ReconnectDelaySecs, Token);
	if (EmitClient->Start())
	{
		bActiveEmitterStarted = true;
		SetStatusMessage(TEXT("connected"), TEXT("Auth token available; emitter started"));
	}
	else
	{
		EmitClient.Reset();
		SetStatusMessage(TEXT("error"), TEXT("Failed to start deferred emitter"));
	}
}

void UUSDConnectSubsystem::Disconnect()
{
	DetachFromStageActor();
	StopClients();
	CachedStageActor = nullptr;
	LastLiveMetadataKey.Empty();
	SetStatusMessage(TEXT("not_connected"), TEXT("Disconnected"));
}

bool UUSDConnectSubsystem::Flush(float TimeoutSeconds) const
{
	if (EmitClient) return EmitClient->FlushPending(TimeoutSeconds);
	return !ProducerState
		|| (ProducerState->GetPendingTransactionCount() == 0
			&& !ProducerState->IsRecoveryRequired());
}

void UUSDConnectSubsystem::RefreshLiveMetadataFromStage(AUsdStageActor* Actor)
{
	const UUSDConnectSettings* Settings = GetDefault<UUSDConnectSettings>();
	if (!Settings || !Settings->bUseLiveMetadataFromStage) return;

	FUSDLiveOpenMetadata Metadata;
	if (!FUSDStageBridge::ReadLiveOpenMetadata(Actor, Metadata)) return;

	const FString MetadataKey = Metadata.MakeKey();
	if (MetadataKey == LastLiveMetadataKey) return;

	LastLiveMetadataKey = MetadataKey;
	UE_LOG(LogUSDConnectSubsystem, Log,
		TEXT("Detected OpenUSDConnect live metadata on stage: %s:%d snapshot_seq=%d vfs_url=%s"),
		*Metadata.Host, Metadata.Port, Metadata.SnapshotSeq, *Metadata.VfsUrl);

	if (Settings->bAutoConnect)
	{
		bPendingAutoConnect = false;
		ConnectResolved(true);
	}
}

bool UUSDConnectSubsystem::IsConnected() const
{
	return SyncClient && SyncClient->IsConnected();
}

FUSDConnectStatus UUSDConnectSubsystem::GetStatus() const
{
	FUSDConnectStatus Status;
	Status.EndpointHost = ActiveServerHost;
	Status.EndpointPort = ActiveServerPort;
	Status.bUsingLiveMetadata = bActiveUsingLiveMetadata;
	Status.SnapshotSeq = ActiveSnapshotSeq;
	Status.bReceiverStarted = bActiveReceiverStarted;
	Status.bReceiverConnected = SyncClient && SyncClient->IsConnected();
	Status.bReceiverSynchronized = bReplaySynchronized.load();
	Status.bEmitterStarted = bActiveEmitterStarted;
	Status.bEmitterConnected = EmitClient && EmitClient->IsConnected();
	if (ProducerState)
	{
		Status.SubmittedTransactions = static_cast<int64>(ProducerState->GetSubmittedTransactionCount());
		Status.AcknowledgedTransactions = static_cast<int64>(ProducerState->GetAcknowledgedTransactionCount());
		Status.PendingTransactions = static_cast<int32>(FMath::Min<uint64>(
			ProducerState->GetPendingTransactionCount(),
			static_cast<uint64>(MAX_int32)));
		Status.bRecoveryRequired = ProducerState->IsRecoveryRequired();
	}
	{
		FScopeLock Lock(&StatusCS);
		Status.AuthState = LastAuthState;
		Status.LastMessage = LastStatusMessage;
	}
	return Status;
}

FString UUSDConnectSubsystem::LoadAuthToken(
	const FString& Host,
	int32 Port,
	const FString& Department) const
{
	FString Token;
	if (GConfig)
	{
		GConfig->GetString(
			OUCAuthConfigSection,
			*MakeAuthConfigKey(Host, Port, Department),
			Token,
			GGameUserSettingsIni);
	}
	return Token;
}

void UUSDConnectSubsystem::SaveAuthToken(
	const FString& Host,
	int32 Port,
	const FString& Department,
	const FString& Token) const
{
	if (!GConfig || Token.IsEmpty()) return;
	GConfig->SetString(
		OUCAuthConfigSection,
		*MakeAuthConfigKey(Host, Port, Department),
		*Token,
		GGameUserSettingsIni);
	GConfig->Flush(false, GGameUserSettingsIni);
}

void UUSDConnectSubsystem::SetStatusMessage(const FString& AuthState, const FString& Message)
{
	FScopeLock Lock(&StatusCS);
	LastAuthState = AuthState;
	LastStatusMessage = Message;
}

void UUSDConnectSubsystem::OnClientTokenIssued(const FString& Token)
{
	if (!IsInGameThread())
	{
		TWeakObjectPtr<UUSDConnectSubsystem> WeakThis(this);
		AsyncTask(ENamedThreads::GameThread, [WeakThis, Token]()
		{
			if (WeakThis.IsValid())
			{
				WeakThis->OnClientTokenIssued(Token);
			}
		});
		return;
	}

	const UUSDConnectSettings* Settings = GetDefault<UUSDConnectSettings>();
	ActiveAuthToken = Token;
	bool bPersisted = false;
	if (Settings && Settings->bPersistAuthTokens)
	{
		SaveAuthToken(ActiveServerHost, ActiveServerPort, Settings->Department, Token);
		bPersisted = true;
	}
	SetStatusMessage(
		bPersisted ? TEXT("token_saved") : TEXT("token_issued"),
		bPersisted ? TEXT("Auth token issued and saved") : TEXT("Auth token issued for this session"));
	if (bDeferredEmitterForToken)
	{
		UE_LOG(LogUSDConnectSubsystem, Log,
			TEXT("Auth token issued; emitter will start on the next tick"));
	}
}

void UUSDConnectSubsystem::OnClientHelloOk(const FString& Role)
{
	if (!IsInGameThread())
	{
		TWeakObjectPtr<UUSDConnectSubsystem> WeakThis(this);
		AsyncTask(ENamedThreads::GameThread, [WeakThis, Role]()
		{
			if (WeakThis.IsValid())
			{
				WeakThis->OnClientHelloOk(Role);
			}
		});
		return;
	}

	SetStatusMessage(TEXT("connected"), FString::Printf(TEXT("%s connected"), *Role));
}

void UUSDConnectSubsystem::OnReceiverReplayGenerationChanged(uint64 ReplayGeneration)
{
	uint64 Current = ActiveReplayGeneration.load(std::memory_order_acquire);
	while (ReplayGeneration > Current
		&& !ActiveReplayGeneration.compare_exchange_weak(
			Current,
			ReplayGeneration,
			std::memory_order_acq_rel,
			std::memory_order_acquire))
	{
	}
	if (ReplayGeneration < Current) return;
	bReplaySynchronized.store(false);
	FScopeLock Lock(&EventQueueCS);
	EventQueue.Reset();
}

void UUSDConnectSubsystem::OnEmitterTransactionRejected(uint64 TxnId, const FString& Reason)
{
	if (!IsInGameThread())
	{
		TWeakObjectPtr<UUSDConnectSubsystem> WeakThis(this);
		AsyncTask(ENamedThreads::GameThread, [WeakThis, TxnId, Reason]()
		{
			if (WeakThis.IsValid())
			{
				WeakThis->OnEmitterTransactionRejected(TxnId, Reason);
			}
		});
		return;
	}

	SetStatusMessage(
		TEXT("recovery_required"),
		Reason.IsEmpty()
			? FString::Printf(TEXT("Transaction %llu rejected"), TxnId)
			: FString::Printf(TEXT("Transaction %llu rejected: %s"), TxnId, *Reason));
}

void UUSDConnectSubsystem::OnClientAuthRejected(const FString& Role)
{
	if (!IsInGameThread())
	{
		TWeakObjectPtr<UUSDConnectSubsystem> WeakThis(this);
		AsyncTask(ENamedThreads::GameThread, [WeakThis, Role]()
		{
			if (WeakThis.IsValid())
			{
				WeakThis->OnClientAuthRejected(Role);
			}
		});
		return;
	}

	SetStatusMessage(TEXT("auth_rejected"), FString::Printf(TEXT("%s auth rejected"), *Role));
}

void UUSDConnectSubsystem::OnClientHelloRejected(
	const FString& Role,
	const FString& Code,
	const FString& Reason)
{
	if (!IsInGameThread())
	{
		TWeakObjectPtr<UUSDConnectSubsystem> WeakThis(this);
		AsyncTask(ENamedThreads::GameThread, [WeakThis, Role, Code, Reason]()
		{
			if (WeakThis.IsValid())
			{
				WeakThis->OnClientHelloRejected(Role, Code, Reason);
			}
		});
		return;
	}

	const FString Message = Reason.IsEmpty()
		? FString::Printf(TEXT("%s connection rejected"), *Role)
		: FString::Printf(TEXT("%s connection rejected: %s"), *Role, *Reason);
	SetStatusMessage(Code.IsEmpty() ? TEXT("connection_rejected") : Code, Message);
}

void UUSDConnectSubsystem::EnqueueEvent(
	uint64 ReplayGeneration,
	TArray<uint8>&& RawBytes)
{
	FScopeLock Lock(&EventQueueCS);
	if (ReplayGeneration != ActiveReplayGeneration.load(std::memory_order_acquire)) return;
	FQueuedReceiverFrame Queued;
	Queued.ReplayGeneration = ReplayGeneration;
	Queued.Bytes = MoveTemp(RawBytes);
	EventQueue.Add(MoveTemp(Queued));
}

void UUSDConnectSubsystem::RequestReceiverReplay(const FString& Reason)
{
	const uint64 NextGeneration =
		ActiveReplayGeneration.fetch_add(1, std::memory_order_acq_rel) + 1;
	OnReceiverReplayGenerationChanged(NextGeneration);
	SetStatusMessage(TEXT("receiver_recovering"), Reason);
	if (SyncClient)
	{
		SyncClient->RequestReplayFromApplied();
	}
}

// ---------------------------------------------------------------------------
// Tick
// ---------------------------------------------------------------------------

TStatId UUSDConnectSubsystem::GetStatId() const
{
	RETURN_QUICK_DECLARE_CYCLE_STAT(UUSDConnectSubsystem, STATGROUP_OpenUSDConnect);
}

void UUSDConnectSubsystem::Tick(float DeltaTime)
{
	SCOPE_CYCLE_COUNTER(STAT_USDConnectTick);

	// Guard: don't touch anything until the world is fully initialized and not
	// being torn down. UTickableWorldSubsystem can tick during the tail end of
	// world load — running heavy work here can deadlock startup.
	UWorld* World = GetWorld();
	if (!World || !World->bIsWorldInitialized || World->bIsTearingDown)
	{
		return;
	}

	// Attach to stage actor if not already done (handles late spawning / PIE).
	AUsdStageActor* StageActor = CachedStageActor.Get();
	if (!StageActor || !IsValid(StageActor))
	{
		StageActor = FindStageActor();
		if (StageActor)
		{
			AttachToStageActor(StageActor);
		}
	}
	if (StageActor)
	{
		RefreshLiveMetadataFromStage(StageActor);
		QueueInitialMaterializations(StageActor);
	}

	// Perform deferred auto-connect after stage metadata has had a chance to
	// override the project-default endpoint.
	if (bPendingAutoConnect)
	{
		bPendingAutoConnect = false;
		ConnectResolved(true);
	}

	DrainAndApply();

	TryStartDeferredEmitter();

	// Emit any user edits captured by the USD notice listener since last tick.
	DrainAndEmit();

	// Refresh .mtlx documents for materials whose networks changed this tick
	// (received or local edits) so the engine's MaterialX rendering follows.
	ProcessPendingMaterializations();
}

// ---------------------------------------------------------------------------
// Stage actor attachment
// ---------------------------------------------------------------------------

AUsdStageActor* UUSDConnectSubsystem::FindStageActor() const
{
	// Picks the first AUsdStageActor in the world. With multiple stage actors
	// (e.g. one per layer file), only the first one is live-synced. See the
	// "Single stage actor" entry in PLUGIN_DEV.md.
	UWorld* World = GetWorld();
	if (!World) return nullptr;
	for (TActorIterator<AUsdStageActor> It(World); It; ++It)
	{
		if (*It) return *It;
	}
	return nullptr;
}

void UUSDConnectSubsystem::AttachToStageActor(AUsdStageActor* Actor)
{
	DetachFromStageActor();
	CachedStageActor = Actor;

	// Subscribe to the engine's USD notice wrapper. Its delegate fires with
	// exact SdfPaths (info-changes + resyncs), unlike OnPrimChanged which
	// rolls every change up to the nearest KindsToCollapse ancestor.
	FUsdListener& Listener = Actor->GetUsdListener();
	ObjectsChangedHandle = Listener.GetOnObjectsChanged().AddLambda(
		[this](const UsdUtils::FObjectChangesByPath& InfoChanges,
		       const UsdUtils::FObjectChangesByPath& ResyncChanges)
	{
		// Echo guard: drop notices generated by our own DrainAndApply.
		if (bSuppressEmit.load()) return;

		FScopeLock Lock(&PendingEmitPathsCS);
		auto Collect = [this](const UsdUtils::FObjectChangesByPath& Map)
		{
			for (const auto& Pair : Map)
			{
				const FString& Path = Pair.Key;
				if (Path.IsEmpty() || Path == TEXT("/")) continue;

				// Strip property suffix ".attrName" — we want the prim path.
				// Changed "inputs:*" properties keep their name so the drain
				// can emit just the edited shader inputs.
				int32 DotIdx = INDEX_NONE;
				if (Path.FindChar(TEXT('.'), DotIdx))
				{
					FString PrimPath = Path.Left(DotIdx);
					FString PropName = Path.RightChop(DotIdx + 1);
					if (PropName.StartsWith(TEXT("inputs:")))
					{
						PendingEmitInputs.FindOrAdd(PrimPath).Add(MoveTemp(PropName));
					}
					PendingEmitPaths.Add(MoveTemp(PrimPath));
				}
				else
				{
					PendingEmitPaths.Add(Path);
				}
			}
		};
		Collect(InfoChanges);
		Collect(ResyncChanges);
	});

	UE_LOG(LogUSDConnectSubsystem, Log,
		TEXT("Attached to AUsdStageActor (%s) — subscribed to FUsdListener::OnObjectsChanged"),
		*Actor->GetName());
}

void UUSDConnectSubsystem::DetachFromStageActor()
{
	if (AUsdStageActor* Actor = CachedStageActor.Get())
	{
		if (ObjectsChangedHandle.IsValid())
		{
			Actor->GetUsdListener().GetOnObjectsChanged().Remove(ObjectsChangedHandle);
		}
	}
	ObjectsChangedHandle.Reset();

	{
		FScopeLock Lock(&PendingEmitPathsCS);
		PendingEmitPaths.Reset();
		PendingEmitInputs.Reset();
	}

	EmittedXformPrims.Reset();
	CachedStageActor = nullptr;
	LastMaterializedRootLayerIdentifier.Empty();
}

// ---------------------------------------------------------------------------
// DrainAndApply (receiver → USD stage)
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::DrainAndApply()
{
	AUsdStageActor* StageActor = CachedStageActor.Get();
	if (!StageActor || !IsValid(StageActor))
	{
		// Keep the HELLO replay until a stage appears, but never truncate it: a
		// truncated prefix cannot safely advance the applied sequence cursor.
		constexpr int32 MaxBufferedFrames = 5000;
		bool bOverflowed = false;
		{
			FScopeLock Lock(&EventQueueCS);
			bOverflowed = EventQueue.Num() > MaxBufferedFrames;
			if (bOverflowed) EventQueue.Reset();
		}
		if (bOverflowed)
		{
			RequestReceiverReplay(
				TEXT("Receiver queue overflowed before a USD stage was available; replay requested from the last applied sequence"));
		}
		return;
	}

	// Apply in bounded chunks. Each ApplyFrame triggers AUsdStageActor to
	// resync Unreal scene components for the affected prim — when the HELLO
	// replay delivers thousands of events, applying them all on a single
	// tick freezes the editor for seconds. Cap by both count and wall-time
	// so a single tick stays interactive; the rest stays queued for the
	// next tick.
	constexpr int32  MaxApplyPerTick        = 512;
	constexpr double MaxApplySecondsPerTick = 0.016; // 16 ms — preserves ~60 fps

	int32 ChunkSize = 0;
	{
		FScopeLock Lock(&EventQueueCS);
		ChunkSize = FMath::Min(EventQueue.Num(), MaxApplyPerTick);
	}
	if (ChunkSize == 0) return;

	TArray<FQueuedReceiverFrame> Chunk;
	Chunk.Reserve(ChunkSize);
	{
		FScopeLock Lock(&EventQueueCS);
		for (int32 i = 0; i < ChunkSize && i < EventQueue.Num(); ++i)
		{
			Chunk.Add(MoveTemp(EventQueue[i]));
		}
		EventQueue.RemoveAt(0, ChunkSize, EAllowShrinking::No);
	}

	const double Start = FPlatformTime::Seconds();
	int32 Applied = 0;
	bool bNeedsReplay = false;
	bool bGenerationChanged = false;
	FString RecoveryReason;

	bSuppressEmit.store(true);
	{
		// Contiguous value events share one SdfChangeBlock so their notices
		// fire as a single batch — the stage actor's per-notice component
		// resync dominates replay throughput. Structural events cannot apply
		// inside a block (UsdStage::DefinePrim and composition-arc edits need
		// the stage to recompose immediately), so the run's block closes
		// before one applies and a fresh block opens for the next value run.
		TUniquePtr<FUSDEventChangeBlock> RunBlock;
		for (FQueuedReceiverFrame& Queued : Chunk)
		{
			if (Queued.ReplayGeneration
				!= ActiveReplayGeneration.load(std::memory_order_acquire))
			{
				bGenerationChanged = true;
				break;
			}
			TArray<uint8>& Frame = Queued.Bytes;
			const OpenUSDConnect::Envelope* Envelope = OUC::GetEnvelopeFromFrame(Frame);
			if (!Envelope)
			{
				bNeedsReplay = true;
				RecoveryReason = TEXT("Receiver dequeued an invalid frame");
				break;
			}
			if (Envelope && Envelope->payload_type() == OpenUSDConnect::Payload::Resync)
			{
				RunBlock.Reset();
				if (SyncClient) SyncClient->ResetAppliedProgress();
				bReplaySynchronized.store(false);
				++Applied;
				continue;
			}
			if (Envelope
				&& Envelope->payload_type() == OpenUSDConnect::Payload::ReplayComplete)
			{
				RunBlock.Reset();
				const OpenUSDConnect::ReplayComplete* Complete =
					Envelope->payload_as_ReplayComplete();
				if (!Complete)
				{
					bNeedsReplay = true;
					RecoveryReason = TEXT("Receiver could not decode ReplayComplete");
					break;
				}
				const int32 AppliedSeq = SyncClient ? SyncClient->GetLastAppliedSeq() : 0;
				if (AppliedSeq != Complete->head_seq())
				{
					bNeedsReplay = true;
					RecoveryReason = FString::Printf(
						TEXT("ReplayComplete advertised seq=%d but applied cursor is seq=%d"),
						Complete->head_seq(), AppliedSeq);
					break;
				}
				ReplayHeadSeq = Complete->head_seq();
				ReplayEpoch = Complete->epoch();
				bReplaySynchronized.store(true);
				UE_LOG(LogUSDConnectSubsystem, Log,
					TEXT("Receiver replay applied through seq=%d epoch=%llu — publishing enabled"),
					ReplayHeadSeq,
					static_cast<unsigned long long>(ReplayEpoch));
				++Applied;
				continue;
			}
			if (Envelope->payload_type() != OpenUSDConnect::Payload::BroadcastEvent)
			{
				bNeedsReplay = true;
				RecoveryReason = TEXT("Unexpected payload entered the receiver apply queue");
				break;
			}

			const OpenUSDConnect::BroadcastEvent* Broadcast =
				Envelope->payload_as_BroadcastEvent();
			if (!Broadcast || !SyncClient)
			{
				bNeedsReplay = true;
				RecoveryReason = TEXT("Receiver could not decode or route a broadcast event");
				break;
			}
			const int32 Seq = Broadcast->seq();
			const int32 LastApplied = SyncClient->GetLastAppliedSeq();
			if (Seq <= LastApplied)
			{
				++Applied;
				continue;
			}
			if (Seq != LastApplied + 1)
			{
				bNeedsReplay = true;
				RecoveryReason = FString::Printf(
					TEXT("Receiver apply gap: expected=%d dequeued=%d"),
					LastApplied + 1, Seq);
				break;
			}

			const FString Origin = OUC::ToFString(Broadcast->origin());
			const bool bOwnEcho = ProducerState
				&& !Origin.IsEmpty()
				&& Origin == ProducerState->GetSessionId();
			if (FUSDEventApplier::FrameUsesChangeBlock(Frame))
			{
				if (!RunBlock)
				{
					RunBlock = MakeUnique<FUSDEventChangeBlock>();
				}
			}
			else
			{
				RunBlock.Reset();
			}
			FString TouchedPrim;
			OpenUSDConnect::EventPayload EventKind = OpenUSDConnect::EventPayload::NONE;
			if (!bOwnEcho
				&& !FUSDEventApplier::ApplyFrame(Frame, StageActor, &TouchedPrim, &EventKind))
			{
				RunBlock.Reset();
				bNeedsReplay = true;
				RecoveryReason = FString::Printf(
					TEXT("Failed to apply receiver sequence %d"), Seq);
				break;
			}
			SyncClient->MarkAppliedThrough(Seq);
			// Received network edits dirty their owning material for the
			// materializer. EnsurePrim is included because shader-node
			// creation changes the network without a connectable event.
			if (!TouchedPrim.IsEmpty()
				&& (EventKind == OpenUSDConnect::EventPayload::SetConnectableInput
					|| EventKind == OpenUSDConnect::EventPayload::SetConnectableConnection
					|| EventKind == OpenUSDConnect::EventPayload::EnsurePrim))
			{
				PendingMaterializePrims.Add(MoveTemp(TouchedPrim));
			}
			++Applied;
			if (FPlatformTime::Seconds() - Start > MaxApplySecondsPerTick)
			{
				break;
			}
		}
	}
	bSuppressEmit.store(false);
	if (bGenerationChanged)
	{
		return;
	}
	if (bNeedsReplay)
	{
		RequestReceiverReplay(RecoveryReason);
		return;
	}

	// If we hit the time budget before draining the chunk, push the unprocessed
	// tail back to the front of the queue so we resume next tick.
	if (Applied < Chunk.Num())
	{
		FScopeLock Lock(&EventQueueCS);
		const int32 Remaining = Chunk.Num() - Applied;
		TArray<FQueuedReceiverFrame> Merged;
		Merged.Reserve(Remaining + EventQueue.Num());
		for (int32 i = Applied; i < Chunk.Num(); ++i)
		{
			Merged.Add(MoveTemp(Chunk[i]));
		}
		for (FQueuedReceiverFrame& Frame : EventQueue)
		{
			Merged.Add(MoveTemp(Frame));
		}
		EventQueue = MoveTemp(Merged);
	}

	int32 QueueRemaining = 0;
	{
		FScopeLock Lock(&EventQueueCS);
		QueueRemaining = EventQueue.Num();
	}
	UE_LOG(LogUSDConnectSubsystem, Verbose,
		TEXT("Applied %d event(s) this tick (queue remaining: %d)"),
		Applied, QueueRemaining);
}

// ---------------------------------------------------------------------------
// DrainAndEmit (emitter ← USD stage, via TfNotice listener)
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::DrainAndEmit()
{
	if (!EmitClient || !EmitClient->IsConnected()) return;
	if (!bReplaySynchronized.load()) return;
	if (bSuppressEmit.load()) return;

	const uint64 ConnectionGeneration = EmitClient->GetConnectionGeneration();
	if (ConnectionGeneration != LastEmitConnectionGeneration)
	{
		// A replacement server has no guarantee that it saw prerequisites from
		// the previous TCP connection. Requeue the current values and make their
		// first transaction on this connection self-contained.
		{
			FScopeLock Lock(&PendingEmitPathsCS);
			for (const FString& PrimPath : EmittedXformPrims)
			{
				PendingEmitPaths.Add(PrimPath);
			}
		}
		EmittedXformPrims.Reset();
		LastEmitConnectionGeneration = ConnectionGeneration;
	}

	AUsdStageActor* StageActor = CachedStageActor.Get();
	if (!StageActor || !IsValid(StageActor)) return;

	TSet<FString> Changed;
	TMap<FString, TSet<FString>> ChangedInputs;
	{
		FScopeLock Lock(&PendingEmitPathsCS);
		if (PendingEmitPaths.Num() == 0 && PendingEmitInputs.Num() == 0) return;
		Changed = MoveTemp(PendingEmitPaths);
		ChangedInputs = MoveTemp(PendingEmitInputs);
		PendingEmitPaths.Reset();
		PendingEmitInputs.Reset();
	}

	UE_LOG(LogUSDConnectSubsystem, Verbose,
		TEXT("Draining %d changed prim path(s) from FUsdListener"), Changed.Num());

	for (const FString& Path : Changed)
	{
		EmitPrimChange(StageActor, Path);
	}
	for (const auto& Pair : ChangedInputs)
	{
		// Edits on a Material's document-projected interface inputs are
		// local artifacts; reroute them onto the inline shader instead of
		// emitting an orphan material-level event. The shader authoring
		// re-enters this path next tick and emits/rematerializes normally.
		if (FUSDMaterialXMaterializer::RerouteMaterialInterfaceEdit(StageActor, Pair.Key, Pair.Value))
		{
			continue;
		}
		EmitConnectableInputs(StageActor, Pair.Key, Pair.Value);
		// Local shader edits also dirty their owning material so the
		// materializer refreshes the local .mtlx document.
		PendingMaterializePrims.Add(Pair.Key);
	}
}

void UUSDConnectSubsystem::EmitPrimChange(AUsdStageActor* StageActor, const FString& PrimPath)
{
	// Try to read and emit TRS
	{
		FEmitXformTrs Xform;
		bool bFromMatrixOp = false;
		if (FUSDStageBridge::ReadXformTrs(StageActor, PrimPath, Xform, &bFromMatrixOp))
		{
			if (bFromMatrixOp)
			{
				// Suppress our own listener: the restore fires notices, but
				// it re-authors the exact values being emitted below.
				bSuppressEmit.store(true);
				FUSDStageBridge::RestoreCanonicalXformOps(StageActor, PrimPath, Xform);
				bSuppressEmit.store(false);
			}

			TArray<FEmitXformTrs> Batch = { Xform };
			const bool bIncludeEnsureXformOps = !EmittedXformPrims.Contains(PrimPath);
			const uint64 TxnId = ProducerState->GetNextTransactionId();
			TArray<uint8> Frame = BuildXformTxnFrame(
				TxnId, Batch, bIncludeEnsureXformOps);
			UE_LOG(LogUSDConnectSubsystem, Verbose,
				TEXT("EmitPrimChange(%s): TRS frame built (%d bytes, fields=0x%02x%s%s); enqueueing"),
				*PrimPath, Frame.Num(), Xform.Fields,
				bFromMatrixOp ? TEXT(", decomposed from matrix op") : TEXT(""),
				bIncludeEnsureXformOps ? TEXT(", includes ensure_xform_ops") : TEXT(""));
			if (Frame.Num() > 0 && EmitClient->EnqueueFrame(TxnId, MoveTemp(Frame)))
			{
				EmittedXformPrims.Add(PrimPath);
			}
		}
	}

	// Try to read and emit visibility
	{
		FEmitVisibility Vis;
		if (FUSDStageBridge::ReadVisibility(StageActor, PrimPath, Vis))
		{
			TArray<FEmitVisibility> Batch = { Vis };
			const uint64 TxnId = ProducerState->GetNextTransactionId();
			TArray<uint8> Frame = BuildVisibilityTxnFrame(
				TxnId, Batch);
			UE_LOG(LogUSDConnectSubsystem, Verbose,
				TEXT("EmitPrimChange(%s): Visibility frame built (%d bytes, visible=%d) — enqueueing"),
				*PrimPath, Frame.Num(), Vis.bVisible ? 1 : 0);
			if (Frame.Num() > 0) EmitClient->EnqueueFrame(TxnId, MoveTemp(Frame));
		}
	}
}

void UUSDConnectSubsystem::EmitConnectableInputs(
	AUsdStageActor* StageActor, const FString& PrimPath, const TSet<FString>& InputAttrNames)
{
	FEmitConnectableInput Event;
	if (!FUSDStageBridge::ReadConnectableInputs(StageActor, PrimPath, InputAttrNames, Event))
	{
		return;
	}

	TArray<FEmitConnectableInput> Batch = { MoveTemp(Event) };
	const uint64 TxnId = ProducerState->GetNextTransactionId();
	TArray<uint8> Frame = BuildConnectableInputTxnFrame(
		TxnId, Batch);
	UE_LOG(LogUSDConnectSubsystem, Verbose,
		TEXT("EmitConnectableInputs(%s): %d input(s), frame %d bytes — enqueueing"),
		*PrimPath, Batch[0].Inputs.Num(), Frame.Num());
	if (Frame.Num() > 0) EmitClient->EnqueueFrame(TxnId, MoveTemp(Frame));
}

// ---------------------------------------------------------------------------
// Materialization (local .mtlx documents for MaterialX rendering)
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::QueueInitialMaterializations(AUsdStageActor* Actor)
{
	FString RootIdentifier;
	TArray<FString> Materials;
	if (!FUSDStageBridge::ReadMaterialXMaterials(Actor, RootIdentifier, Materials))
	{
		return;
	}
	if (RootIdentifier.IsEmpty() || RootIdentifier == LastMaterializedRootLayerIdentifier)
	{
		return;
	}

	LastMaterializedRootLayerIdentifier = RootIdentifier;

	if (Materials.IsEmpty())
	{
		return;
	}

	int32 Changed = 0;
	bSuppressEmit.store(true);
	{
#if WITH_EDITOR
		const FScopedTransaction Transaction(
			NSLOCTEXT("OpenUSDConnect", "InitialMaterialXRefresh", "Prepare live MaterialX materials"));
#endif

		for (const FString& Material : Materials)
		{
			if (FUSDMaterialXMaterializer::MaterializeMaterial(Actor, Material))
			{
				++Changed;
			}
		}
	}

	const FName MaterialXRenderContext(TEXT("mtlx"));
	const bool bChangedRenderContext = Actor->RenderContext != MaterialXRenderContext;
	if (bChangedRenderContext)
	{
		// The materialization transaction has ended, so its accumulated USD
		// notices are fully processed before SetRenderContext reloads the stage.
		Actor->SetRenderContext(MaterialXRenderContext);
	}
	bSuppressEmit.store(false);

	UE_LOG(LogUSDConnectSubsystem, Log,
		TEXT("Initial OpenUSDConnect MaterialX refresh scanned %d material(s), updated %d, render_context=%s"),
		Materials.Num(), Changed, bChangedRenderContext ? TEXT("mtlx (selected)") : TEXT("mtlx"));
}

void UUSDConnectSubsystem::ProcessPendingMaterializations()
{
	if (PendingMaterializePrims.IsEmpty())
	{
		return;
	}

	AUsdStageActor* StageActor = CachedStageActor.Get();
	if (!StageActor || !IsValid(StageActor))
	{
		PendingMaterializePrims.Reset();
		return;
	}

	TSet<FString> Sources = MoveTemp(PendingMaterializePrims);
	PendingMaterializePrims.Reset();

	TSet<FString> Materials;
	for (const FString& Path : Sources)
	{
		FString Material = FUSDMaterialXMaterializer::FindOwningMaterial(StageActor, Path);
		if (!Material.IsEmpty())
		{
			Materials.Add(MoveTemp(Material));
		}
	}
	if (Materials.IsEmpty())
	{
		return;
	}

	{
#if WITH_EDITOR
	const FScopedTransaction Transaction(
		NSLOCTEXT("OpenUSDConnect", "LiveMaterialXRefresh", "Refresh live MaterialX materials"));
#endif

	// The session-layer authoring below fires stage notices; suppress our own
	// listener so they don't loop back into the emit path. The stage actor's
	// listener still sees them — that's what triggers the re-import.
	bSuppressEmit.store(true);
	for (const FString& Material : Materials)
	{
		FUSDMaterialXMaterializer::MaterializeMaterial(StageActor, Material);
	}
	}
	bSuppressEmit.store(false);
}
