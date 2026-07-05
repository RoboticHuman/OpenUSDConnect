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
#include "pxr/usd/usd/namespaceEditor.h"
#include "pxr/usd/usd/relationship.h"
#include "pxr/usd/usdGeom/xformable.h"
#include "pxr/usd/usdGeom/xformOp.h"
#include "pxr/usd/usdGeom/imageable.h"
#include "pxr/usd/usdGeom/primvarsAPI.h"
#include "pxr/usd/usdGeom/pointInstancer.h"
#include "pxr/usd/usdGeom/tokens.h"
#include "pxr/usd/usdShade/material.h"
#include "pxr/usd/usdShade/materialBindingAPI.h"
#include "pxr/usd/usdShade/shader.h"
#include "pxr/usd/usdShade/connectableAPI.h"
#include "pxr/usd/usdShade/input.h"
#include "pxr/usd/usdShade/output.h"
#include "pxr/usd/sdf/changeBlock.h"
#include "pxr/usd/sdf/reference.h"
#include "pxr/usd/sdf/payload.h"
#include "pxr/usd/sdf/schema.h"
#include "pxr/usd/sdf/assetPath.h"
#include "pxr/usd/sdf/valueTypeName.h"
#include "pxr/usd/sdf/listOp.h"
#include "pxr/base/gf/vec2d.h"
#include "pxr/base/gf/vec2f.h"
#include "pxr/base/gf/vec3d.h"
#include "pxr/base/gf/vec3f.h"
#include "pxr/base/gf/vec4d.h"
#include "pxr/base/gf/vec4f.h"
#include "pxr/base/gf/matrix4d.h"
#include "pxr/base/gf/quatf.h"
#include "pxr/base/vt/types.h"
#include "pxr/base/vt/value.h"

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

// ---- RenamePrim -------------------------------------------------------------
void ApplyRenamePrim(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::RenamePrim_Prim);
	const FString NewName  = FB::GetStr(Ev, VT::RenamePrim_NewName);
	if (PrimPath.IsEmpty() || NewName.IsEmpty()) return;

	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	pxr::UsdNamespaceEditor Editor(Stage);
	if (!Editor.RenamePrim(Prim, ToToken(NewName)) || !Editor.ApplyEdits())
	{
		UE_LOG(LogUSDEventApplier, Warning,
			TEXT("RenamePrim: could not rename %s to %s"), *PrimPath, *NewName);
	}
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
	const FString Purpose      = FB::GetStr(Ev, VT::SetMaterialBinding_MaterialPurpose);
	if (PrimPath.IsEmpty()) return;

	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	pxr::UsdShadeMaterialBindingAPI::Apply(Prim);

	// Direct relationship authoring mirrors the core applier: bindings may
	// target materials that do not exist yet (USD relationships tolerate
	// dangling targets), the purpose suffix picks the slot consumers select
	// via ComputeBoundMaterial(purpose), and an empty material path unbinds.
	std::string RelName = "material:binding";
	if (!Purpose.IsEmpty())
	{
		RelName += ":";
		RelName += TCHAR_TO_UTF8(*Purpose);
	}
	pxr::UsdRelationship Rel = Prim.GetRelationship(pxr::TfToken(RelName));
	if (!Rel)
	{
		Rel = Prim.CreateRelationship(pxr::TfToken(RelName));
	}
	Rel.ClearTargets(false);
	if (!MaterialPath.IsEmpty())
	{
		Rel.AddTarget(ToSdfPath(MaterialPath));
	}
}

// ---------------------------------------------------------------------------
// Typed wire-value decoding (shared by the connectable / gprim / instancer
// appliers)
// ---------------------------------------------------------------------------

bool TypeIs(const std::string& Type, std::initializer_list<const char*> Names)
{
	for (const char* Name : Names)
	{
		if (Type == Name) { return true; }
	}
	return false;
}

// Copy a wire vector straight into a VtArray: one allocation, one memcpy, no
// intermediate buffer — the wire rows are little-endian and layout-identical
// to GfVec2f/GfVec3f/float/int32/int64 elements. WireScalar sizes the length
// prefix (a [float] field carrying Vec3f rows counts floats, not rows).
template <typename VtArrayType, typename WireScalar = float>
VtArrayType VtArrayFromWire(const uint8* Table, uint16 VtOff)
{
	using ElemType = typename VtArrayType::value_type;
	const uint8* Vec = FB::GetPtr(Table, VtOff);
	if (!Vec) { return {}; }
	const size_t Bytes = static_cast<size_t>(FB::ReadLE<uint32>(Vec)) * sizeof(WireScalar);
	VtArrayType Out(Bytes / sizeof(ElemType));
	FMemory::Memcpy(Out.data(), Vec + 4, Out.size() * sizeof(ElemType));
	return Out;
}

// Wire quaternions are [w, x, y, z] rows; GfQuatf stores imaginary first,
// so rows go through the (real, i, j, k) constructor instead of a memcpy.
pxr::VtQuatfArray QuatArrayFromWire(const uint8* Table, uint16 VtOff)
{
	const uint8* Vec = FB::GetPtr(Table, VtOff);
	if (!Vec) { return {}; }
	const uint32 N = FB::ReadLE<uint32>(Vec) / 4;
	const float* F = reinterpret_cast<const float*>(Vec + 4);
	pxr::VtQuatfArray Out(N);
	pxr::GfQuatf* Q = Out.data();
	for (uint32 i = 0; i < N; ++i, F += 4)
	{
		Q[i] = pxr::GfQuatf(F[0], F[1], F[2], F[3]);
	}
	return Out;
}

// Numeric payload slots reflect the emitting language's literal type (a JSON
// "1" packs as int even for a float input); the declared USD type drives the
// coercion, matching the core applier.
pxr::VtValue CoerceNumericScalar(double Number, const std::string& Type)
{
	if (Type == "double") { return pxr::VtValue(Number); }
	if (Type == "int")    { return pxr::VtValue(static_cast<int32>(Number)); }
	if (Type == "bool")   { return pxr::VtValue(Number != 0.0); }
	return pxr::VtValue(static_cast<float>(Number));
}

pxr::VtValue CoerceString(const std::string& S, const std::string& Type)
{
	if (Type == "token") { return pxr::VtValue(pxr::TfToken(S)); }
	if (Type == "asset")
	{
		return pxr::VtValue(S.empty() ? pxr::SdfAssetPath() : pxr::SdfAssetPath(S));
	}
	return pxr::VtValue(S);
}

// Build a float-flavored VtValue from an element buffer. Serves the small
// fixed-size types plus integral payloads that need per-element conversion;
// large float wire arrays take the VtArrayFromWire memcpy path instead.
// Fixed-size types demand exact element counts — a mismatch is a malformed
// encoder and must surface, not truncate.
bool FloatsToValue(const TArray<float>& A, const std::string& Type, pxr::VtValue& Out)
{
	if (TypeIs(Type, {"color3f", "float3", "normal3f", "point3f", "vector3f"}) && A.Num() == 3)
	{
		Out = pxr::GfVec3f(A[0], A[1], A[2]);
	}
	else if (TypeIs(Type, {"color3d", "double3", "normal3d", "point3d", "vector3d"}) && A.Num() == 3)
	{
		Out = pxr::GfVec3d(A[0], A[1], A[2]);
	}
	else if (TypeIs(Type, {"float2", "texCoord2f"}) && A.Num() == 2)
	{
		Out = pxr::GfVec2f(A[0], A[1]);
	}
	else if (Type == "double2" && A.Num() == 2)
	{
		Out = pxr::GfVec2d(A[0], A[1]);
	}
	else if (TypeIs(Type, {"float4", "color4f"}) && A.Num() == 4)
	{
		Out = pxr::GfVec4f(A[0], A[1], A[2], A[3]);
	}
	else if (Type == "double4" && A.Num() == 4)
	{
		Out = pxr::GfVec4d(A[0], A[1], A[2], A[3]);
	}
	else if (Type == "matrix4d" && A.Num() == 16)
	{
		double M[4][4];
		for (int32 i = 0; i < 16; ++i) { M[i / 4][i % 4] = A[i]; }
		Out = pxr::GfMatrix4d(M);
	}
	else if (Type == "float[]")
	{
		pxr::VtFloatArray Arr(A.Num());
		float* D = Arr.data();
		for (int32 i = 0; i < A.Num(); ++i) { D[i] = A[i]; }
		Out = Arr;
	}
	else if (TypeIs(Type, {"float3[]", "vector3f[]", "normal3f[]", "point3f[]", "color3f[]"})
			 && A.Num() % 3 == 0)
	{
		pxr::VtVec3fArray Arr(A.Num() / 3);
		pxr::GfVec3f* D = Arr.data();
		for (int32 i = 0; i < static_cast<int32>(Arr.size()); ++i)
		{
			D[i] = pxr::GfVec3f(A[i * 3], A[i * 3 + 1], A[i * 3 + 2]);
		}
		Out = Arr;
	}
	else if (TypeIs(Type, {"float2[]", "texCoord2f[]"}) && A.Num() % 2 == 0)
	{
		pxr::VtVec2fArray Arr(A.Num() / 2);
		pxr::GfVec2f* D = Arr.data();
		for (int32 i = 0; i < static_cast<int32>(Arr.size()); ++i)
		{
			D[i] = pxr::GfVec2f(A[i * 2], A[i * 2 + 1]);
		}
		Out = Arr;
	}
	else
	{
		return false;
	}
	return true;
}

// Integral payloads destined for float-typed attributes (a JSON [1, 1, 1]
// packs as ints even for a color3f input) convert per element.
TArray<float> FloatsFromIntWire(const uint8* Table, uint16 VtOff)
{
	const uint8* Vec = FB::GetPtr(Table, VtOff);
	if (!Vec) { return {}; }
	const uint32 N = FB::ReadLE<uint32>(Vec);
	const int32* I = reinterpret_cast<const int32*>(Vec + 4);
	TArray<float> Out;
	Out.SetNumUninitialized(static_cast<int32>(N));
	for (uint32 i = 0; i < N; ++i) { Out[i] = static_cast<float>(I[i]); }
	return Out;
}

// ---- OpenPBR -> standard_surface translation --------------------------------
//
// UE's USD MaterialX parser handles standard_surface but not open_pbr_surface,
// so OpenPBR shaders translate at apply time. Faithful port of
// integrations/openpbr_to_standard_surface.py (itself a port of MaterialX's
// official ND_open_pbr_surface_to_standard_surface nodegraph) — that Python
// module is canonical; keep this in lockstep with it.

struct FOpenPBRParams
{
	TMap<FString, float>        Scalars;
	TMap<FString, pxr::GfVec3f> Colors;
	bool bThinWalled = false;
};

FOpenPBRParams OpenPBRDefaults()
{
	FOpenPBRParams P;
	P.Scalars = {
		{ TEXT("base_weight"), 1.0f },
		{ TEXT("base_diffuse_roughness"), 0.0f },
		{ TEXT("base_metalness"), 0.0f },
		{ TEXT("specular_weight"), 1.0f },
		{ TEXT("specular_roughness"), 0.3f },
		{ TEXT("specular_ior"), 1.5f },
		{ TEXT("specular_roughness_anisotropy"), 0.0f },
		{ TEXT("transmission_weight"), 0.0f },
		{ TEXT("transmission_depth"), 0.0f },
		{ TEXT("transmission_scatter_anisotropy"), 0.0f },
		{ TEXT("transmission_dispersion_scale"), 0.0f },
		{ TEXT("subsurface_weight"), 0.0f },
		{ TEXT("subsurface_radius"), 1.0f },
		{ TEXT("subsurface_scatter_anisotropy"), 0.0f },
		{ TEXT("fuzz_weight"), 0.0f },
		{ TEXT("fuzz_roughness"), 0.5f },
		{ TEXT("coat_weight"), 0.0f },
		{ TEXT("coat_roughness"), 0.0f },
		{ TEXT("coat_roughness_anisotropy"), 0.0f },
		{ TEXT("coat_ior"), 1.6f },
		{ TEXT("coat_darkening"), 1.0f },
		{ TEXT("thin_film_weight"), 0.0f },
		{ TEXT("thin_film_thickness"), 0.5f },
		{ TEXT("thin_film_ior"), 1.4f },
		{ TEXT("emission_luminance"), 0.0f },
		{ TEXT("geometry_opacity"), 1.0f },
	};
	P.Colors = {
		{ TEXT("base_color"), pxr::GfVec3f(0.8f) },
		{ TEXT("specular_color"), pxr::GfVec3f(1.0f) },
		{ TEXT("transmission_color"), pxr::GfVec3f(1.0f) },
		{ TEXT("transmission_scatter"), pxr::GfVec3f(0.0f) },
		{ TEXT("subsurface_color"), pxr::GfVec3f(0.8f) },
		{ TEXT("subsurface_radius_scale"), pxr::GfVec3f(1.0f, 0.5f, 0.25f) },
		{ TEXT("fuzz_color"), pxr::GfVec3f(1.0f) },
		{ TEXT("coat_color"), pxr::GfVec3f(1.0f) },
		{ TEXT("emission_color"), pxr::GfVec3f(1.0f) },
	};
	return P;
}

pxr::GfVec3f MixColor(const pxr::GfVec3f& Fg, const pxr::GfVec3f& Bg, float T)
{
	return Bg + (Fg - Bg) * T;
}

// The modulated_base_darkening color the nodegraph multiplies base and
// subsurface color by, modelling how a coat darkens what sits under it.
pxr::GfVec3f CoatBaseDarkening(const FOpenPBRParams& P)
{
	const float CoatIor = P.Scalars.FindRef(TEXT("coat_ior"));
	const float F0Sqrt = (CoatIor - 1.0f) / (CoatIor + 1.0f);
	const float CoatF0 = F0Sqrt * F0Sqrt;
	const float Kcoat = 1.0f - (1.0f - CoatF0) / (CoatIor * CoatIor);

	const pxr::GfVec3f BaseColor = P.Colors.FindRef(TEXT("base_color"));
	const pxr::GfVec3f Emetal = BaseColor * P.Scalars.FindRef(TEXT("specular_weight"));
	const pxr::GfVec3f Edielectric = MixColor(
		P.Colors.FindRef(TEXT("subsurface_color")), BaseColor,
		P.Scalars.FindRef(TEXT("subsurface_weight")));
	const pxr::GfVec3f Ebase = MixColor(
		Emetal, Edielectric, P.Scalars.FindRef(TEXT("base_metalness")));

	pxr::GfVec3f Darkening;
	for (int32 i = 0; i < 3; ++i)
	{
		Darkening[i] = (1.0f - Kcoat) / (1.0f - Ebase[i] * Kcoat);
	}
	const float T = P.Scalars.FindRef(TEXT("coat_weight")) * P.Scalars.FindRef(TEXT("coat_darkening"));
	return MixColor(Darkening, pxr::GfVec3f(1.0f), T);
}

// Translated standard_surface inputs: (name, is-color, value). Scalar entries
// carry X in Value; thin_walled authors separately as bool.
struct FStdSurfaceInput
{
	const TCHAR* Name;
	bool bIsColor;
	pxr::GfVec3f Value;
};

TArray<FStdSurfaceInput> OpenPBRToStandardSurface(const FOpenPBRParams& P)
{
	const pxr::GfVec3f Darkening = CoatBaseDarkening(P);
	auto S = [&P](const TCHAR* N) { return P.Scalars.FindRef(N); };
	auto C = [&P](const TCHAR* N) { return P.Colors.FindRef(N); };
	auto MulC = [](const pxr::GfVec3f& A, const pxr::GfVec3f& B)
	{
		return pxr::GfVec3f(A[0] * B[0], A[1] * B[1], A[2] * B[2]);
	};
	// MaterialX <mix>: bg at t=0, fg at t=1 (same shape as MixColor).
	auto MixF = [](float Fg, float Bg, float T) { return Bg + (Fg - Bg) * T; };

	const float CoatWeight = S(TEXT("coat_weight"));
	const float ThinFilm = S(TEXT("thin_film_weight")) > 0.0f
		? S(TEXT("thin_film_thickness")) * 1000.0f : 0.0f;

	return {
		{ TEXT("base"),                false, pxr::GfVec3f(S(TEXT("base_weight"))) },
		{ TEXT("base_color"),          true,  MulC(C(TEXT("base_color")), Darkening) },
		{ TEXT("diffuse_roughness"),   false, pxr::GfVec3f(S(TEXT("base_diffuse_roughness"))) },
		{ TEXT("metalness"),           false, pxr::GfVec3f(S(TEXT("base_metalness"))) },
		{ TEXT("specular"),            false, pxr::GfVec3f(S(TEXT("specular_weight"))) },
		{ TEXT("specular_color"),      true,  C(TEXT("specular_color")) },
		{ TEXT("specular_roughness"),  false, pxr::GfVec3f(MixF(
			S(TEXT("coat_roughness")), S(TEXT("specular_roughness")), CoatWeight)) },
		{ TEXT("specular_IOR"),        false, pxr::GfVec3f(S(TEXT("specular_ior"))) },
		{ TEXT("specular_anisotropy"), false, pxr::GfVec3f(S(TEXT("specular_roughness_anisotropy"))) },
		{ TEXT("transmission"),        false, pxr::GfVec3f(S(TEXT("transmission_weight"))) },
		{ TEXT("transmission_color"),  true,  C(TEXT("transmission_color")) },
		{ TEXT("transmission_depth"),  false, pxr::GfVec3f(S(TEXT("transmission_depth"))) },
		{ TEXT("transmission_scatter"), true, C(TEXT("transmission_scatter")) },
		{ TEXT("transmission_scatter_anisotropy"), false, pxr::GfVec3f(S(TEXT("transmission_scatter_anisotropy"))) },
		{ TEXT("transmission_dispersion"), false, pxr::GfVec3f(S(TEXT("transmission_dispersion_scale"))) },
		{ TEXT("subsurface"),          false, pxr::GfVec3f(S(TEXT("subsurface_weight"))) },
		{ TEXT("subsurface_color"),    true,  MulC(C(TEXT("subsurface_color")), Darkening) },
		{ TEXT("subsurface_radius"),   true,  C(TEXT("subsurface_radius_scale")) },
		{ TEXT("subsurface_scale"),    false, pxr::GfVec3f(S(TEXT("subsurface_radius"))) },
		{ TEXT("subsurface_anisotropy"), false, pxr::GfVec3f(S(TEXT("subsurface_scatter_anisotropy"))) },
		{ TEXT("sheen"),               false, pxr::GfVec3f(S(TEXT("fuzz_weight"))) },
		{ TEXT("sheen_color"),         true,  C(TEXT("fuzz_color")) },
		{ TEXT("sheen_roughness"),     false, pxr::GfVec3f(FMath::Pow(S(TEXT("fuzz_roughness")), 2.5f)) },
		{ TEXT("coat"),                false, pxr::GfVec3f(CoatWeight) },
		{ TEXT("coat_color"),          true,  C(TEXT("coat_color")) },
		{ TEXT("coat_roughness"),      false, pxr::GfVec3f(S(TEXT("coat_roughness"))) },
		{ TEXT("coat_anisotropy"),     false, pxr::GfVec3f(S(TEXT("coat_roughness_anisotropy"))) },
		{ TEXT("coat_IOR"),            false, pxr::GfVec3f(S(TEXT("coat_ior"))) },
		{ TEXT("coat_affect_roughness"), false, pxr::GfVec3f(1.0f) },
		{ TEXT("thin_film_thickness"), false, pxr::GfVec3f(ThinFilm) },
		{ TEXT("thin_film_IOR"),       false, pxr::GfVec3f(S(TEXT("thin_film_ior"))) },
		{ TEXT("emission"),            false, pxr::GfVec3f(S(TEXT("emission_luminance"))) },
		{ TEXT("emission_color"),      true,  C(TEXT("emission_color")) },
		{ TEXT("opacity"),             true,  pxr::GfVec3f(S(TEXT("geometry_opacity"))) },
	};
}

// ---- SetConnectableInput / SetConnectableConnection -----------------------

// Build a VtValue for a ConnectableInputValue payload according to its USD
// type name. Mirrors the core applier's value coercion table. Fixed-size
// types demand exact element counts — a mismatch is a malformed encoder and
// must surface, not truncate.
bool DecodeConnectableValue(const uint8* Val, const FString& TypeNameStr, pxr::VtValue& Out)
{
	const std::string Type = TCHAR_TO_UTF8(*TypeNameStr);
	const uint8 Kind = FB::GetField<uint8>(Val, VT::ConnectableInputValue_ValueType, 0);
	switch (Kind)
	{
	case 1: // ScalarFloat
		Out = CoerceNumericScalar(
			FB::GetField<float>(Val, VT::ConnectableInputValue_ScalarFloat, 0.0f), Type);
		return true;
	case 2: // ScalarInt
		Out = CoerceNumericScalar(
			FB::GetField<int32>(Val, VT::ConnectableInputValue_ScalarInt, 0), Type);
		return true;
	case 3: // ScalarBool
		Out = CoerceNumericScalar(
			FB::GetField<uint8>(Val, VT::ConnectableInputValue_ScalarBool, 0), Type);
		return true;
	case 4: // ScalarString
		Out = CoerceString(
			TCHAR_TO_UTF8(*FB::GetStr(Val, VT::ConnectableInputValue_ScalarString)), Type);
		return true;
	case 5: // FloatArray
	{
		if (Type == "float[]")
		{
			Out = VtArrayFromWire<pxr::VtFloatArray>(Val, VT::ConnectableInputValue_FloatArray);
			return true;
		}
		return FloatsToValue(
			FB::GetFloatVec(Val, VT::ConnectableInputValue_FloatArray), Type, Out);
	}
	case 6: // IntArray — the encoder types numeric payloads by the declared
	        // type, so an int payload for anything but int[] is malformed.
		if (Type != "int[]") { return false; }
		Out = VtArrayFromWire<pxr::VtIntArray, int32>(Val, VT::ConnectableInputValue_IntArray);
		return true;
	case 7: // StringArray
	{
		const uint32 N = FB::GetVecSize(Val, VT::ConnectableInputValue_StringArray);
		if (Type == "token[]")
		{
			pxr::VtTokenArray Arr(N);
			for (uint32 i = 0; i < N; ++i)
			{
				Arr[i] = pxr::TfToken(TCHAR_TO_UTF8(
					*FB::GetStrVecElem(Val, VT::ConnectableInputValue_StringArray, i)));
			}
			Out = Arr;
		}
		else
		{
			pxr::VtStringArray Arr(N);
			for (uint32 i = 0; i < N; ++i)
			{
				Arr[i] = TCHAR_TO_UTF8(
					*FB::GetStrVecElem(Val, VT::ConnectableInputValue_StringArray, i));
			}
			Out = Arr;
		}
		return true;
	}
	default:
		return false;
	}
}

// OpenPBR shaders: incoming originals accumulate under an "openpbr:" input
// namespace (names like specular_roughness collide with standard_surface
// inputs of different semantics), then the full translated standard_surface
// parameter set re-authors from the accumulated stage state — stateless
// across partial updates. Texture connections into OpenPBR inputs are not
// remapped.
void ApplyOpenPBRInputs(pxr::UsdPrim& Prim, const uint8* Ev, const pxr::UsdTimeCode& Time)
{
	pxr::UsdShadeShader Shader(Prim);
	if (!Shader) return;
	Shader.CreateIdAttr(pxr::VtValue(pxr::TfToken("ND_standard_surface_surfaceshader")));

	pxr::UsdShadeConnectableAPI Connectable(Prim);
	const uint32 Count = FB::GetVecSize(Ev, VT::SetConnectableInput_Inputs);
	for (uint32 i = 0; i < Count; ++i)
	{
		const uint8* Val = FB::GetVecElem(Ev, VT::SetConnectableInput_Inputs, i);
		if (!Val) continue;
		const FString Name     = FB::GetStr(Val, VT::ConnectableInputValue_Name);
		const FString TypeName = FB::GetStr(Val, VT::ConnectableInputValue_TypeName);
		if (Name.IsEmpty()) continue;
		const pxr::SdfValueTypeName SdfType =
			pxr::SdfSchema::GetInstance().FindType(TCHAR_TO_UTF8(*TypeName));
		pxr::VtValue Value;
		if (!SdfType || !DecodeConnectableValue(Val, TypeName, Value))
		{
			UE_LOG(LogUSDEventApplier, Warning,
				TEXT("OpenPBR: undecodable input %s (%s)"), *Name, *TypeName);
			continue;
		}
		const pxr::TfToken Namespaced = ToToken(TEXT("openpbr:") + Name);
		pxr::UsdShadeInput Input = Connectable.GetInput(Namespaced);
		if (!Input) { Input = Connectable.CreateInput(Namespaced, SdfType); }
		Input.Set(Value, Time);
	}

	FOpenPBRParams P = OpenPBRDefaults();
	for (const pxr::UsdShadeInput& Input : Connectable.GetInputs())
	{
		const FString Base = UTF8_TO_TCHAR(Input.GetBaseName().GetString().c_str());
		if (!Base.StartsWith(TEXT("openpbr:"))) continue;
		const FString Name = Base.Mid(8);
		pxr::VtValue V;
		if (!Input.Get(&V, Time)) continue;
		if (V.IsHolding<float>())
		{
			if (Name == TEXT("geometry_thin_walled"))
			{
				P.bThinWalled = V.Get<float>() != 0.0f;
			}
			else if (P.Colors.Contains(Name))
			{
				P.Colors.Add(Name, pxr::GfVec3f(V.Get<float>()));
			}
			else
			{
				P.Scalars.Add(Name, V.Get<float>());
			}
		}
		else if (V.IsHolding<pxr::GfVec3f>())
		{
			P.Colors.Add(Name, V.Get<pxr::GfVec3f>());
		}
		else if (V.IsHolding<bool>())
		{
			P.bThinWalled = V.Get<bool>();
		}
	}

	for (const FStdSurfaceInput& Std : OpenPBRToStandardSurface(P))
	{
		const pxr::TfToken NameTok = ToToken(Std.Name);
		pxr::UsdShadeInput Input = Connectable.GetInput(NameTok);
		if (!Input)
		{
			Input = Connectable.CreateInput(NameTok, Std.bIsColor
				? pxr::SdfValueTypeNames->Color3f : pxr::SdfValueTypeNames->Float);
		}
		if (Std.bIsColor) { Input.Set(Std.Value, Time); }
		else              { Input.Set(Std.Value[0], Time); }
	}
	pxr::UsdShadeInput ThinWalled = Connectable.GetInput(pxr::TfToken("thin_walled"));
	if (!ThinWalled)
	{
		ThinWalled = Connectable.CreateInput(
			pxr::TfToken("thin_walled"), pxr::SdfValueTypeNames->Bool);
	}
	ThinWalled.Set(P.bThinWalled, Time);
}

void ApplySetConnectableInput(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::SetConnectableInput_Prim);
	const FString InfoId   = FB::GetStr(Ev, VT::SetConnectableInput_InfoId);
	if (PrimPath.IsEmpty()) return;

	// A non-empty info_id marks a Shader prim and may create it; an empty one
	// (NodeGraph / Material / light) must already exist so a phantom Shader
	// spec is never authored over a real prim.
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim)
	{
		if (InfoId.IsEmpty()) return;
		Prim = Stage->DefinePrim(ToSdfPath(PrimPath), pxr::TfToken("Shader"));
		if (!Prim) return;
	}

	const pxr::UsdTimeCode Time = FB::HasField(Ev, VT::SetConnectableInput_Time)
		? pxr::UsdTimeCode(FB::GetField<double>(Ev, VT::SetConnectableInput_Time, 0.0))
		: pxr::UsdTimeCode::Default();

	if (InfoId == TEXT("ND_open_pbr_surface_surfaceshader"))
	{
		ApplyOpenPBRInputs(Prim, Ev, Time);
		return;
	}
	if (!InfoId.IsEmpty() && Prim.IsA<pxr::UsdShadeShader>())
	{
		pxr::UsdShadeShader(Prim).CreateIdAttr(pxr::VtValue(ToToken(InfoId)));
	}

	pxr::UsdShadeConnectableAPI Connectable(Prim);
	const uint32 Count = FB::GetVecSize(Ev, VT::SetConnectableInput_Inputs);
	for (uint32 i = 0; i < Count; ++i)
	{
		const uint8* Val = FB::GetVecElem(Ev, VT::SetConnectableInput_Inputs, i);
		if (!Val) continue;
		const FString Name     = FB::GetStr(Val, VT::ConnectableInputValue_Name);
		const FString TypeName = FB::GetStr(Val, VT::ConnectableInputValue_TypeName);
		if (Name.IsEmpty()) continue;

		const pxr::SdfValueTypeName SdfType =
			pxr::SdfSchema::GetInstance().FindType(TCHAR_TO_UTF8(*TypeName));
		if (!SdfType)
		{
			UE_LOG(LogUSDEventApplier, Warning,
				TEXT("SetConnectableInput: unknown type '%s' for %s.%s"),
				*TypeName, *PrimPath, *Name);
			continue;
		}

		pxr::VtValue Value;
		if (!DecodeConnectableValue(Val, TypeName, Value))
		{
			UE_LOG(LogUSDEventApplier, Warning,
				TEXT("SetConnectableInput: undecodable %s value for %s.%s"),
				*TypeName, *PrimPath, *Name);
			continue;
		}

		pxr::UsdShadeInput Input = Connectable.GetInput(ToToken(Name));
		if (!Input)
		{
			Input = Connectable.CreateInput(ToToken(Name), SdfType);
		}
		Input.Set(Value, Time);
	}
}

// "inputs:foo" / "outputs:bar" -> (is-input, base name).
bool SplitQualifiedAttr(const FString& Qualified, bool& bOutIsInput, FString& OutBase)
{
	if (Qualified.StartsWith(TEXT("inputs:")))
	{
		bOutIsInput = true;
		OutBase = Qualified.Mid(7);
		return !OutBase.IsEmpty();
	}
	if (Qualified.StartsWith(TEXT("outputs:")))
	{
		bOutIsInput = false;
		OutBase = Qualified.Mid(8);
		return !OutBase.IsEmpty();
	}
	return false;
}

void ApplySetConnectableConnection(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::SetConnectableConnection_Prim);
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;
	pxr::UsdShadeConnectableAPI Connectable(Prim);

	const uint32 Count = FB::GetVecSize(Ev, VT::SetConnectableConnection_Connections);
	for (uint32 i = 0; i < Count; ++i)
	{
		const uint8* Conn = FB::GetVecElem(Ev, VT::SetConnectableConnection_Connections, i);
		if (!Conn) continue;
		bool bLocalIsInput = false, bSrcIsInput = false;
		FString LocalBase, SrcBase;
		if (!SplitQualifiedAttr(FB::GetStr(Conn, VT::Connection_LocalAttr), bLocalIsInput, LocalBase)) continue;
		if (!SplitQualifiedAttr(FB::GetStr(Conn, VT::Connection_SourceAttr), bSrcIsInput, SrcBase)) continue;

		pxr::UsdPrim SrcPrim = Stage->GetPrimAtPath(
			ToSdfPath(FB::GetStr(Conn, VT::Connection_SourcePrim)));
		if (!SrcPrim)
		{
			UE_LOG(LogUSDEventApplier, Warning,
				TEXT("SetConnectableConnection: missing source prim for %s"), *PrimPath);
			continue;
		}
		pxr::UsdShadeConnectableAPI SrcConn(SrcPrim);

		// Existing port types win; Token otherwise. The core applier adds an
		// Sdr lookup here, but the edge plus the shader's info:id is all the
		// consumer needs to resolve the network.
		pxr::SdfValueTypeName SrcType;
		if (bSrcIsInput)
		{
			pxr::UsdShadeInput P = SrcConn.GetInput(ToToken(SrcBase));
			if (!P) { P = SrcConn.CreateInput(ToToken(SrcBase), pxr::SdfValueTypeNames->Token); }
			SrcType = P.GetTypeName();
		}
		else
		{
			pxr::UsdShadeOutput P = SrcConn.GetOutput(ToToken(SrcBase));
			if (!P) { P = SrcConn.CreateOutput(ToToken(SrcBase), pxr::SdfValueTypeNames->Token); }
			SrcType = P.GetTypeName();
		}
		if (!SrcType) { SrcType = pxr::SdfValueTypeNames->Token; }

		const pxr::UsdShadeConnectionSourceInfo SourceInfo(
			SrcConn, ToToken(SrcBase),
			bSrcIsInput ? pxr::UsdShadeAttributeType::Input
			            : pxr::UsdShadeAttributeType::Output);

		if (bLocalIsInput)
		{
			pxr::UsdShadeInput Local = Connectable.GetInput(ToToken(LocalBase));
			if (!Local) { Local = Connectable.CreateInput(ToToken(LocalBase), SrcType); }
			Local.ConnectToSource(SourceInfo);
		}
		else
		{
			pxr::UsdShadeOutput Local = Connectable.GetOutput(ToToken(LocalBase));
			if (!Local) { Local = Connectable.CreateOutput(ToToken(LocalBase), SrcType); }
			Local.ConnectToSource(SourceInfo);
		}
	}

	const uint32 DiscCount = FB::GetVecSize(Ev, VT::SetConnectableConnection_Disconnections);
	for (uint32 i = 0; i < DiscCount; ++i)
	{
		bool bIsInput = false;
		FString Base;
		if (!SplitQualifiedAttr(
				FB::GetStrVecElem(Ev, VT::SetConnectableConnection_Disconnections, i),
				bIsInput, Base))
		{
			continue;
		}
		if (bIsInput)
		{
			if (pxr::UsdShadeInput P = Connectable.GetInput(ToToken(Base))) { P.ClearSources(); }
		}
		else
		{
			if (pxr::UsdShadeOutput P = Connectable.GetOutput(ToToken(Base))) { P.ClearSources(); }
		}
	}
}

// ---- SetGprimAttrs ----------------------------------------------------------

// Build a VtValue for an AttrValue payload, coerced to the attribute's
// schema-defined type. Same contract as DecodeConnectableValue; the strided
// float-array cases cover the bulk mesh path (points, normals, UVs).
bool DecodeGprimAttrValue(const uint8* Val, const std::string& Type, pxr::VtValue& Out)
{
	const uint8 Kind = FB::GetField<uint8>(Val, VT::AttrValue_ValueType, 0);
	switch (Kind)
	{
	case 1: // ScalarFloat
		Out = CoerceNumericScalar(
			FB::GetField<float>(Val, VT::AttrValue_ScalarFloat, 0.0f), Type);
		return true;
	case 2: // ScalarInt
		Out = CoerceNumericScalar(
			FB::GetField<int32>(Val, VT::AttrValue_ScalarInt, 0), Type);
		return true;
	case 3: // ScalarBool
		Out = CoerceNumericScalar(
			FB::GetField<uint8>(Val, VT::AttrValue_ScalarBool, 0), Type);
		return true;
	case 4: // ScalarString
	case 7: // StringVal
		Out = CoerceString(
			TCHAR_TO_UTF8(*FB::GetStr(Val, VT::AttrValue_ScalarString)), Type);
		return true;
	case 5: // FloatArray
	{
		if (TypeIs(Type, {"float3[]", "vector3f[]", "normal3f[]", "point3f[]", "color3f[]"}))
		{
			Out = VtArrayFromWire<pxr::VtVec3fArray>(Val, VT::AttrValue_FloatArray);
			return true;
		}
		if (TypeIs(Type, {"float2[]", "texCoord2f[]"}))
		{
			Out = VtArrayFromWire<pxr::VtVec2fArray>(Val, VT::AttrValue_FloatArray);
			return true;
		}
		if (Type == "float[]")
		{
			Out = VtArrayFromWire<pxr::VtFloatArray>(Val, VT::AttrValue_FloatArray);
			return true;
		}
		return FloatsToValue(FB::GetFloatVec(Val, VT::AttrValue_FloatArray), Type, Out);
	}
	case 6: // IntArray — gprim attrs carry no declared type on the wire, so
	        // the prim's schema type drives; int payloads for float-typed
	        // schema attrs (a hand-authored "radius": 1) convert per element.
		if (Type == "int[]")
		{
			Out = VtArrayFromWire<pxr::VtIntArray, int32>(Val, VT::AttrValue_IntArray);
			return true;
		}
		return FloatsToValue(FloatsFromIntWire(Val, VT::AttrValue_IntArray), Type, Out);
	default: // None / NestedList (JSON fallback for exotic types)
		return false;
	}
}

void ApplySetGprimAttrs(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::SetGprimAttrs_Prim);
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	const pxr::UsdTimeCode Time = FB::HasField(Ev, VT::SetGprimAttrs_Time)
		? pxr::UsdTimeCode(FB::GetField<double>(Ev, VT::SetGprimAttrs_Time, 0.0))
		: pxr::UsdTimeCode::Default();

	TMap<FString, TPair<FString, FString>> PrimvarMeta; // name -> (type, interpolation)
	const uint32 MetaCount = FB::GetVecSize(Ev, VT::SetGprimAttrs_PrimvarMeta);
	for (uint32 i = 0; i < MetaCount; ++i)
	{
		const uint8* Meta = FB::GetVecElem(Ev, VT::SetGprimAttrs_PrimvarMeta, i);
		if (!Meta) continue;
		PrimvarMeta.Add(
			FB::GetStr(Meta, VT::PrimvarMeta_AttrName),
			TPair<FString, FString>(
				FB::GetStr(Meta, VT::PrimvarMeta_TypeName),
				FB::GetStr(Meta, VT::PrimvarMeta_Interpolation)));
	}

	pxr::UsdGeomPrimvarsAPI PvApi(Prim);
	const uint32 Count = FB::GetVecSize(Ev, VT::SetGprimAttrs_Attrs);
	for (uint32 i = 0; i < Count; ++i)
	{
		const uint8* Entry = FB::GetVecElem(Ev, VT::SetGprimAttrs_Attrs, i);
		if (!Entry) continue;
		const FString Name = FB::GetStr(Entry, VT::NamedAttr_Name);
		const uint8*  Val  = FB::GetPtr(Entry, VT::NamedAttr_Value);
		if (Name.IsEmpty() || !Val) continue;

		// Non-schema primvars are created from their wire metadata; schema
		// attributes (points, radius, camera params) already exist typed.
		pxr::UsdAttribute Attr = Prim.GetAttribute(ToToken(Name));
		const TPair<FString, FString>* Meta = PrimvarMeta.Find(Name);
		if (!Attr && Meta && Name.StartsWith(TEXT("primvars:")))
		{
			const pxr::SdfValueTypeName SdfType =
				pxr::SdfSchema::GetInstance().FindType(TCHAR_TO_UTF8(*Meta->Key));
			if (SdfType)
			{
				Attr = PvApi.CreatePrimvar(
					ToToken(Name.Mid(9)), SdfType, ToToken(Meta->Value)).GetAttr();
			}
		}
		if (!Attr) continue;

		pxr::VtValue Value;
		const std::string AttrType = Attr.GetTypeName().GetAsToken().GetString();
		if (!DecodeGprimAttrValue(Val, AttrType, Value))
		{
			UE_LOG(LogUSDEventApplier, Warning,
				TEXT("SetGprimAttrs: undecodable value for %s.%s (%hs)"),
				*PrimPath, *Name, AttrType.c_str());
			continue;
		}
		Attr.Set(Value, Time);
	}

	// Interpolation on primvars: schema-defined ones (e.g. displayColor) can
	// carry an authored interpolation that differs from their default.
	for (const auto& Pair : PrimvarMeta)
	{
		if (Pair.Value.Value.IsEmpty() || !Pair.Key.StartsWith(TEXT("primvars:"))) continue;
		pxr::UsdGeomPrimvar Pv = PvApi.GetPrimvar(ToToken(Pair.Key.Mid(9)));
		if (Pv)
		{
			Pv.SetInterpolation(ToToken(Pair.Value.Value));
		}
	}

	// Interpolation metadata on non-primvar attributes (e.g. normals).
	const uint32 InterpCount = FB::GetVecSize(Ev, VT::SetGprimAttrs_AttrInterp);
	for (uint32 i = 0; i < InterpCount; ++i)
	{
		const uint8* Entry = FB::GetVecElem(Ev, VT::SetGprimAttrs_AttrInterp, i);
		if (!Entry) continue;
		pxr::UsdAttribute Attr =
			Prim.GetAttribute(ToToken(FB::GetStr(Entry, VT::AttrInterp_AttrName)));
		const FString Interp = FB::GetStr(Entry, VT::AttrInterp_Interpolation);
		if (Attr && !Interp.IsEmpty())
		{
			Attr.SetMetadata(pxr::TfToken("interpolation"), ToToken(Interp));
		}
	}
}

// ---- SetInstanceable / SetPointInstancer ------------------------------------

void ApplySetInstanceable(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::SetInstanceable_Prim);
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim)
	{
		Prim = Stage->DefinePrim(ToSdfPath(PrimPath), pxr::TfToken());
		if (!Prim) return;
	}
	Prim.SetInstanceable(FB::GetField<uint8>(Ev, VT::SetInstanceable_Instanceable, 1) != 0);
}

void ApplySetPointInstancer(pxr::UsdStageRefPtr& Stage, const uint8* Ev)
{
	const FString PrimPath = FB::GetStr(Ev, VT::SetPointInstancer_Prim);
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;
	pxr::UsdGeomPointInstancer Instancer(Prim);
	if (!Instancer) return;

	// Field bits follow POINT_INSTANCER_FIELDS order in protocol_constants.py.
	const uint32 Fields = FB::GetField<uint32>(Ev, VT::SetPointInstancer_Fields, 0);
	const pxr::UsdTimeCode Time = FB::HasField(Ev, VT::SetPointInstancer_Time)
		? pxr::UsdTimeCode(FB::GetField<double>(Ev, VT::SetPointInstancer_Time, 0.0))
		: pxr::UsdTimeCode::Default();

	if (Fields & 1) // prototypes
	{
		pxr::SdfPathVector Targets;
		const uint32 N = FB::GetVecSize(Ev, VT::SetPointInstancer_Prototypes);
		for (uint32 i = 0; i < N; ++i)
		{
			Targets.push_back(ToSdfPath(
				FB::GetStrVecElem(Ev, VT::SetPointInstancer_Prototypes, i)));
		}
		Instancer.CreatePrototypesRel().SetTargets(Targets);
	}
	if (Fields & 2) // proto_indices
	{
		Instancer.CreateProtoIndicesAttr().Set(
			VtArrayFromWire<pxr::VtIntArray, int32>(Ev, VT::SetPointInstancer_ProtoIndices), Time);
	}
	if (Fields & 8) // orientations: authored to orientationsf, wins resolution
	{
		Instancer.CreateOrientationsfAttr().Set(
			QuatArrayFromWire(Ev, VT::SetPointInstancer_Orientations), Time);
	}

	struct FVec3Field
	{
		uint32 Bit;
		uint16 Field;
		pxr::UsdAttribute (pxr::UsdGeomPointInstancer::*CreateAttr)(const pxr::VtValue&, bool) const;
	};
	static const FVec3Field Vec3Fields[] =
	{
		{ 4,   VT::SetPointInstancer_Positions,         &pxr::UsdGeomPointInstancer::CreatePositionsAttr },
		{ 16,  VT::SetPointInstancer_Scales,            &pxr::UsdGeomPointInstancer::CreateScalesAttr },
		{ 32,  VT::SetPointInstancer_Velocities,        &pxr::UsdGeomPointInstancer::CreateVelocitiesAttr },
		{ 64,  VT::SetPointInstancer_Accelerations,     &pxr::UsdGeomPointInstancer::CreateAccelerationsAttr },
		{ 128, VT::SetPointInstancer_AngularVelocities, &pxr::UsdGeomPointInstancer::CreateAngularVelocitiesAttr },
	};
	for (const FVec3Field& F : Vec3Fields)
	{
		if (Fields & F.Bit)
		{
			(Instancer.*F.CreateAttr)(pxr::VtValue(), false).Set(
				VtArrayFromWire<pxr::VtVec3fArray>(Ev, F.Field), Time);
		}
	}

	if (Fields & 256) // ids
	{
		Instancer.CreateIdsAttr().Set(
			VtArrayFromWire<pxr::VtInt64Array, int64>(Ev, VT::SetPointInstancer_Ids), Time);
	}
	if (Fields & 512) // invisible_ids
	{
		Instancer.CreateInvisibleIdsAttr().Set(
			VtArrayFromWire<pxr::VtInt64Array, int64>(Ev, VT::SetPointInstancer_InvisibleIds), Time);
	}
	if (Fields & 1024) // inactive_ids: prim metadata as an explicit list op
	{
		const pxr::VtInt64Array Ids =
			VtArrayFromWire<pxr::VtInt64Array, int64>(Ev, VT::SetPointInstancer_InactiveIds);
		pxr::SdfInt64ListOp ListOp;
		ListOp.SetExplicitItems(std::vector<int64_t>(Ids.cbegin(), Ids.cend()));
		Prim.SetMetadata(pxr::TfToken("inactiveIds"), ListOp);
	}
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
	case kEvSetGprimAttrs:        ApplySetGprimAttrs(Stage, Ev);        break;
	case kEvSetInstanceable:      ApplySetInstanceable(Stage, Ev);      break;
	case kEvSetPointInstancer:    ApplySetPointInstancer(Stage, Ev);    break;
	case kEvSetReference:         ApplySetReference(Stage, Ev);         break;
	case kEvSetPayload:           ApplySetPayload(Stage, Ev);           break;
	case kEvLoadPayload:          ApplyLoadPayload(Stage, Ev);          break;
	case kEvUnloadPayload:        ApplyUnloadPayload(Stage, Ev);        break;
	case kEvSetVariantSelections: ApplySetVariantSelections(Stage, Ev); break;
	case kEvSetMaterialBinding:   ApplySetMaterialBinding(Stage, Ev);   break;
	case kEvSetConnectableInput:      ApplySetConnectableInput(Stage, Ev);      break;
	case kEvSetConnectableConnection: ApplySetConnectableConnection(Stage, Ev); break;
	case kEvSetStageMetadata:     ApplySetStageMetadata(Stage, Ev);     break;
	default:
		UE_LOG(LogUSDEventApplier, Warning, TEXT("Unknown event kind: %u"), EventKind);
		break;
	}
}

// Value writes on existing prims batch inside an SdfChangeBlock; every other
// kind mutates composition (DefinePrim, arcs, variants, schema application)
// and must see the stage recompose as it goes, so it runs outside. Mirrors
// the core applier's STRUCTURAL_EVENT_KINDS split.
bool UsesChangeBlock(uint8 EventKind)
{
	switch (EventKind)
	{
	case kEvSetXformTrs:
	case kEvSetVisibility:
	case kEvSetGprimAttrs:
	case kEvSetPointInstancer:
	case kEvDeactivatePrim:
		return true;
	default:
		return false;
	}
}

} // namespace
#endif // USE_USD_SDK

// ---------------------------------------------------------------------------
// FUSDEventApplier::FrameUsesChangeBlock
// ---------------------------------------------------------------------------
bool FUSDEventApplier::FrameUsesChangeBlock(const TArray<uint8>& RawFrame)
{
	if (RawFrame.Num() < 8) return false;
#if USE_USD_SDK
	const uint8* Env = FB::GetRoot(RawFrame);
	if (!Env) return false;
	const uint8* BcEvent = FB::GetPtr(Env, VT::Envelope_Payload);
	if (!BcEvent) return false;
	const uint8* EventWrapper = FB::GetPtr(BcEvent, VT::BroadcastEvent_Event);
	if (!EventWrapper) return false;
	return UsesChangeBlock(
		FB::GetField<uint8>(EventWrapper, VT::EventWrapper_EventType, 0));
#else
	return false;
#endif
}

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

	// Structural events must apply outside an SdfChangeBlock: recomposition is
	// deferred until the block closes, so UsdStage::DefinePrim cannot return
	// the newly defined prim and arc edits act on a stale composed view. Value
	// writes on existing prims are ChangeBlock-safe and batch into one
	// consolidated ObjectsChanged notice, which the stage actor's FUsdListener
	// turns into a single scene refresh.
	if (UsesChangeBlock(EventKind))
	{
		pxr::SdfChangeBlock ChangeBlock;
		DispatchEvent(PxrStage, EventKind, EventTable);
	}
	else
	{
		DispatchEvent(PxrStage, EventKind, EventTable);
	}
#else
	UE_LOG(LogUSDEventApplier, Warning,
		TEXT("USD SDK not available — cannot apply USD events"));
#endif
}
