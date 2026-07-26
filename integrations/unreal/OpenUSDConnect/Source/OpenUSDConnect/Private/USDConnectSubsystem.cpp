// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "USDConnectSubsystem.h"

#include "SyncClient.h"
#include "EmitClient.h"
#include "USDConnectSettings.h"
#include "USDEventApplier.h"
#include "USDMaterialXMaterializer.h"
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
#include <cstdint>

#if WITH_EDITOR
#include "ScopedTransaction.h"
#endif

#if USE_USD_SDK
#include "USDIncludesStart.h"
#include "pxr/base/vt/dictionary.h"
#include "pxr/usd/sdf/changeBlock.h"
#include "pxr/usd/sdf/layer.h"
#include "pxr/usd/sdf/valueTypeName.h"
#include "pxr/usd/usd/stage.h"
#include "pxr/usd/usd/prim.h"
#include "pxr/usd/usd/primRange.h"
#include "pxr/usd/usd/timeCode.h"
#include "pxr/usd/usdGeom/xformable.h"
#include "pxr/usd/usdGeom/xformOp.h"
#include "pxr/usd/usdGeom/imageable.h"
#include "pxr/usd/usdGeom/tokens.h"
#include "pxr/usd/usdShade/connectableAPI.h"
#include "pxr/usd/usdShade/material.h"
#include "pxr/usd/usdShade/shader.h"
#include "pxr/usd/sdf/assetPath.h"
#include "pxr/base/gf/matrix4d.h"
#include "pxr/base/gf/transform.h"
#include "pxr/base/gf/vec2f.h"
#include "pxr/base/gf/vec3d.h"
#include "pxr/base/gf/vec3f.h"
#include "pxr/base/gf/vec4f.h"
#include "pxr/base/gf/quatd.h"
#include "pxr/base/gf/quatf.h"
#include "pxr/base/vt/array.h"
#include "pxr/base/vt/value.h"
#include "USDIncludesEnd.h"
#endif

DEFINE_LOG_CATEGORY_STATIC(LogUSDConnectSubsystem, Log, All);

DECLARE_STATS_GROUP(TEXT("OpenUSDConnect"), STATGROUP_OpenUSDConnect, STATCAT_Advanced);
DECLARE_CYCLE_STAT(TEXT("USDConnect Tick"), STAT_USDConnectTick, STATGROUP_OpenUSDConnect);

// ---------------------------------------------------------------------------
// Helpers to read TRS/visibility from the pxr stage
// ---------------------------------------------------------------------------
#if USE_USD_SDK
struct FLiveOpenMetadata
{
	FString LayerIdentifier;
	FString Host;
	int32 Port = 0;
	int32 SnapshotSeq = 0;
	int32 ProtocolVersion = 0;
	int32 Epoch = 0;
	FString SceneId;
	FString VfsUrl;
	bool bRequiresToken = false;

	FString MakeKey() const
	{
		return FString::Printf(TEXT("%s|%s|%d|%d|%d"),
			*LayerIdentifier, *Host, Port, Epoch, SnapshotSeq);
	}
};

static bool TryGetLiveDictString(const pxr::VtDictionary& Dict, const char* Key, FString& Out)
{
	const auto It = Dict.find(Key);
	if (It == Dict.end()) return false;
	const pxr::VtValue& Value = It->second;
	if (!Value.IsHolding<std::string>()) return false;
	Out = UTF8_TO_TCHAR(Value.UncheckedGet<std::string>().c_str());
	return true;
}

static bool TryGetLiveDictBool(const pxr::VtDictionary& Dict, const char* Key, bool& Out)
{
	const auto It = Dict.find(Key);
	if (It == Dict.end()) return false;
	const pxr::VtValue& Value = It->second;
	if (!Value.IsHolding<bool>()) return false;
	Out = Value.UncheckedGet<bool>();
	return true;
}

static bool TryGetLiveDictInt(const pxr::VtDictionary& Dict, const char* Key, int32& Out)
{
	const auto It = Dict.find(Key);
	if (It == Dict.end()) return false;
	const pxr::VtValue& Value = It->second;
	if (Value.IsHolding<int>())
	{
		Out = static_cast<int32>(Value.UncheckedGet<int>());
		return true;
	}
	if (Value.IsHolding<int64_t>())
	{
		Out = static_cast<int32>(Value.UncheckedGet<int64_t>());
		return true;
	}
	if (Value.IsHolding<unsigned int>())
	{
		Out = static_cast<int32>(Value.UncheckedGet<unsigned int>());
		return true;
	}
	if (Value.IsHolding<uint64_t>())
	{
		Out = static_cast<int32>(Value.UncheckedGet<uint64_t>());
		return true;
	}
	return false;
}

static bool TryReadLiveOpenMetadata(AUsdStageActor* StageActor, FLiveOpenMetadata& Out)
{
	if (!StageActor || !IsValid(StageActor)) return false;

	pxr::UsdStageRefPtr PxrStage = static_cast<pxr::UsdStageRefPtr>(
		StageActor->GetOrOpenUsdStage());
	if (!PxrStage) return false;

	pxr::SdfLayerHandle RootLayer = PxrStage->GetRootLayer();
	if (!RootLayer) return false;

	pxr::VtDictionary CustomData = RootLayer->GetCustomLayerData();
	const auto MetaIt = CustomData.find("openusdconnect");
	if (MetaIt == CustomData.end()) return false;

	const pxr::VtValue& MetaValue = MetaIt->second;
	if (!MetaValue.IsHolding<pxr::VtDictionary>()) return false;

	const pxr::VtDictionary& Meta = MetaValue.UncheckedGet<pxr::VtDictionary>();
	bool bLive = false;
	if (!TryGetLiveDictBool(Meta, "live", bLive) || !bLive) return false;

	FLiveOpenMetadata Candidate;
	Candidate.LayerIdentifier = UTF8_TO_TCHAR(RootLayer->GetIdentifier().c_str());
	TryGetLiveDictString(Meta, "host", Candidate.Host);
	TryGetLiveDictInt(Meta, "port", Candidate.Port);
	TryGetLiveDictInt(Meta, "snapshot_seq", Candidate.SnapshotSeq);
	TryGetLiveDictInt(Meta, "protocol_version", Candidate.ProtocolVersion);
	TryGetLiveDictInt(Meta, "epoch", Candidate.Epoch);
	TryGetLiveDictString(Meta, "scene_id", Candidate.SceneId);
	TryGetLiveDictString(Meta, "vfs_url", Candidate.VfsUrl);
	TryGetLiveDictBool(Meta, "requires_token", Candidate.bRequiresToken);

	if (Candidate.Host.IsEmpty() || Candidate.Port < 1 || Candidate.Port > 65535)
	{
		UE_LOG(LogUSDConnectSubsystem, Warning,
			TEXT("OpenUSDConnect live metadata is present but has no valid host/port"));
		return false;
	}

	Out = Candidate;
	return true;
}

static bool ReadXformTrs(
	pxr::UsdStageRefPtr& Stage, const FString& PrimPath, FEmitXformTrs& OutTrs,
	bool* bOutFromMatrixOp = nullptr)
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
		else if (OpType == pxr::UsdGeomXformOp::TypeTransform)
		{
			// The engine's component write-back collapses op stacks into a
			// single matrix op (UnrealToUsd::ConvertXformable). Component
			// transforms are shear-free, so decompose back to full TRS.
			pxr::GfMatrix4d Matrix(1.0);
			Op.Get(&Matrix, Time);
			const pxr::GfTransform Decomposed(Matrix);

			const pxr::GfVec3d T = Decomposed.GetTranslation();
			OutTrs.T[0] = (float)T[0]; OutTrs.T[1] = (float)T[1]; OutTrs.T[2] = (float)T[2];
			const pxr::GfQuatd R = Decomposed.GetRotation().GetQuat().GetNormalized();
			OutTrs.R[0] = (float)R.GetReal();
			const pxr::GfVec3d Im = R.GetImaginary();
			OutTrs.R[1] = (float)Im[0]; OutTrs.R[2] = (float)Im[1]; OutTrs.R[3] = (float)Im[2];
			const pxr::GfVec3d S = Decomposed.GetScale();
			OutTrs.S[0] = (float)S[0]; OutTrs.S[1] = (float)S[1]; OutTrs.S[2] = (float)S[2];

			OutTrs.Fields |= 7;
			if (bOutFromMatrixOp)
			{
				*bOutFromMatrixOp = true;
			}
		}
	}
	return OutTrs.Fields != 0;
}

// Rewrite a matrix-collapsed prim back to the canonical translate/orient/scale
// op stack with the decomposed values. The composed transform is identical, so
// the stage actor's component sync sees no change — but per-op attribute edits
// (USD Stage panel) work again and the wire keeps TRS semantics.
static void RestoreCanonicalXformOps(
	pxr::UsdStageRefPtr& Stage, const FString& PrimPath, const FEmitXformTrs& Trs)
{
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(pxr::SdfPath(TCHAR_TO_UTF8(*PrimPath)));
	if (!Prim)
	{
		return;
	}

	// Only reshape prims that carried canonical ops before the engine
	// collapsed them — foreign matrix-native prims keep their authored shape.
	const pxr::UsdAttribute Translate = Prim.GetAttribute(pxr::TfToken("xformOp:translate"));
	const pxr::UsdAttribute Orient    = Prim.GetAttribute(pxr::TfToken("xformOp:orient"));
	if ((!Translate || !Translate.HasAuthoredValue()) && (!Orient || !Orient.HasAuthoredValue()))
	{
		return;
	}

	pxr::SdfChangeBlock ChangeBlock;

	Prim.CreateAttribute(
			pxr::TfToken("xformOp:translate"), pxr::SdfValueTypeNames->Double3, false)
		.Set(pxr::GfVec3d(Trs.T[0], Trs.T[1], Trs.T[2]));
	Prim.CreateAttribute(
			pxr::TfToken("xformOp:orient"), pxr::SdfValueTypeNames->Quatf, false)
		.Set(pxr::GfQuatf(Trs.R[0], pxr::GfVec3f(Trs.R[1], Trs.R[2], Trs.R[3])));
	Prim.CreateAttribute(
			pxr::TfToken("xformOp:scale"), pxr::SdfValueTypeNames->Float3, false)
		.Set(pxr::GfVec3f(Trs.S[0], Trs.S[1], Trs.S[2]));

	pxr::VtTokenArray Order{
		pxr::TfToken("xformOp:translate"),
		pxr::TfToken("xformOp:orient"),
		pxr::TfToken("xformOp:scale"),
	};
	pxr::UsdGeomXformable(Prim).CreateXformOpOrderAttr().Set(Order);
}

// Map a shader-input VtValue onto the typed wire slots. Values arrive as their
// declared USD type (read from the stage, not JSON), so no cross-slot routing
// is needed — unhandled types (arrays, matrices) are skipped by returning false.
static bool ConvertVtValueToWire(const pxr::VtValue& Value, FEmitConnectableValue& Out)
{
	using OpenUSDConnect::ConnectableInputValueType;

	if (Value.IsHolding<float>())
	{
		Out.ValueType   = ConnectableInputValueType::ScalarFloat;
		Out.ScalarFloat = Value.UncheckedGet<float>();
	}
	else if (Value.IsHolding<double>())
	{
		Out.ValueType   = ConnectableInputValueType::ScalarFloat;
		Out.ScalarFloat = static_cast<float>(Value.UncheckedGet<double>());
	}
	else if (Value.IsHolding<int>())
	{
		Out.ValueType = ConnectableInputValueType::ScalarInt;
		Out.ScalarInt = Value.UncheckedGet<int>();
	}
	else if (Value.IsHolding<bool>())
	{
		Out.ValueType   = ConnectableInputValueType::ScalarBool;
		Out.bScalarBool = Value.UncheckedGet<bool>();
	}
	else if (Value.IsHolding<pxr::TfToken>())
	{
		Out.ValueType    = ConnectableInputValueType::ScalarString;
		Out.ScalarString = UTF8_TO_TCHAR(Value.UncheckedGet<pxr::TfToken>().GetText());
	}
	else if (Value.IsHolding<std::string>())
	{
		Out.ValueType    = ConnectableInputValueType::ScalarString;
		Out.ScalarString = UTF8_TO_TCHAR(Value.UncheckedGet<std::string>().c_str());
	}
	else if (Value.IsHolding<pxr::SdfAssetPath>())
	{
		Out.ValueType    = ConnectableInputValueType::ScalarString;
		Out.ScalarString = UTF8_TO_TCHAR(Value.UncheckedGet<pxr::SdfAssetPath>().GetAssetPath().c_str());
	}
	else if (Value.IsHolding<pxr::GfVec2f>())
	{
		const pxr::GfVec2f V = Value.UncheckedGet<pxr::GfVec2f>();
		Out.ValueType = ConnectableInputValueType::FloatArray;
		Out.Floats    = {V[0], V[1]};
	}
	else if (Value.IsHolding<pxr::GfVec3f>())
	{
		const pxr::GfVec3f V = Value.UncheckedGet<pxr::GfVec3f>();
		Out.ValueType = ConnectableInputValueType::FloatArray;
		Out.Floats    = {V[0], V[1], V[2]};
	}
	else if (Value.IsHolding<pxr::GfVec4f>())
	{
		const pxr::GfVec4f V = Value.UncheckedGet<pxr::GfVec4f>();
		Out.ValueType = ConnectableInputValueType::FloatArray;
		Out.Floats    = {V[0], V[1], V[2], V[3]};
	}
	else
	{
		return false;
	}
	return true;
}

// Read the authored values of specific "inputs:*" attributes on a UsdShade
// connectable prim (Shader, Material, NodeGraph, or light container).
static bool ReadConnectableInputs(
	pxr::UsdStageRefPtr& Stage,
	const FString& PrimPath,
	const TSet<FString>& InputAttrNames,
	FEmitConnectableInput& Out)
{
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(pxr::SdfPath(TCHAR_TO_UTF8(*PrimPath)));
	if (!Prim || !pxr::UsdShadeConnectableAPI(Prim))
	{
		return false;
	}

	Out.PrimPath = PrimPath;
	if (pxr::UsdShadeShader Shader{Prim})
	{
		pxr::TfToken IdToken;
		Shader.GetIdAttr().Get(&IdToken);
		Out.InfoId = UTF8_TO_TCHAR(IdToken.GetText());
	}

	for (const FString& AttrName : InputAttrNames)
	{
		pxr::UsdAttribute Attr = Prim.GetAttribute(pxr::TfToken(TCHAR_TO_UTF8(*AttrName)));
		pxr::VtValue Value;
		if (!Attr || !Attr.Get(&Value, pxr::UsdTimeCode::Default()))
		{
			continue;
		}

		FEmitConnectableValue Wire;
		Wire.Name     = AttrName.RightChop(7);	  // strip "inputs:" — wire names are bare
		Wire.TypeName = UTF8_TO_TCHAR(Attr.GetTypeName().GetAsToken().GetText());
		if (!ConvertVtValueToWire(Value, Wire))
		{
			UE_LOG(LogUSDConnectSubsystem, Verbose,
				TEXT("ReadConnectableInputs(%s): skipping %s — unhandled value type %s"),
				*PrimPath, *AttrName, UTF8_TO_TCHAR(Value.GetTypeName().c_str()));
			continue;
		}
		Out.Inputs.Add(MoveTemp(Wire));
	}
	return Out.Inputs.Num() > 0;
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
	ConnectResolved(false);
}

void UUSDConnectSubsystem::StopClients()
{
	if (SyncClient) { SyncClient->StopAndWait(); SyncClient.Reset(); }
	if (EmitClient) { EmitClient->StopAndWait(); EmitClient.Reset(); }

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

#if USE_USD_SDK
	if (Settings->bUseLiveMetadataFromStage)
	{
		if (AUsdStageActor* StageActor = CachedStageActor.Get())
		{
			FLiveOpenMetadata Metadata;
			if (TryReadLiveOpenMetadata(StageActor, Metadata))
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
#endif

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
			Settings->Department, ClientId, SessionOrigin,
			Settings->ReconnectDelaySecs, ReceiverInitialLastSeq, TargetToken);
		if (SyncClient->Start()) { bActiveReceiverStarted = true; }
		else { SyncClient.Reset(); }
	}

	if (bStartEmitter && !bDelayEmitterForToken)
	{
		EmitClient = MakeShared<FEmitClient>(
			this, TargetHost, TargetPort,
			Settings->Department, ClientId, SessionOrigin,
			Settings->ReconnectDelaySecs, TargetToken);
		if (EmitClient->Start()) { bActiveEmitterStarted = true; }
		else { EmitClient.Reset(); }
	}
}

void UUSDConnectSubsystem::TryStartDeferredEmitter()
{
	if (!bDeferredEmitterForToken || EmitClient || ActiveServerHost.IsEmpty() || ActiveServerPort <= 0)
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
		this, ActiveServerHost, ActiveServerPort,
		Settings->Department, ClientId, SessionOrigin,
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

void UUSDConnectSubsystem::RefreshLiveMetadataFromStage(AUsdStageActor* Actor)
{
	const UUSDConnectSettings* Settings = GetDefault<UUSDConnectSettings>();
	if (!Settings || !Settings->bUseLiveMetadataFromStage) return;

#if USE_USD_SDK
	FLiveOpenMetadata Metadata;
	if (!TryReadLiveOpenMetadata(Actor, Metadata)) return;

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
#else
	(void)Actor;
#endif
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
	Status.bEmitterStarted = bActiveEmitterStarted;
	Status.bEmitterConnected = EmitClient && EmitClient->IsConnected();
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

	if (!EventQueue.IsEmpty())
	{
		DrainAndApply();
	}

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
		// Contiguous value events share one SdfChangeBlock so their notices
		// fire as a single batch — the stage actor's per-notice component
		// resync dominates replay throughput. Structural events cannot apply
		// inside a block (UsdStage::DefinePrim and composition-arc edits need
		// the stage to recompose immediately), so the run's block closes
		// before one applies and a fresh block opens for the next value run.
		TUniquePtr<pxr::SdfChangeBlock> RunBlock;
#endif
		for (TArray<uint8>& Frame : Chunk)
		{
#if USE_USD_SDK
			if (FUSDEventApplier::FrameUsesChangeBlock(Frame))
			{
				if (!RunBlock)
				{
					RunBlock = MakeUnique<pxr::SdfChangeBlock>();
				}
			}
			else
			{
				RunBlock.Reset();
			}
#endif
			FString TouchedPrim;
			OpenUSDConnect::EventPayload EventKind = OpenUSDConnect::EventPayload::NONE;
			FUSDEventApplier::ApplyFrame(Frame, StageActor, &TouchedPrim, &EventKind);
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
#if USE_USD_SDK
	pxr::UsdStageRefPtr PxrStage = static_cast<pxr::UsdStageRefPtr>(
		StageActor->GetOrOpenUsdStage());
	if (!PxrStage) return;

	// Try to read and emit TRS
	{
		FEmitXformTrs Xform;
		bool bFromMatrixOp = false;
		if (ReadXformTrs(PxrStage, PrimPath, Xform, &bFromMatrixOp))
		{
			if (bFromMatrixOp)
			{
				// Suppress our own listener: the restore fires notices, but
				// it re-authors the exact values being emitted below.
				bSuppressEmit.store(true);
				RestoreCanonicalXformOps(PxrStage, PrimPath, Xform);
				bSuppressEmit.store(false);
			}

			TArray<FEmitXformTrs> Batch = { Xform };
			TArray<uint8> Frame = BuildXformTxnFrame(ClientId, Batch);
			UE_LOG(LogUSDConnectSubsystem, Verbose,
				TEXT("EmitPrimChange(%s): TRS frame built (%d bytes, fields=0x%02x%s) — enqueueing"),
				*PrimPath, Frame.Num(), Xform.Fields,
				bFromMatrixOp ? TEXT(", decomposed from matrix op") : TEXT(""));
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

void UUSDConnectSubsystem::EmitConnectableInputs(
	AUsdStageActor* StageActor, const FString& PrimPath, const TSet<FString>& InputAttrNames)
{
#if USE_USD_SDK
	pxr::UsdStageRefPtr PxrStage = static_cast<pxr::UsdStageRefPtr>(
		StageActor->GetOrOpenUsdStage());
	if (!PxrStage) return;

	FEmitConnectableInput Event;
	if (!ReadConnectableInputs(PxrStage, PrimPath, InputAttrNames, Event))
	{
		return;
	}

	TArray<FEmitConnectableInput> Batch = { MoveTemp(Event) };
	TArray<uint8> Frame = BuildConnectableInputTxnFrame(ClientId, Batch);
	UE_LOG(LogUSDConnectSubsystem, Verbose,
		TEXT("EmitConnectableInputs(%s): %d input(s), frame %d bytes — enqueueing"),
		*PrimPath, Batch[0].Inputs.Num(), Frame.Num());
	if (Frame.Num() > 0)
	{
		EmitClient->EnqueueFrame(MoveTemp(Frame));
	}
#endif
}

// ---------------------------------------------------------------------------
// Materialization (local .mtlx documents for MaterialX rendering)
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::QueueInitialMaterializations(AUsdStageActor* Actor)
{
#if USE_USD_SDK
	if (!Actor || !IsValid(Actor))
	{
		return;
	}

	pxr::UsdStageRefPtr Stage = static_cast<pxr::UsdStageRefPtr>(Actor->GetOrOpenUsdStage());
	if (!Stage)
	{
		return;
	}

	pxr::SdfLayerHandle RootLayer = Stage->GetRootLayer();
	if (!RootLayer)
	{
		return;
	}

	const FString RootIdentifier = UTF8_TO_TCHAR(RootLayer->GetIdentifier().c_str());
	if (RootIdentifier.IsEmpty() || RootIdentifier == LastMaterializedRootLayerIdentifier)
	{
		return;
	}

	TArray<FString> Materials;
	for (const pxr::UsdPrim& Prim : Stage->Traverse())
	{
		const pxr::UsdShadeMaterial Material(Prim);
		if (Material && Material.GetSurfaceOutput(pxr::TfToken("mtlx")))
		{
			Materials.Add(UTF8_TO_TCHAR(Prim.GetPath().GetText()));
		}
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
#endif
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
