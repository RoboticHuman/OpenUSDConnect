// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "USDConnectSubsystem.h"

#include "SyncClient.h"
#include "EmitClient.h"
#include "USDConnectSettings.h"
#include "USDEventApplier.h"
#include "TxnBuilder.h"

#include "USDStageActor.h"
#include "USDListener.h"
#include "EngineUtils.h"
#include "Logging/LogMacros.h"
#include "Stats/Stats.h"
#include "Misc/Guid.h"
#include "Misc/App.h"
#include "Misc/Crc.h"
#include "HAL/PlatformProcess.h"

#if USE_USD_SDK
#include "USDIncludesStart.h"
#include "pxr/usd/sdf/changeBlock.h"
#include "pxr/usd/usd/stage.h"
#include "pxr/usd/usd/prim.h"
#include "pxr/usd/usd/timeCode.h"
#include "pxr/usd/usdGeom/xformable.h"
#include "pxr/usd/usdGeom/xformOp.h"
#include "pxr/usd/usdGeom/imageable.h"
#include "pxr/usd/usdGeom/tokens.h"
#include "pxr/base/gf/vec3d.h"
#include "pxr/base/gf/vec3f.h"
#include "pxr/base/gf/quatf.h"
#include "USDIncludesEnd.h"
#endif

DEFINE_LOG_CATEGORY_STATIC(LogUSDConnectSubsystem, Log, All);

DECLARE_STATS_GROUP(TEXT("OpenUSDConnect"), STATGROUP_OpenUSDConnect, STATCAT_Advanced);
DECLARE_CYCLE_STAT(TEXT("USDConnect Tick"), STAT_USDConnectTick, STATGROUP_OpenUSDConnect);

// ---------------------------------------------------------------------------
// Helpers to read TRS/visibility from the pxr stage
// ---------------------------------------------------------------------------
#if USE_USD_SDK
static bool ReadXformTrs(pxr::UsdStageRefPtr& Stage, const FString& PrimPath, FEmitXformTrs& OutTrs)
{
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(pxr::SdfPath(TCHAR_TO_UTF8(*PrimPath)));
	if (!Prim)
	{
		UE_LOG(LogUSDConnectSubsystem, Verbose,
			TEXT("ReadXformTrs(%s): no prim at path"), *PrimPath);
		return false;
	}

	pxr::UsdGeomXformable Xformable(Prim);
	if (!Xformable)
	{
		UE_LOG(LogUSDConnectSubsystem, Verbose,
			TEXT("ReadXformTrs(%s): prim is not Xformable (typeName=%s)"),
			*PrimPath, UTF8_TO_TCHAR(Prim.GetTypeName().GetText()));
		return false;
	}

	bool bResetXformStack = false;
	std::vector<pxr::UsdGeomXformOp> Ops = Xformable.GetOrderedXformOps(&bResetXformStack);
	if (Ops.empty())
	{
		UE_LOG(LogUSDConnectSubsystem, Verbose,
			TEXT("ReadXformTrs(%s): Xformable has no ordered xform ops — nothing to emit"),
			*PrimPath);
		return false;
	}

	pxr::UsdTimeCode Time = pxr::UsdTimeCode::Default();
	OutTrs.PrimPath = PrimPath;
	OutTrs.Fields   = 0;

	for (auto& Op : Ops)
	{
		const auto OpType = Op.GetOpType();
		if (OpType == pxr::UsdGeomXformOp::TypeTranslate)
		{
			pxr::GfVec3d T(0, 0, 0);
			Op.Get(&T, Time);
			OutTrs.T[0] = (float)T[0];
			OutTrs.T[1] = (float)T[1];
			OutTrs.T[2] = (float)T[2];
			OutTrs.Fields |= 1;
		}
		else if (OpType == pxr::UsdGeomXformOp::TypeOrient)
		{
			pxr::GfQuatf R(1, 0, 0, 0);
			Op.Get(&R, Time);
			OutTrs.R[0] = R.GetReal();
			auto Im = R.GetImaginary();
			OutTrs.R[1] = Im[0]; OutTrs.R[2] = Im[1]; OutTrs.R[3] = Im[2];
			OutTrs.Fields |= 2;
		}
		else if (OpType == pxr::UsdGeomXformOp::TypeScale)
		{
			pxr::GfVec3f S(1, 1, 1);
			Op.Get(&S, Time);
			OutTrs.S[0] = S[0]; OutTrs.S[1] = S[1]; OutTrs.S[2] = S[2];
			OutTrs.Fields |= 4;
		}
	}
	return OutTrs.Fields != 0;
}

static bool ReadVisibility(pxr::UsdStageRefPtr& Stage, const FString& PrimPath, FEmitVisibility& OutVis)
{
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(pxr::SdfPath(TCHAR_TO_UTF8(*PrimPath)));
	if (!Prim)
	{
		UE_LOG(LogUSDConnectSubsystem, Verbose,
			TEXT("ReadVisibility(%s): no prim at path"), *PrimPath);
		return false;
	}

	pxr::UsdGeomImageable Imageable(Prim);
	if (!Imageable)
	{
		UE_LOG(LogUSDConnectSubsystem, Verbose,
			TEXT("ReadVisibility(%s): prim is not Imageable"), *PrimPath);
		return false;
	}

	pxr::TfToken VisToken;
	Imageable.GetVisibilityAttr().Get(&VisToken, pxr::UsdTimeCode::Default());
	OutVis.PrimPath = PrimPath;
	OutVis.bVisible = (VisToken != pxr::UsdGeomTokens->invisible);
	return true;
}
#endif // USE_USD_SDK

// ---------------------------------------------------------------------------
// UWorldSubsystem lifecycle
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::Initialize(FSubsystemCollectionBase& Collection)
{
	Super::Initialize(Collection);
	bSuppressEmit.store(false);

	// Generate stable ClientId + session origin (same for both connections)
	{
		const FString MachineId = FPlatformProcess::ComputerName();
		const FString ProjectId = FApp::GetProjectName();
		const FString Combined  = MachineId + ProjectId;
		const uint32  Hash      = FCrc::MemCrc32(TCHAR_TO_UTF8(*Combined), Combined.Len());
		ClientId      = FString::Printf(TEXT("unreal-%08x-%s"), Hash, *MachineId);
		SessionOrigin = FGuid::NewGuid().ToString(EGuidFormats::Digits);
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
	if (SyncClient)
	{
		UE_LOG(LogUSDConnectSubsystem, Warning, TEXT("Already connected"));
		return;
	}

	const UUSDConnectSettings* Settings = GetDefault<UUSDConnectSettings>();
	if (!Settings) return;

	UE_LOG(LogUSDConnectSubsystem, Log, TEXT("Connecting to %s:%d (client_id=%s)"),
		*Settings->ServerHost, Settings->ServerPort, *ClientId);

	// Both threads share the same ClientId and SessionOrigin.
	// The server uses origin to route corrections; we use it to suppress echo.
	SyncClient = MakeShared<FSyncClient>(
		this, Settings->ServerHost, Settings->ServerPort,
		Settings->Department, ClientId, SessionOrigin,
		Settings->ReconnectDelaySecs);
	if (!SyncClient->Start()) { SyncClient.Reset(); return; }

	// Emitter thread — shares the same ClientId/SessionOrigin so the server
	// can correlate the two connections to the same logical client
	EmitClient = MakeShared<FEmitClient>(
		Settings->ServerHost, Settings->ServerPort,
		Settings->Department, ClientId, SessionOrigin,
		Settings->ReconnectDelaySecs);
	if (!EmitClient->Start()) { EmitClient.Reset(); }
}

void UUSDConnectSubsystem::Disconnect()
{
	DetachFromStageActor();
	if (SyncClient) { SyncClient->StopAndWait(); SyncClient.Reset(); }
	if (EmitClient) { EmitClient->StopAndWait(); EmitClient.Reset(); }
	CachedStageActor = nullptr;
}

bool UUSDConnectSubsystem::IsConnected() const
{
	return SyncClient && SyncClient->IsConnected();
}

void UUSDConnectSubsystem::EnqueueEvent(TArray<uint8>&& RawBytes)
{
	FScopeLock Lock(&EventQueueCS);
	EventQueue.Add(MoveTemp(RawBytes));
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

	// Perform deferred auto-connect on the first safe tick.
	if (bPendingAutoConnect)
	{
		bPendingAutoConnect = false;
		Connect();
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

	if (!EventQueue.IsEmpty())
	{
		DrainAndApply();
	}

	// Emit any user edits captured by the USD notice listener since last tick.
	DrainAndEmit();
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
				int32 DotIdx = INDEX_NONE;
				if (Path.FindChar(TEXT('.'), DotIdx))
				{
					PendingEmitPaths.Add(Path.Left(DotIdx));
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
	}

	CachedStageActor = nullptr;
}

// ---------------------------------------------------------------------------
// DrainAndApply (receiver → USD stage)
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::DrainAndApply()
{
	AUsdStageActor* StageActor = CachedStageActor.Get();
	if (!StageActor || !IsValid(StageActor))
	{
		// Don't discard: the queue holds the HELLO replay that arrived before
		// the user imported their stage. Cap the buffer so we don't grow
		// without bound if a stage never appears; FIFO-evict the oldest.
		constexpr int32 MaxBufferedFrames = 5000;
		FScopeLock Lock(&EventQueueCS);
		if (EventQueue.Num() > MaxBufferedFrames)
		{
			const int32 ToDrop = EventQueue.Num() - MaxBufferedFrames;
			EventQueue.RemoveAt(0, ToDrop, EAllowShrinking::No);
		}
		return;
	}

	// Apply in bounded chunks. Each ApplyFrame triggers AUsdStageActor to
	// resync Unreal scene components for the affected prim — when the HELLO
	// replay delivers thousands of events, applying them all on a single
	// tick freezes the editor for seconds. Cap by both count and wall-time
	// so a single tick stays interactive; the rest stays queued for the
	// next tick.
	//
	// The chunk is wrapped in a single outer pxr::SdfChangeBlock so that all
	// stage notices fire once per tick instead of once per event — the stage
	// actor's component-resync cost dominates per-event work, and batching
	// the notice into one dispatch is the main throughput lever during a
	// HELLO replay.
	constexpr int32  MaxApplyPerTick        = 512;
	constexpr double MaxApplySecondsPerTick = 0.016; // 16 ms — preserves ~60 fps

	int32 ChunkSize = 0;
	{
		FScopeLock Lock(&EventQueueCS);
		ChunkSize = FMath::Min(EventQueue.Num(), MaxApplyPerTick);
	}
	if (ChunkSize == 0) return;

	TArray<TArray<uint8>> Chunk;
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

	bSuppressEmit.store(true);
	{
#if USE_USD_SDK
		// Outer SdfChangeBlock — nests with the per-frame block inside ApplyFrame.
		// Notices only fire when the outermost block destructs, so the stage
		// actor processes the whole chunk's worth of changes in one batch
		// rather than once per event.
		pxr::SdfChangeBlock BatchBlock;
#endif
		for (TArray<uint8>& Frame : Chunk)
		{
			FUSDEventApplier::ApplyFrame(Frame, StageActor);
			++Applied;
			if (FPlatformTime::Seconds() - Start > MaxApplySecondsPerTick)
			{
				break;
			}
		}
	}
	bSuppressEmit.store(false);

	// If we hit the time budget before draining the chunk, push the unprocessed
	// tail back to the front of the queue so we resume next tick.
	if (Applied < Chunk.Num())
	{
		FScopeLock Lock(&EventQueueCS);
		const int32 Remaining = Chunk.Num() - Applied;
		TArray<TArray<uint8>> Merged;
		Merged.Reserve(Remaining + EventQueue.Num());
		for (int32 i = Applied; i < Chunk.Num(); ++i)
		{
			Merged.Add(MoveTemp(Chunk[i]));
		}
		for (TArray<uint8>& Frame : EventQueue)
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
	if (bSuppressEmit.load()) return;

	AUsdStageActor* StageActor = CachedStageActor.Get();
	if (!StageActor || !IsValid(StageActor)) return;

	TSet<FString> Changed;
	{
		FScopeLock Lock(&PendingEmitPathsCS);
		if (PendingEmitPaths.Num() == 0) return;
		Changed = MoveTemp(PendingEmitPaths);
		PendingEmitPaths.Reset();
	}

	UE_LOG(LogUSDConnectSubsystem, Verbose,
		TEXT("Draining %d changed prim path(s) from FUsdListener"), Changed.Num());

	for (const FString& Path : Changed)
	{
		EmitPrimChange(StageActor, Path);
	}
}

void UUSDConnectSubsystem::EmitPrimChange(AUsdStageActor* StageActor, const FString& PrimPath)
{
#if USE_USD_SDK
	pxr::UsdStageRefPtr PxrStage = static_cast<pxr::UsdStageRefPtr>(
		StageActor->GetOrOpenUsdStage());
	if (!PxrStage) return;

	// Try to read and emit TRS
	{
		FEmitXformTrs Xform;
		if (ReadXformTrs(PxrStage, PrimPath, Xform))
		{
			TArray<FEmitXformTrs> Batch = { Xform };
			TArray<uint8> Frame = BuildXformTxnFrame(ClientId, Batch);
			UE_LOG(LogUSDConnectSubsystem, Verbose,
				TEXT("EmitPrimChange(%s): TRS frame built (%d bytes, fields=0x%02x) — enqueueing"),
				*PrimPath, Frame.Num(), Xform.Fields);
			if (Frame.Num() > 0)
			{
				EmitClient->EnqueueFrame(MoveTemp(Frame));
			}
		}
	}

	// Try to read and emit visibility
	{
		FEmitVisibility Vis;
		if (ReadVisibility(PxrStage, PrimPath, Vis))
		{
			TArray<FEmitVisibility> Batch = { Vis };
			TArray<uint8> Frame = BuildVisibilityTxnFrame(ClientId, Batch);
			UE_LOG(LogUSDConnectSubsystem, Verbose,
				TEXT("EmitPrimChange(%s): Visibility frame built (%d bytes, visible=%d) — enqueueing"),
				*PrimPath, Frame.Num(), Vis.bVisible ? 1 : 0);
			if (Frame.Num() > 0)
			{
				EmitClient->EnqueueFrame(MoveTemp(Frame));
			}
		}
	}
#endif
}
