// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "USDEventApplier.h"
#include "USDConnectProtocol.h"

#include "Logging/LogMacros.h"
#include "Containers/Array.h"

#include "USDStageActor.h"
#include "UsdWrappers/UsdStage.h"
#include "UsdWrappers/SdfPath.h"

#if USE_USD_SDK
#include "USDIncludesStart.h"

#include "pxr/usd/usd/stage.h"
#include "pxr/usd/usd/prim.h"
#include "pxr/usd/usd/timeCode.h"
#include "pxr/usd/usd/references.h"
#include "pxr/usd/usd/payloads.h"
#include "pxr/usd/usd/variantSets.h"
#include "pxr/usd/usdGeom/xformable.h"
#include "pxr/usd/usdGeom/xformOp.h"
#include "pxr/usd/usdGeom/imageable.h"
#include "pxr/usd/usdGeom/tokens.h"
#include "pxr/usd/usdShade/material.h"
#include "pxr/usd/usdShade/materialBindingAPI.h"
#include "pxr/usd/sdf/changeBlock.h"
#include "pxr/usd/sdf/reference.h"
#include "pxr/usd/sdf/payload.h"
#include "pxr/base/gf/vec3d.h"
#include "pxr/base/gf/vec3f.h"
#include "pxr/base/gf/quatf.h"

#include "USDIncludesEnd.h"
#endif // USE_USD_SDK

DEFINE_LOG_CATEGORY_STATIC(LogUSDEventApplier, Log, All);

using namespace OUC;

// ---------------------------------------------------------------------------
// pxr USD apply handlers — all guarded by USE_USD_SDK
// ---------------------------------------------------------------------------
#if USE_USD_SDK

namespace
{

pxr::SdfPath ToSdfPath(const FString& S)
{
	return pxr::SdfPath(TCHAR_TO_UTF8(*S));
}

pxr::TfToken ToToken(const FString& S)
{
	return pxr::TfToken(TCHAR_TO_UTF8(*S));
}

// ---- EnsurePrim -----------------------------------------------------------
void ApplyEnsurePrim(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::EnsurePrim_Prim);
	const FString TypeName = FB::GetStr(Ev, VT::EnsurePrim_TypeName);
	if (PrimPath.IsEmpty()) return;

	pxr::UsdPrim Prim = Stage->DefinePrim(
		ToSdfPath(PrimPath),
		TypeName.IsEmpty() ? pxr::TfToken() : ToToken(TypeName));

	// Apply API schemas listed in the event
	const uint32 SchemaCount = FB::GetVecSize(Ev, VT::EnsurePrim_ApiSchemas);
	for (uint32 i = 0; i < SchemaCount; ++i)
	{
		const FString Schema = FB::GetStrVecElem(Ev, VT::EnsurePrim_ApiSchemas, i);
		if (!Schema.IsEmpty())
		{
			Prim.ApplyAPI(ToToken(Schema));
		}
	}
}

// ---- EnsureXformOps -------------------------------------------------------
void ApplyEnsureXformOps(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::EnsureXformOps_Prim);
	if (PrimPath.IsEmpty()) return;

	const pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	pxr::UsdGeomXformable Xformable(Prim);
	if (!Xformable) return;

	bool bReset = false;
	if (!Xformable.GetOrderedXformOps(&bReset).empty()) return;  // already set up

	Xformable.AddTranslateOp();
	Xformable.AddOrientOp();
	Xformable.AddScaleOp();
}

// ---- SetXformTrs ----------------------------------------------------------
void ApplySetXformTrs(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::SetXformTrs_Prim);
	if (PrimPath.IsEmpty()) return;

	const pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	pxr::UsdGeomXformable Xformable(Prim);
	if (!Xformable) return;

	bool bReset = false;
	std::vector<pxr::UsdGeomXformOp> Ops = Xformable.GetOrderedXformOps(&bReset);

	pxr::UsdGeomXformOp TranslateOp, RotateOp, ScaleOp;
	for (auto& Op : Ops)
	{
		switch (Op.GetOpType())
		{
		case pxr::UsdGeomXformOp::TypeTranslate: TranslateOp = Op; break;
		case pxr::UsdGeomXformOp::TypeOrient:    RotateOp    = Op; break;
		case pxr::UsdGeomXformOp::TypeScale:     ScaleOp     = Op; break;
		default: break;
		}
	}

	const pxr::UsdTimeCode Time = FB::HasField(Ev, VT::SetXformTrs_Time)
		? pxr::UsdTimeCode(FB::GetField<double>(Ev, VT::SetXformTrs_Time, 0.0))
		: pxr::UsdTimeCode::Default();

	const uint8 Fields = FB::GetField<uint8>(Ev, VT::SetXformTrs_Fields, 0);

	if (Fields & 1)  // Translate
	{
		const TArray<float> T = FB::GetFloatVec(Ev, VT::SetXformTrs_T);
		if (T.Num() >= 3)
		{
			if (!TranslateOp) TranslateOp = Xformable.AddTranslateOp();
			TranslateOp.Set(pxr::GfVec3d(T[0], T[1], T[2]), Time);
		}
	}
	if (Fields & 2)  // Rotate (quaternion [w,x,y,z])
	{
		const TArray<float> R = FB::GetFloatVec(Ev, VT::SetXformTrs_R);
		if (R.Num() >= 4)
		{
			if (!RotateOp) RotateOp = Xformable.AddOrientOp();
			RotateOp.Set(pxr::GfQuatf(R[0], pxr::GfVec3f(R[1], R[2], R[3])), Time);
		}
	}
	if (Fields & 4)  // Scale
	{
		const TArray<float> S = FB::GetFloatVec(Ev, VT::SetXformTrs_S);
		if (S.Num() >= 3)
		{
			if (!ScaleOp) ScaleOp = Xformable.AddScaleOp();
			ScaleOp.Set(pxr::GfVec3f(S[0], S[1], S[2]), Time);
		}
	}
}

// ---- DeletePrim -----------------------------------------------------------
void ApplyDeletePrim(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::DeletePrim_Prim);
	if (!PrimPath.IsEmpty())
	{
		Stage->RemovePrim(ToSdfPath(PrimPath));
	}
}

// ---- DeactivatePrim -------------------------------------------------------
void ApplyDeactivatePrim(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::DeactivatePrim_Prim);
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;
	const bool bActive = FB::GetField<uint8>(Ev, VT::DeactivatePrim_Active, 1) != 0;
	Prim.SetActive(bActive);
}

// ---- RenamePrim (stub) ----------------------------------------------------
void ApplyRenamePrim(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::RenamePrim_Prim);
	const FString NewName  = FB::GetStr(Ev, VT::RenamePrim_NewName);
	UE_LOG(LogUSDEventApplier, Warning,
		TEXT("RenamePrim is not implemented (prim=%s newName=%s) — needs SdfCopySpec"),
		*PrimPath, *NewName);
}

// ---- SetVisibility --------------------------------------------------------
void ApplySetVisibility(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::SetVisibility_Prim);
	if (PrimPath.IsEmpty()) return;

	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	pxr::UsdGeomImageable Imageable(Prim);
	if (!Imageable) return;

	const pxr::UsdTimeCode Time = FB::HasField(Ev, VT::SetVisibility_Time)
		? pxr::UsdTimeCode(FB::GetField<double>(Ev, VT::SetVisibility_Time, 0.0))
		: pxr::UsdTimeCode::Default();

	if (FB::GetField<uint8>(Ev, VT::SetVisibility_Visible, 1))
	{
		Imageable.MakeVisible(Time);
	}
	else
	{
		Imageable.MakeInvisible(Time);
	}
}

// ---- SetStageMetadata -----------------------------------------------------
void ApplySetStageMetadata(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	if (FB::HasField(Ev, VT::SetStageMetadata_TimeCodesPerSecond))
	{
		Stage->SetTimeCodesPerSecond(FB::GetField<double>(Ev, VT::SetStageMetadata_TimeCodesPerSecond, 24.0));
	}
	if (FB::HasField(Ev, VT::SetStageMetadata_FramesPerSecond))
	{
		Stage->SetFramesPerSecond(FB::GetField<double>(Ev, VT::SetStageMetadata_FramesPerSecond, 24.0));
	}
	if (FB::HasField(Ev, VT::SetStageMetadata_StartTimeCode))
	{
		Stage->SetStartTimeCode(FB::GetField<double>(Ev, VT::SetStageMetadata_StartTimeCode, 1.0));
	}
	if (FB::HasField(Ev, VT::SetStageMetadata_EndTimeCode))
	{
		Stage->SetEndTimeCode(FB::GetField<double>(Ev, VT::SetStageMetadata_EndTimeCode, 1.0));
	}
	if (FB::HasField(Ev, VT::SetStageMetadata_MetersPerUnit))
	{
		const double Mpu = FB::GetField<double>(Ev, VT::SetStageMetadata_MetersPerUnit, 0.01);
		Stage->SetMetadata(pxr::TfToken("metersPerUnit"), Mpu);
	}
	// upAxis: USDImporter reads this separately on stage open — skip for now
}

// ---- SetReference ---------------------------------------------------------
void ApplySetReference(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::SetReference_Prim);
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	const uint32 Count = FB::GetVecSize(Ev, VT::SetReference_Refs);
	pxr::UsdReferences Refs = Prim.GetReferences();
	Refs.ClearReferences();

	for (uint32 i = 0; i < Count; ++i)
	{
		const uint8* Arc = FB::GetVecElem(Ev, VT::SetReference_Refs, i);
		if (!Arc) continue;
		const FString AssetPath   = FB::GetStr(Arc, VT::ArcEntry_AssetPath);
		const FString ArcPrimPath = FB::GetStr(Arc, VT::ArcEntry_PrimPath);
		Refs.AddReference(pxr::SdfReference(
			TCHAR_TO_UTF8(*AssetPath),
			ArcPrimPath.IsEmpty() ? pxr::SdfPath() : ToSdfPath(ArcPrimPath)));
	}
}

// ---- SetPayload -----------------------------------------------------------
void ApplySetPayload(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::SetPayload_Prim);
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	const uint32 Count = FB::GetVecSize(Ev, VT::SetPayload_Payloads);
	pxr::UsdPayloads Payloads = Prim.GetPayloads();
	Payloads.ClearPayloads();

	for (uint32 i = 0; i < Count; ++i)
	{
		const uint8* Arc = FB::GetVecElem(Ev, VT::SetPayload_Payloads, i);
		if (!Arc) continue;
		const FString AssetPath   = FB::GetStr(Arc, VT::ArcEntry_AssetPath);
		const FString ArcPrimPath = FB::GetStr(Arc, VT::ArcEntry_PrimPath);
		Payloads.AddPayload(pxr::SdfPayload(
			TCHAR_TO_UTF8(*AssetPath),
			ArcPrimPath.IsEmpty() ? pxr::SdfPath() : ToSdfPath(ArcPrimPath)));
	}
}

// ---- LoadPayload / UnloadPayload ------------------------------------------
void ApplyLoadPayload(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::LoadPayload_Prim);
	if (!PrimPath.IsEmpty()) Stage->Load(ToSdfPath(PrimPath));
}

void ApplyUnloadPayload(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::UnloadPayload_Prim);
	if (!PrimPath.IsEmpty()) Stage->Unload(ToSdfPath(PrimPath));
}

// ---- SetVariantSelections -------------------------------------------------
void ApplySetVariantSelections(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::SetVariantSelections_Prim);
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	pxr::UsdVariantSets VarSets = Prim.GetVariantSets();
	const uint32 Count = FB::GetVecSize(Ev, VT::SetVariantSelections_Selections);
	for (uint32 i = 0; i < Count; ++i)
	{
		const uint8* Pair = FB::GetVecElem(Ev, VT::SetVariantSelections_Selections, i);
		if (!Pair) continue;
		const FString Key   = FB::GetStr(Pair, VT::StringPair_Key);
		const FString Value = FB::GetStr(Pair, VT::StringPair_Value);
		if (Key.IsEmpty()) continue;
		pxr::UsdVariantSet VSet = VarSets.GetVariantSet(TCHAR_TO_UTF8(*Key));
		VSet.SetVariantSelection(TCHAR_TO_UTF8(*Value));
	}
}

// ---- SetMaterialBinding ---------------------------------------------------
void ApplySetMaterialBinding(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath     = FB::GetStr(Ev, VT::SetMaterialBinding_Prim);
	const FString MaterialPath = FB::GetStr(Ev, VT::SetMaterialBinding_MaterialPath);
	if (PrimPath.IsEmpty() || MaterialPath.IsEmpty()) return;

	pxr::UsdPrim Prim    = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	pxr::UsdPrim MatPrim = Stage->GetPrimAtPath(ToSdfPath(MaterialPath));
	if (!Prim || !MatPrim) return;

	pxr::UsdShadeMaterial Mat(MatPrim);
	pxr::UsdShadeMaterialBindingAPI BindingAPI =
		pxr::UsdShadeMaterialBindingAPI::Apply(Prim);
	BindingAPI.Bind(Mat);
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------
void DispatchEvent(pxr::UsdStageRefPtr& Stage, uint8 EventKind, const uint8* Ev)
{
	if (!Ev) return;
	switch (EventKind)
	{
	case kEvEnsurePrim:           ApplyEnsurePrim(Stage, Ev);           break;
	case kEvEnsureXformOps:       ApplyEnsureXformOps(Stage, Ev);       break;
	case kEvSetXformTrs:          ApplySetXformTrs(Stage, Ev);          break;
	case kEvDeletePrim:           ApplyDeletePrim(Stage, Ev);           break;
	case kEvDeactivatePrim:       ApplyDeactivatePrim(Stage, Ev);       break;
	case kEvRenamePrim:           ApplyRenamePrim(Stage, Ev);           break;
	case kEvSetVisibility:        ApplySetVisibility(Stage, Ev);        break;
	case kEvSetGprimAttrs:
		UE_LOG(LogUSDEventApplier, Verbose,
			TEXT("SetGprimAttrs: skipped (not implemented in MVP)"));
		break;
	case kEvSetReference:         ApplySetReference(Stage, Ev);         break;
	case kEvSetPayload:           ApplySetPayload(Stage, Ev);           break;
	case kEvLoadPayload:          ApplyLoadPayload(Stage, Ev);          break;
	case kEvUnloadPayload:        ApplyUnloadPayload(Stage, Ev);        break;
	case kEvSetVariantSelections: ApplySetVariantSelections(Stage, Ev); break;
	case kEvSetMaterialBinding:   ApplySetMaterialBinding(Stage, Ev);   break;
	case kEvSetConnectableInput:
	case kEvSetConnectableConnection:
		UE_LOG(LogUSDEventApplier, Verbose,
			TEXT("SetConnectable*: skipped (not implemented in MVP)"));
		break;
	case kEvSetStageMetadata:     ApplySetStageMetadata(Stage, Ev);     break;
	default:
		UE_LOG(LogUSDEventApplier, Warning, TEXT("Unknown event kind: %u"), EventKind);
		break;
	}
}

} // namespace
#endif // USE_USD_SDK

// ---------------------------------------------------------------------------
// FUSDEventApplier::ApplyFrame
// ---------------------------------------------------------------------------
void FUSDEventApplier::ApplyFrame(const TArray<uint8>& RawFrame, AUsdStageActor* StageActor)
{
	if (!StageActor || RawFrame.Num() < 8) return;

#if USE_USD_SDK
	pxr::UsdStageRefPtr PxrStage =
		static_cast<pxr::UsdStageRefPtr>(StageActor->GetOrOpenUsdStage());
	if (!PxrStage)
	{
		UE_LOG(LogUSDEventApplier, Warning,
			TEXT("No USD stage open on AUsdStageActor — skipping event"));
		return;
	}

	const uint8* Env = FB::GetRoot(RawFrame);
	if (!Env) return;

	const uint8* BcEvent = FB::GetPtr(Env, VT::Envelope_Payload);
	if (!BcEvent) return;

	const uint8* EventWrapper = FB::GetPtr(BcEvent, VT::BroadcastEvent_Event);
	if (!EventWrapper) return;

	const uint8  EventKind  = FB::GetField<uint8>(EventWrapper, VT::EventWrapper_EventType, 0);
	const uint8* EventTable = FB::GetPtr(EventWrapper, VT::EventWrapper_Event);

	// SdfChangeBlock batches all SDF mutations done within its scope into a single
	// consolidated ObjectsChanged notice dispatched on destruction.  The stage
	// actor's FUsdListener receives that notice and refreshes the Unreal scene once.
	{
		pxr::SdfChangeBlock ChangeBlock;
		DispatchEvent(PxrStage, EventKind, EventTable);
	}
#else
	UE_LOG(LogUSDEventApplier, Warning,
		TEXT("USD SDK not available — cannot apply USD events"));
#endif
}
