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
		// Discard stale events
		FScopeLock Lock(&EventQueueCS);
		EventQueue.Reset();
		return;
	}

	TArray<TArray<uint8>> LocalQueue;
	{
		FScopeLock Lock(&EventQueueCS);
		LocalQueue = MoveTemp(EventQueue);
	}

	UE_LOG(LogUSDConnectSubsystem, Verbose,
		TEXT("Applying %d incoming event(s) to stage"), LocalQueue.Num());

	// Suppress emitter while applying to prevent echo
	bSuppressEmit.store(true);
	for (TArray<uint8>& Frame : LocalQueue)
	{
		FUSDEventApplier::ApplyFrame(Frame, StageActor);
	}
	bSuppressEmit.store(false);
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
