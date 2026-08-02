// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "USDEventApplier.h"

#include "Logging/LogMacros.h"
#include "Containers/Array.h"

#include "USDStageActor.h"
#include "UsdWrappers/UsdStage.h"
#include "UsdWrappers/SdfPath.h"

#if USE_USD_SDK
#include "USDIncludesStart.h"

#include "pxr/usd/usd/stage.h"
#include "pxr/usd/usd/editTarget.h"
#include "pxr/usd/usd/prim.h"
#include "pxr/usd/usd/timeCode.h"
#include "pxr/usd/usd/variantSets.h"
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
#include "pxr/usd/sdr/registry.h"
#include "pxr/usd/sdr/shaderNode.h"
#include "pxr/usd/sdr/shaderProperty.h"
#include "pxr/usd/sdf/changeBlock.h"
#include "pxr/usd/sdf/copyUtils.h"
#include "pxr/usd/sdf/layer.h"
#include "pxr/usd/sdf/namespaceEdit.h"
#include "pxr/usd/sdf/primSpec.h"
#include "pxr/usd/sdf/reference.h"
#include "pxr/usd/sdf/payload.h"
#include "pxr/usd/sdf/schema.h"
#include "pxr/usd/sdf/variantSetSpec.h"
#include "pxr/usd/sdf/variantSpec.h"
#include "pxr/usd/sdf/assetPath.h"
#include "pxr/usd/sdf/valueTypeName.h"
#include "pxr/usd/sdf/listOp.h"
#include "pxr/usd/sdf/layerOffset.h"
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

struct FUSDEventChangeBlock::FImpl
{
#if USE_USD_SDK
	pxr::SdfChangeBlock ChangeBlock;
#endif
};

FUSDEventChangeBlock::FUSDEventChangeBlock()
	: Impl(MakeUnique<FImpl>())
{
}

FUSDEventChangeBlock::~FUSDEventChangeBlock() = default;

DEFINE_LOG_CATEGORY_STATIC(LogUSDEventApplier, Log, All);

using namespace OUC;
namespace Wire = OpenUSDConnect;

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

pxr::UsdTimeCode ToTimeCode(::flatbuffers::Optional<double> Time)
{
	return Time.has_value() ? pxr::UsdTimeCode(*Time) : pxr::UsdTimeCode::Default();
}

// ---- EnsurePrim -----------------------------------------------------------
void ApplyEnsurePrim(pxr::UsdStageRefPtr& Stage, const Wire::EnsurePrim* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	const FString TypeName = ToFString(Ev->type_name());
	if (PrimPath.IsEmpty()) return;

	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim || !TypeName.IsEmpty())
	{
		Prim = Stage->DefinePrim(
			ToSdfPath(PrimPath),
			TypeName.IsEmpty() ? pxr::TfToken() : ToToken(TypeName));
	}

	// Apply API schemas listed in the event
	if (const auto* Schemas = Ev->api_schemas())
	{
		for (const ::flatbuffers::String* Schema : *Schemas)
		{
			if (Schema && Schema->size() > 0)
			{
				Prim.ApplyAPI(pxr::TfToken(Schema->c_str()));
			}
		}
	}
}

// ---- EnsureXformOps -------------------------------------------------------
void ApplyEnsureXformOps(pxr::UsdStageRefPtr& Stage, const Wire::EnsureXformOps* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (PrimPath.IsEmpty()) return;

	const pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	pxr::UsdGeomXformable Xformable(Prim);
	if (!Xformable) return;

	const pxr::TfToken TranslateName("xformOp:translate");
	const pxr::TfToken OrientName("xformOp:orient");
	const pxr::TfToken ScaleName("xformOp:scale");

	Prim.CreateAttribute(TranslateName, pxr::SdfValueTypeNames->Double3, false);
	Prim.CreateAttribute(OrientName, pxr::SdfValueTypeNames->Quatf, false);
	Prim.CreateAttribute(ScaleName, pxr::SdfValueTypeNames->Float3, false);

	const pxr::VtTokenArray Order{TranslateName, OrientName, ScaleName};
	Xformable.CreateXformOpOrderAttr().Set(Order);
}

// ---- SetXformTrs ----------------------------------------------------------
void ApplySetXformTrs(pxr::UsdStageRefPtr& Stage, const Wire::SetXformTrs* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
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

	const pxr::UsdTimeCode Time = ToTimeCode(Ev->time());
	const uint8 Fields = Ev->fields();

	if (Fields & 1)  // Translate
	{
		const auto* T = Ev->t();
		if (T && T->size() >= 3)
		{
			if (!TranslateOp) TranslateOp = Xformable.AddTranslateOp();
			TranslateOp.Set(pxr::GfVec3d(T->Get(0), T->Get(1), T->Get(2)), Time);
		}
	}
	if (Fields & 2)  // Rotate (quaternion [w,x,y,z])
	{
		const auto* R = Ev->r();
		if (R && R->size() >= 4)
		{
			if (!RotateOp) RotateOp = Xformable.AddOrientOp();
			RotateOp.Set(
				pxr::GfQuatf(R->Get(0), pxr::GfVec3f(R->Get(1), R->Get(2), R->Get(3))), Time);
		}
	}
	if (Fields & 4)  // Scale
	{
		const auto* S = Ev->s();
		if (S && S->size() >= 3)
		{
			if (!ScaleOp) ScaleOp = Xformable.AddScaleOp();
			ScaleOp.Set(pxr::GfVec3f(S->Get(0), S->Get(1), S->Get(2)), Time);
		}
	}
}

// ---- DeletePrim -----------------------------------------------------------
void ApplyDeletePrim(pxr::UsdStageRefPtr& Stage, const Wire::DeletePrim* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (!PrimPath.IsEmpty())
	{
		Stage->RemovePrim(ToSdfPath(PrimPath));
	}
}

// ---- DeactivatePrim -------------------------------------------------------
void ApplyDeactivatePrim(pxr::UsdStageRefPtr& Stage, const Wire::DeactivatePrim* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;
	Prim.SetActive(Ev->active());
}

// ---- RenamePrim -------------------------------------------------------------
void ApplyRenamePrim(pxr::UsdStageRefPtr& Stage, const Wire::RenamePrim* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	const FString NewName  = ToFString(Ev->new_name());
	if (PrimPath.IsEmpty() || NewName.IsEmpty()) return;

	const pxr::UsdEditTarget EditTarget = Stage->GetEditTarget();
	const pxr::SdfPath SpecPath = EditTarget.MapToSpecPath(ToSdfPath(PrimPath));
	const pxr::SdfLayerHandle Layer = EditTarget.GetLayer();
	if (SpecPath.IsEmpty() || !Layer || !Layer->GetPrimAtPath(SpecPath)) return;

	pxr::SdfBatchNamespaceEdit Edits;
	Edits.Add(pxr::SdfNamespaceEdit::Rename(SpecPath, ToToken(NewName)));
	if (!Layer->Apply(Edits))
	{
		UE_LOG(LogUSDEventApplier, Warning,
			TEXT("RenamePrim: could not rename %s to %s"), *PrimPath, *NewName);
	}
}

// ---- SetVisibility --------------------------------------------------------
void ApplySetVisibility(pxr::UsdStageRefPtr& Stage, const Wire::SetVisibility* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (PrimPath.IsEmpty()) return;

	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	pxr::UsdGeomImageable Imageable(Prim);
	if (!Imageable) return;

	const pxr::UsdTimeCode Time = ToTimeCode(Ev->time());
	if (Ev->visible())
	{
		Imageable.MakeVisible(Time);
	}
	else
	{
		Imageable.MakeInvisible(Time);
	}
}

// ---- SetStageMetadata -----------------------------------------------------
void ApplySetStageMetadata(pxr::UsdStageRefPtr& Stage, const Wire::SetStageMetadata* Ev)
{
	if (Ev->timeCodesPerSecond().has_value())
	{
		Stage->SetTimeCodesPerSecond(*Ev->timeCodesPerSecond());
	}
	if (Ev->framesPerSecond().has_value())
	{
		Stage->SetFramesPerSecond(*Ev->framesPerSecond());
	}
	if (Ev->startTimeCode().has_value())
	{
		Stage->SetStartTimeCode(*Ev->startTimeCode());
	}
	if (Ev->endTimeCode().has_value())
	{
		Stage->SetEndTimeCode(*Ev->endTimeCode());
	}
	if (Ev->metersPerUnit().has_value())
	{
		Stage->SetMetadata(pxr::TfToken("metersPerUnit"), *Ev->metersPerUnit());
	}
	// upAxis: USDImporter reads this separately on stage open — skip for now
}

// ---- SetReference ---------------------------------------------------------
void ApplySetReference(pxr::UsdStageRefPtr& Stage, const Wire::SetReference* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (PrimPath.IsEmpty()) return;

	const pxr::UsdEditTarget EditTarget = Stage->GetEditTarget();
	const pxr::SdfLayerHandle TargetLayer = EditTarget.GetLayer();
	const pxr::SdfPath TargetPath = EditTarget.MapToSpecPath(ToSdfPath(PrimPath));
	if (!TargetLayer || TargetPath.IsEmpty()) return;
	if (!Ev->list_op_authored())
	{
		TargetLayer->EraseField(TargetPath, pxr::SdfFieldKeys->References);
		return;
	}

	std::vector<pxr::SdfReference> ExplicitItems;
	std::vector<pxr::SdfReference> AddedItems;
	std::vector<pxr::SdfReference> PrependedItems;
	std::vector<pxr::SdfReference> AppendedItems;
	std::vector<pxr::SdfReference> DeletedItems;
	std::vector<pxr::SdfReference> OrderedItems;
	if (const auto* Arcs = Ev->refs())
	{
		for (const Wire::ArcEntry* Arc : *Arcs)
		{
			if (!Arc) continue;
			const FString ArcPrimPath = ToFString(Arc->prim_path());
			pxr::VtDictionary CustomData;
			if (Arc->custom_data_fragment() && !Arc->custom_data_fragment()->str().empty())
			{
				const pxr::SdfLayerRefPtr Fragment =
					pxr::SdfLayer::CreateAnonymous("openusdconnect-reference-custom-data");
				if (!Fragment->ImportFromString(Arc->custom_data_fragment()->str()))
				{
					UE_LOG(LogUSDEventApplier, Warning,
						TEXT("Invalid reference customData fragment for %s"), *PrimPath);
					return;
				}
				CustomData = Fragment->GetCustomLayerData();
			}
			const pxr::SdfReference Item(
				Arc->asset_path() ? Arc->asset_path()->str() : std::string(),
				ArcPrimPath.IsEmpty() ? pxr::SdfPath() : ToSdfPath(ArcPrimPath),
				pxr::SdfLayerOffset(Arc->layer_offset(), Arc->layer_scale()),
				CustomData);
			switch (Arc->list_position())
			{
			case Wire::ArcListPosition::Explicit: ExplicitItems.push_back(Item); break;
			case Wire::ArcListPosition::Added: AddedItems.push_back(Item); break;
			case Wire::ArcListPosition::Prepended: PrependedItems.push_back(Item); break;
			case Wire::ArcListPosition::Appended: AppendedItems.push_back(Item); break;
			case Wire::ArcListPosition::Deleted: DeletedItems.push_back(Item); break;
			case Wire::ArcListPosition::Ordered: OrderedItems.push_back(Item); break;
			default: return;
			}
		}
	}

	pxr::SdfReferenceListOp ListOp;
	if (Ev->list_op_explicit())
	{
		if (!AddedItems.empty() || !PrependedItems.empty() || !AppendedItems.empty() ||
			!DeletedItems.empty() || !OrderedItems.empty()) return;
		ListOp.SetExplicitItems(ExplicitItems);
	}
	else
	{
		if (!ExplicitItems.empty()) return;
		ListOp.SetAddedItems(AddedItems);
		ListOp.SetPrependedItems(PrependedItems);
		ListOp.SetAppendedItems(AppendedItems);
		ListOp.SetDeletedItems(DeletedItems);
		ListOp.SetOrderedItems(OrderedItems);
	}
	pxr::SdfCreatePrimInLayer(TargetLayer, TargetPath);
	TargetLayer->SetField(TargetPath, pxr::SdfFieldKeys->References, ListOp);
}

// ---- SetPayload -----------------------------------------------------------
void ApplySetPayload(pxr::UsdStageRefPtr& Stage, const Wire::SetPayload* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (PrimPath.IsEmpty()) return;

	const pxr::UsdEditTarget EditTarget = Stage->GetEditTarget();
	const pxr::SdfLayerHandle TargetLayer = EditTarget.GetLayer();
	const pxr::SdfPath TargetPath = EditTarget.MapToSpecPath(ToSdfPath(PrimPath));
	if (!TargetLayer || TargetPath.IsEmpty()) return;
	if (!Ev->list_op_authored())
	{
		TargetLayer->EraseField(TargetPath, pxr::SdfFieldKeys->Payload);
		return;
	}

	std::vector<pxr::SdfPayload> ExplicitItems;
	std::vector<pxr::SdfPayload> AddedItems;
	std::vector<pxr::SdfPayload> PrependedItems;
	std::vector<pxr::SdfPayload> AppendedItems;
	std::vector<pxr::SdfPayload> DeletedItems;
	std::vector<pxr::SdfPayload> OrderedItems;
	if (const auto* Arcs = Ev->payloads())
	{
		for (const Wire::ArcEntry* Arc : *Arcs)
		{
			if (!Arc) continue;
			if (Arc->custom_data_fragment() && !Arc->custom_data_fragment()->str().empty())
			{
				UE_LOG(LogUSDEventApplier, Warning,
					TEXT("Payload arc carried reference customData for %s"), *PrimPath);
				return;
			}
			const FString ArcPrimPath = ToFString(Arc->prim_path());
			const pxr::SdfPayload Item(
				Arc->asset_path() ? Arc->asset_path()->str() : std::string(),
				ArcPrimPath.IsEmpty() ? pxr::SdfPath() : ToSdfPath(ArcPrimPath),
				pxr::SdfLayerOffset(Arc->layer_offset(), Arc->layer_scale()));
			switch (Arc->list_position())
			{
			case Wire::ArcListPosition::Explicit: ExplicitItems.push_back(Item); break;
			case Wire::ArcListPosition::Added: AddedItems.push_back(Item); break;
			case Wire::ArcListPosition::Prepended: PrependedItems.push_back(Item); break;
			case Wire::ArcListPosition::Appended: AppendedItems.push_back(Item); break;
			case Wire::ArcListPosition::Deleted: DeletedItems.push_back(Item); break;
			case Wire::ArcListPosition::Ordered: OrderedItems.push_back(Item); break;
			default: return;
			}
		}
	}

	pxr::SdfPayloadListOp ListOp;
	if (Ev->list_op_explicit())
	{
		if (!AddedItems.empty() || !PrependedItems.empty() || !AppendedItems.empty() ||
			!DeletedItems.empty() || !OrderedItems.empty()) return;
		ListOp.SetExplicitItems(ExplicitItems);
	}
	else
	{
		if (!ExplicitItems.empty()) return;
		ListOp.SetAddedItems(AddedItems);
		ListOp.SetPrependedItems(PrependedItems);
		ListOp.SetAppendedItems(AppendedItems);
		ListOp.SetDeletedItems(DeletedItems);
		ListOp.SetOrderedItems(OrderedItems);
	}
	pxr::SdfCreatePrimInLayer(TargetLayer, TargetPath);
	TargetLayer->SetField(TargetPath, pxr::SdfFieldKeys->Payload, ListOp);
}

// ---- LoadPayload / UnloadPayload ------------------------------------------
void ApplyLoadPayload(pxr::UsdStageRefPtr& Stage, const Wire::LoadPayload* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (!PrimPath.IsEmpty()) Stage->Load(ToSdfPath(PrimPath));
}

void ApplyUnloadPayload(pxr::UsdStageRefPtr& Stage, const Wire::UnloadPayload* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (!PrimPath.IsEmpty()) Stage->Unload(ToSdfPath(PrimPath));
}

// ---- SetVariantSelections -------------------------------------------------
void ApplySetVariantSelections(pxr::UsdStageRefPtr& Stage, const Wire::SetVariantSelections* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	pxr::UsdVariantSets VarSets = Prim.GetVariantSets();
	if (const auto* Selections = Ev->selections())
	{
		for (const Wire::StringPair* Pair : *Selections)
		{
			if (!Pair || !Pair->key() || Pair->key()->size() == 0) continue;
			pxr::UsdVariantSet VSet = VarSets.GetVariantSet(Pair->key()->str());
			const std::string Value = Pair->value() ? Pair->value()->str() : std::string();
			if (Value.empty())
			{
				VSet.ClearVariantSelection();
			}
			else
			{
				VSet.SetVariantSelection(Value);
			}
		}
	}
}

// ---- SetMaterialBinding ---------------------------------------------------
void ApplySetMaterialBinding(pxr::UsdStageRefPtr& Stage, const Wire::SetMaterialBinding* Ev)
{
	const FString PrimPath     = ToFString(Ev->prim());
	const FString MaterialPath = ToFString(Ev->material_path());
	const FString Purpose      = ToFString(Ev->material_purpose());
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
VtArrayType VtArrayFromWire(const ::flatbuffers::Vector<WireScalar>* Vec)
{
	using ElemType = typename VtArrayType::value_type;
	if (!Vec) { return {}; }
	const size_t Bytes = static_cast<size_t>(Vec->size()) * sizeof(WireScalar);
	VtArrayType Out(Bytes / sizeof(ElemType));
	FMemory::Memcpy(Out.data(), Vec->data(), Out.size() * sizeof(ElemType));
	return Out;
}

// Wire quaternions are [w, x, y, z] rows; GfQuatf stores imaginary first,
// so rows go through the (real, i, j, k) constructor instead of a memcpy.
pxr::VtQuatfArray QuatArrayFromWire(const ::flatbuffers::Vector<float>* Vec)
{
	if (!Vec) { return {}; }
	const uint32 N = Vec->size() / 4;
	const float* F = Vec->data();
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

TArray<float> ToFloatArray(const ::flatbuffers::Vector<float>* Vec)
{
	if (!Vec) { return {}; }
	TArray<float> Out;
	Out.SetNumUninitialized(static_cast<int32>(Vec->size()));
	FMemory::Memcpy(Out.GetData(), Vec->data(), Vec->size() * sizeof(float));
	return Out;
}

// Integral payloads destined for float-typed attributes (a JSON [1, 1, 1]
// packs as ints even for a color3f input) convert per element.
TArray<float> FloatsFromIntWire(const ::flatbuffers::Vector<int32_t>* Vec)
{
	if (!Vec) { return {}; }
	TArray<float> Out;
	Out.SetNumUninitialized(static_cast<int32>(Vec->size()));
	for (uint32 i = 0; i < Vec->size(); ++i) { Out[i] = static_cast<float>(Vec->Get(i)); }
	return Out;
}

// ---- SetConnectableInput / SetConnectableConnection -----------------------

// Build a VtValue for a ConnectableInputValue payload according to its USD
// type name. Mirrors the core applier's value coercion table. Fixed-size
// types demand exact element counts — a mismatch is a malformed encoder and
// must surface, not truncate.
bool DecodeConnectableValue(const Wire::ConnectableInputValue* Val, const FString& TypeNameStr, pxr::VtValue& Out)
{
	const std::string Type = TCHAR_TO_UTF8(*TypeNameStr);
	switch (Val->value_type())
	{
	case Wire::ConnectableInputValueType::ScalarFloat:
		Out = CoerceNumericScalar(Val->scalar_float(), Type);
		return true;
	case Wire::ConnectableInputValueType::ScalarInt:
		Out = CoerceNumericScalar(Val->scalar_int(), Type);
		return true;
	case Wire::ConnectableInputValueType::ScalarBool:
		Out = CoerceNumericScalar(Val->scalar_bool() ? 1.0 : 0.0, Type);
		return true;
	case Wire::ConnectableInputValueType::ScalarString:
		Out = CoerceString(
			Val->scalar_string() ? Val->scalar_string()->str() : std::string(), Type);
		return true;
	case Wire::ConnectableInputValueType::FloatArray:
	{
		if (Type == "float[]")
		{
			Out = VtArrayFromWire<pxr::VtFloatArray>(Val->float_array());
			return true;
		}
		return FloatsToValue(ToFloatArray(Val->float_array()), Type, Out);
	}
	case Wire::ConnectableInputValueType::IntArray:
		// The encoder types numeric payloads by the declared type, so an int
		// payload for anything but int[] is malformed.
		if (Type != "int[]") { return false; }
		Out = VtArrayFromWire<pxr::VtIntArray, int32_t>(Val->int_array());
		return true;
	case Wire::ConnectableInputValueType::StringArray:
	{
		const auto* Strings = Val->string_array();
		const uint32 N = Strings ? Strings->size() : 0;
		if (Type == "token[]")
		{
			pxr::VtTokenArray Arr(N);
			for (uint32 i = 0; i < N; ++i)
			{
				Arr[i] = pxr::TfToken(Strings->Get(i)->str());
			}
			Out = Arr;
		}
		else
		{
			pxr::VtStringArray Arr(N);
			for (uint32 i = 0; i < N; ++i)
			{
				Arr[i] = Strings->Get(i)->str();
			}
			Out = Arr;
		}
		return true;
	}
	default:
		return false;
	}
}

void ApplySetConnectableInput(pxr::UsdStageRefPtr& Stage, const Wire::SetConnectableInput* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	const FString InfoId   = ToFString(Ev->info_id());
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

	const pxr::UsdTimeCode Time = ToTimeCode(Ev->time());

	if (!InfoId.IsEmpty() && Prim.IsA<pxr::UsdShadeShader>())
	{
		const pxr::TfToken DesiredId = ToToken(InfoId);
		const pxr::UsdShadeShader Shader(Prim);
		pxr::TfToken CurrentId;
		const pxr::UsdAttribute IdAttr = Shader.GetIdAttr();
		if (!IdAttr || !IdAttr.Get(&CurrentId) || CurrentId != DesiredId)
		{
			Shader.CreateIdAttr(pxr::VtValue(DesiredId));
		}
	}

	pxr::UsdShadeConnectableAPI Connectable(Prim);
	const auto* Inputs = Ev->inputs();
	if (!Inputs) return;
	for (const Wire::ConnectableInputValue* Val : *Inputs)
	{
		if (!Val) continue;
		const FString Name     = ToFString(Val->name());
		const FString TypeName = ToFString(Val->type_name());
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

pxr::SdfValueTypeName ResolveShaderPortType(
	const pxr::UsdPrim& Prim,
	const pxr::TfToken& PortName,
	bool bIsInput)
{
	if (!Prim) return {};

	pxr::TfToken ShaderId;
	const pxr::UsdAttribute IdAttr = Prim.GetAttribute(pxr::TfToken("info:id"));
	if (!IdAttr || !IdAttr.Get(&ShaderId) || ShaderId.IsEmpty()) return {};

	const pxr::SdrShaderNodeConstPtr Node =
		pxr::SdrRegistry::GetInstance().GetShaderNodeByIdentifier(ShaderId);
	if (!Node) return {};

	const pxr::SdrShaderPropertyConstPtr Property = bIsInput
		? Node->GetShaderInput(PortName)
		: Node->GetShaderOutput(PortName);
	return Property ? Property->GetTypeAsSdfType().GetSdfType() : pxr::SdfValueTypeName();
}

void ApplySetConnectableConnection(pxr::UsdStageRefPtr& Stage, const Wire::SetConnectableConnection* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;
	pxr::UsdShadeConnectableAPI Connectable(Prim);

	if (const auto* Connections = Ev->connections())
	{
		for (const Wire::Connection* Conn : *Connections)
		{
			if (!Conn) continue;
			bool bLocalIsInput = false, bSrcIsInput = false;
			FString LocalBase, SrcBase;
			if (!SplitQualifiedAttr(ToFString(Conn->local_attr()), bLocalIsInput, LocalBase)) continue;
			if (!SplitQualifiedAttr(ToFString(Conn->source_attr()), bSrcIsInput, SrcBase)) continue;

			pxr::UsdPrim SrcPrim = Stage->GetPrimAtPath(
				ToSdfPath(ToFString(Conn->source_prim())));
			if (!SrcPrim)
			{
				UE_LOG(LogUSDEventApplier, Warning,
					TEXT("SetConnectableConnection: missing source prim for %s"), *PrimPath);
				continue;
			}
			pxr::UsdShadeConnectableAPI SrcConn(SrcPrim);

			pxr::SdfValueTypeName SrcType;
			if (bSrcIsInput)
			{
				pxr::UsdShadeInput P = SrcConn.GetInput(ToToken(SrcBase));
				if (!P)
				{
					pxr::SdfValueTypeName Type = ResolveShaderPortType(
						SrcPrim, ToToken(SrcBase), true);
					P = SrcConn.CreateInput(
						ToToken(SrcBase), Type ? Type : pxr::SdfValueTypeNames->Token);
				}
				SrcType = P.GetTypeName();
			}
			else
			{
				pxr::UsdShadeOutput P = SrcConn.GetOutput(ToToken(SrcBase));
				if (!P)
				{
					pxr::SdfValueTypeName Type = ResolveShaderPortType(
						SrcPrim, ToToken(SrcBase), false);
					P = SrcConn.CreateOutput(
						ToToken(SrcBase), Type ? Type : pxr::SdfValueTypeNames->Token);
				}
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
				if (!Local)
				{
					pxr::SdfValueTypeName Type = ResolveShaderPortType(
						Prim, ToToken(LocalBase), true);
					Local = Connectable.CreateInput(
						ToToken(LocalBase), Type ? Type : SrcType);
				}
				Local.ConnectToSource(SourceInfo);
			}
			else
			{
				pxr::UsdShadeOutput Local = Connectable.GetOutput(ToToken(LocalBase));
				if (!Local)
				{
					pxr::SdfValueTypeName Type = ResolveShaderPortType(
						Prim, ToToken(LocalBase), false);
					Local = Connectable.CreateOutput(
						ToToken(LocalBase), Type ? Type : SrcType);
				}
				Local.ConnectToSource(SourceInfo);
			}
		}
	}

	if (const auto* Disconnections = Ev->disconnections())
	{
		for (const ::flatbuffers::String* Qualified : *Disconnections)
		{
			bool bIsInput = false;
			FString Base;
			if (!Qualified || !SplitQualifiedAttr(ToFString(Qualified), bIsInput, Base))
			{
				continue;
			}
			if (bIsInput)
			{
				if (pxr::UsdShadeInput P = Connectable.GetInput(ToToken(Base)))
				{
					P.DisconnectSource();
				}
			}
			else
			{
				if (pxr::UsdShadeOutput P = Connectable.GetOutput(ToToken(Base)))
				{
					P.DisconnectSource();
				}
			}
		}
	}
}

// ---- SetGprimAttrs ----------------------------------------------------------

// Build a VtValue for an AttrValue payload, coerced to the attribute's
// schema-defined type. Same contract as DecodeConnectableValue; the strided
// float-array cases cover the bulk mesh path (points, normals, UVs).
bool DecodeGprimAttrValue(const Wire::AttrValue* Val, const std::string& Type, pxr::VtValue& Out)
{
	switch (Val->value_type())
	{
	case Wire::AttrValueType::ScalarFloat:
		Out = CoerceNumericScalar(Val->scalar_float(), Type);
		return true;
	case Wire::AttrValueType::ScalarInt:
		Out = CoerceNumericScalar(Val->scalar_int(), Type);
		return true;
	case Wire::AttrValueType::ScalarBool:
		Out = CoerceNumericScalar(Val->scalar_bool() ? 1.0 : 0.0, Type);
		return true;
	case Wire::AttrValueType::ScalarString:
	case Wire::AttrValueType::StringVal:
		Out = CoerceString(
			Val->scalar_string() ? Val->scalar_string()->str() : std::string(), Type);
		return true;
	case Wire::AttrValueType::FloatArray:
	{
		if (TypeIs(Type, {"float3[]", "vector3f[]", "normal3f[]", "point3f[]", "color3f[]"}))
		{
			Out = VtArrayFromWire<pxr::VtVec3fArray>(Val->float_array());
			return true;
		}
		if (TypeIs(Type, {"float2[]", "texCoord2f[]"}))
		{
			Out = VtArrayFromWire<pxr::VtVec2fArray>(Val->float_array());
			return true;
		}
		if (Type == "float[]")
		{
			Out = VtArrayFromWire<pxr::VtFloatArray>(Val->float_array());
			return true;
		}
		return FloatsToValue(ToFloatArray(Val->float_array()), Type, Out);
	}
	case Wire::AttrValueType::IntArray:
		// gprim attrs carry no declared type on the wire, so the prim's schema
		// type drives; int payloads for float-typed schema attrs (a
		// hand-authored "radius": 1) convert per element.
		if (Type == "int[]")
		{
			Out = VtArrayFromWire<pxr::VtIntArray, int32_t>(Val->int_array());
			return true;
		}
		return FloatsToValue(FloatsFromIntWire(Val->int_array()), Type, Out);
	default: // None / NestedList (JSON fallback for exotic types)
		return false;
	}
}

void ApplySetGprimAttrs(pxr::UsdStageRefPtr& Stage, const Wire::SetGprimAttrs* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;

	const pxr::UsdTimeCode Time = ToTimeCode(Ev->time());

	TMap<FString, TPair<FString, FString>> PrimvarMeta; // name -> (type, interpolation)
	if (const auto* Metas = Ev->primvar_meta())
	{
		for (const Wire::PrimvarMeta* Meta : *Metas)
		{
			if (!Meta) continue;
			PrimvarMeta.Add(
				ToFString(Meta->attr_name()),
				TPair<FString, FString>(
					ToFString(Meta->type_name()),
					ToFString(Meta->interpolation())));
		}
	}

	pxr::UsdGeomPrimvarsAPI PvApi(Prim);
	if (const auto* Attrs = Ev->attrs())
	{
		for (const Wire::NamedAttr* Entry : *Attrs)
		{
			if (!Entry) continue;
			const FString Name = ToFString(Entry->name());
			const Wire::AttrValue* Val = Entry->value();
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
	if (const auto* Interps = Ev->attr_interp())
	{
		for (const Wire::AttrInterp* Entry : *Interps)
		{
			if (!Entry) continue;
			pxr::UsdAttribute Attr =
				Prim.GetAttribute(ToToken(ToFString(Entry->attr_name())));
			const FString Interp = ToFString(Entry->interpolation());
			if (Attr && !Interp.IsEmpty())
			{
				Attr.SetMetadata(pxr::TfToken("interpolation"), ToToken(Interp));
			}
		}
	}
}

// ---- SetInstanceable / SetPointInstancer ------------------------------------

void ApplySetInstanceable(pxr::UsdStageRefPtr& Stage, const Wire::SetInstanceable* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim)
	{
		Prim = Stage->DefinePrim(ToSdfPath(PrimPath), pxr::TfToken());
		if (!Prim) return;
	}
	Prim.SetInstanceable(Ev->instanceable());
}

void ApplySetPointInstancer(pxr::UsdStageRefPtr& Stage, const Wire::SetPointInstancer* Ev)
{
	const FString PrimPath = ToFString(Ev->prim());
	if (PrimPath.IsEmpty()) return;
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(ToSdfPath(PrimPath));
	if (!Prim) return;
	pxr::UsdGeomPointInstancer Instancer(Prim);
	if (!Instancer) return;

	// Field bits follow POINT_INSTANCER_FIELDS order in protocol_constants.py.
	const uint32 Fields = Ev->fields();
	const pxr::UsdTimeCode Time = ToTimeCode(Ev->time());

	if (Fields & 1) // prototypes
	{
		pxr::SdfPathVector Targets;
		if (const auto* Prototypes = Ev->prototypes())
		{
			for (const ::flatbuffers::String* Proto : *Prototypes)
			{
				if (Proto) { Targets.push_back(pxr::SdfPath(Proto->str())); }
			}
		}
		Instancer.CreatePrototypesRel().SetTargets(Targets);
	}
	if (Fields & 2) // proto_indices
	{
		Instancer.CreateProtoIndicesAttr().Set(
			VtArrayFromWire<pxr::VtIntArray, int32_t>(Ev->proto_indices()), Time);
	}
	if (Fields & 8) // orientations: authored to orientationsf, wins resolution
	{
		Instancer.CreateOrientationsfAttr().Set(
			QuatArrayFromWire(Ev->orientations()), Time);
	}

	struct FVec3Field
	{
		uint32 Bit;
		const ::flatbuffers::Vector<float>* Vec;
		pxr::UsdAttribute (pxr::UsdGeomPointInstancer::*CreateAttr)(const pxr::VtValue&, bool) const;
	};
	const FVec3Field Vec3Fields[] =
	{
		{ 4,   Ev->positions(),          &pxr::UsdGeomPointInstancer::CreatePositionsAttr },
		{ 16,  Ev->scales(),             &pxr::UsdGeomPointInstancer::CreateScalesAttr },
		{ 32,  Ev->velocities(),         &pxr::UsdGeomPointInstancer::CreateVelocitiesAttr },
		{ 64,  Ev->accelerations(),      &pxr::UsdGeomPointInstancer::CreateAccelerationsAttr },
		{ 128, Ev->angular_velocities(), &pxr::UsdGeomPointInstancer::CreateAngularVelocitiesAttr },
	};
	for (const FVec3Field& F : Vec3Fields)
	{
		if (Fields & F.Bit)
		{
			(Instancer.*F.CreateAttr)(pxr::VtValue(), false).Set(
				VtArrayFromWire<pxr::VtVec3fArray>(F.Vec), Time);
		}
	}

	if (Fields & 256) // ids
	{
		Instancer.CreateIdsAttr().Set(
			VtArrayFromWire<pxr::VtInt64Array, int64_t>(Ev->ids()), Time);
	}
	if (Fields & 512) // invisible_ids
	{
		Instancer.CreateInvisibleIdsAttr().Set(
			VtArrayFromWire<pxr::VtInt64Array, int64_t>(Ev->invisible_ids()), Time);
	}
	if (Fields & 1024) // inactive_ids: prim metadata as an explicit list op
	{
		const pxr::VtInt64Array Ids =
			VtArrayFromWire<pxr::VtInt64Array, int64_t>(Ev->inactive_ids());
		pxr::SdfInt64ListOp ListOp;
		ListOp.SetExplicitItems(std::vector<int64_t>(Ids.cbegin(), Ids.cend()));
		Prim.SetMetadata(pxr::TfToken("inactiveIds"), ListOp);
	}
}

// ---- SetSdfSpecFields -------------------------------------------------------------

void ApplySetSdfSpecFields(pxr::UsdStageRefPtr& Stage, const Wire::SetSdfSpecFields* Ev)
{
	if (!Ev || !Ev->prim() || !Ev->spec_path()) return;
	const pxr::SdfPath EventPath(Ev->spec_path()->str());
	if (!EventPath.IsAbsolutePath()) return;
	const Wire::SdfSpecKind Kind = Ev->spec_kind();
	auto ExpectedSpecType = [](Wire::SdfSpecKind SpecKind)
	{
		switch (SpecKind)
		{
		case Wire::SdfSpecKind::Layer:        return pxr::SdfSpecTypePseudoRoot;
		case Wire::SdfSpecKind::Prim:         return pxr::SdfSpecTypePrim;
		case Wire::SdfSpecKind::Attribute:    return pxr::SdfSpecTypeAttribute;
		case Wire::SdfSpecKind::Relationship: return pxr::SdfSpecTypeRelationship;
		case Wire::SdfSpecKind::VariantSet:   return pxr::SdfSpecTypeVariantSet;
		case Wire::SdfSpecKind::Variant:      return pxr::SdfSpecTypeVariant;
		}
		return pxr::SdfSpecTypeUnknown;
	};
	const pxr::SdfSpecType ExpectedType = ExpectedSpecType(Kind);
	const auto VariantSelection = EventPath.GetVariantSelection();
	const bool IsValidPath =
		(Kind == Wire::SdfSpecKind::Layer && EventPath == pxr::SdfPath::AbsoluteRootPath()) ||
		(Kind == Wire::SdfSpecKind::Prim && EventPath.IsPrimPath()) ||
		((Kind == Wire::SdfSpecKind::Attribute || Kind == Wire::SdfSpecKind::Relationship) &&
			EventPath.IsPropertyPath()) ||
		(Kind == Wire::SdfSpecKind::VariantSet && EventPath.IsPrimVariantSelectionPath() &&
			VariantSelection.second.empty()) ||
		(Kind == Wire::SdfSpecKind::Variant && EventPath.IsPrimVariantSelectionPath() &&
			!VariantSelection.second.empty());
	if (!IsValidPath || ExpectedType == pxr::SdfSpecTypeUnknown)
	{
		UE_LOG(LogUSDEventApplier, Warning, TEXT("Invalid Sdf spec kind or path: %s"),
			UTF8_TO_TCHAR(Ev->spec_path()->c_str()));
		return;
	}
	const pxr::SdfPath ExpectedPrimPath = Kind == Wire::SdfSpecKind::Layer
		? pxr::SdfPath::AbsoluteRootPath()
		: EventPath.GetPrimPath().StripAllVariantSelections();
	if (Ev->prim()->str() != ExpectedPrimPath.GetString())
	{
		UE_LOG(LogUSDEventApplier, Warning, TEXT("Sdf spec routing prim does not match %s"),
			UTF8_TO_TCHAR(Ev->spec_path()->c_str()));
		return;
	}

	const pxr::SdfLayerHandle TargetLayer = Stage->GetEditTarget().GetLayer();
	if (!TargetLayer) return;

	auto RemoveSpec = [TargetLayer, ExpectedSpecType](
		const pxr::SdfPath& Path,
		Wire::SdfSpecKind SpecKind)
	{
		if (SpecKind == Wire::SdfSpecKind::Layer) return false;
		if (!TargetLayer->HasSpec(Path)) return true;
		if (TargetLayer->GetSpecType(Path) != ExpectedSpecType(SpecKind)) return true;
		if (SpecKind == Wire::SdfSpecKind::Variant)
		{
			const pxr::SdfVariantSpecHandle Variant = pxr::TfDynamic_cast<pxr::SdfVariantSpecHandle>(
				TargetLayer->GetObjectAtPath(Path));
			if (Variant && Variant->GetOwner()) Variant->GetOwner()->RemoveVariant(Variant);
			return true;
		}
		if (SpecKind == Wire::SdfSpecKind::VariantSet)
		{
			const pxr::SdfVariantSetSpecHandle VariantSet =
				pxr::TfDynamic_cast<pxr::SdfVariantSetSpecHandle>(
					TargetLayer->GetObjectAtPath(Path));
			if (!VariantSet) return true;
			const pxr::SdfSpecHandle Owner = VariantSet->GetOwner();
			if (const pxr::SdfPrimSpecHandle Prim =
				pxr::TfDynamic_cast<pxr::SdfPrimSpecHandle>(Owner))
			{
				Prim->RemoveVariantSet(VariantSet->GetName());
			}
			else if (const pxr::SdfVariantSpecHandle Variant =
				pxr::TfDynamic_cast<pxr::SdfVariantSpecHandle>(Owner))
			{
				Variant->GetVariantSets().erase(VariantSet->GetName());
			}
			return true;
		}
		pxr::SdfBatchNamespaceEdit Edits;
		Edits.Add(pxr::SdfNamespaceEdit::Remove(Path));
		return TargetLayer->Apply(Edits);
	};

	if (Kind == Wire::SdfSpecKind::Layer && Ev->fields())
	{
		for (const ::flatbuffers::String* FieldString : *Ev->fields())
		{
			if (FieldString &&
				(FieldString->str() == "subLayers" ||
				 FieldString->str() == "subLayerOffsets"))
			{
				UE_LOG(LogUSDEventApplier, Warning,
					TEXT("set_sdf_spec_fields cannot author sublayer topology"));
				return;
			}
		}
	}
	if (Ev->removed())
	{
		if (!RemoveSpec(EventPath, Kind))
		{
			UE_LOG(LogUSDEventApplier, Warning, TEXT("Could not remove Sdf spec %s"),
				UTF8_TO_TCHAR(Ev->spec_path()->c_str()));
		}
		return;
	}

	if (!Ev->fragment() || !Ev->fields()) return;
	const pxr::SdfLayerRefPtr Incoming = pxr::SdfLayer::CreateAnonymous(
		"openusdconnect-sdf-spec-delta");
	if (!Incoming->ImportFromString(Ev->fragment()->str()) || !Incoming->HasSpec(EventPath))
	{
		UE_LOG(LogUSDEventApplier, Warning, TEXT("Invalid set_sdf_spec_fields fragment for %s"),
			UTF8_TO_TCHAR(Ev->spec_path()->c_str()));
		return;
	}
	if (Incoming->GetSpecType(EventPath) != ExpectedType)
	{
		UE_LOG(LogUSDEventApplier, Warning, TEXT("Sdf fragment kind does not match %s"),
			UTF8_TO_TCHAR(Ev->spec_path()->c_str()));
		return;
	}

	if (TargetLayer->HasSpec(EventPath) &&
		TargetLayer->GetSpecType(EventPath) != ExpectedType)
	{
		if (Kind != Wire::SdfSpecKind::Attribute && Kind != Wire::SdfSpecKind::Relationship)
		{
			UE_LOG(LogUSDEventApplier, Warning, TEXT("Could not replace Sdf spec %s"),
				UTF8_TO_TCHAR(Ev->spec_path()->c_str()));
			return;
		}
		pxr::SdfBatchNamespaceEdit Edits;
		Edits.Add(pxr::SdfNamespaceEdit::Remove(EventPath));
		if (!TargetLayer->Apply(Edits))
		{
			UE_LOG(LogUSDEventApplier, Warning, TEXT("Could not replace Sdf property spec %s"),
				UTF8_TO_TCHAR(Ev->spec_path()->c_str()));
			return;
		}
	}

	const bool WasMissing = !TargetLayer->HasSpec(EventPath);
	if (WasMissing)
	{
		if (Kind == Wire::SdfSpecKind::Attribute || Kind == Wire::SdfSpecKind::Relationship)
		{
			static const pxr::TfToken CustomField("custom");
			static const pxr::TfToken TypeNameField("typeName");
			static const pxr::TfToken VariabilityField("variability");
			auto IsSelectedField = [Ev, Kind](const pxr::TfToken& Field)
			{
				if (Field == CustomField || Field == VariabilityField ||
					(Kind == Wire::SdfSpecKind::Attribute && Field == TypeNameField))
				{
					return true;
				}
				for (const ::flatbuffers::String* FieldString : *Ev->fields())
				{
					if (FieldString && Field == pxr::TfToken(FieldString->str())) return true;
				}
				return false;
			};
			for (const pxr::TfToken& Field : Incoming->ListFields(EventPath))
			{
				if (!IsSelectedField(Field)) Incoming->EraseField(EventPath, Field);
			}
			pxr::SdfCreatePrimInLayer(TargetLayer, EventPath.GetParentPath());
			if (!pxr::SdfCopySpec(Incoming, EventPath, TargetLayer, EventPath))
			{
				UE_LOG(LogUSDEventApplier, Warning, TEXT("Could not create Sdf spec %s"),
					UTF8_TO_TCHAR(Ev->spec_path()->c_str()));
			}
			return;
		}
		if (Kind == Wire::SdfSpecKind::Prim)
		{
			pxr::SdfCreatePrimInLayer(TargetLayer, EventPath);
		}
		else if (Kind == Wire::SdfSpecKind::Variant)
		{
			const auto Selection = EventPath.GetVariantSelection();
			pxr::SdfCreateVariantInLayer(
				TargetLayer,
				EventPath.GetPrimPath(),
				Selection.first,
				Selection.second);
		}
		else if (Kind == Wire::SdfSpecKind::VariantSet)
		{
			const auto Selection = EventPath.GetVariantSelection();
			const pxr::SdfPrimSpecHandle Owner = pxr::SdfCreatePrimInLayer(
				TargetLayer,
				EventPath.GetPrimPath());
			if (Owner)
			{
				pxr::SdfVariantSetSpec::New(Owner, Selection.first);
			}
		}
	}

	if (WasMissing &&
		(Kind == Wire::SdfSpecKind::Prim || Kind == Wire::SdfSpecKind::Variant))
	{
		static const pxr::TfToken SpecifierField("specifier");
		pxr::VtValue Value;
		if (Incoming->HasField(EventPath, SpecifierField, &Value))
		{
			TargetLayer->SetField(EventPath, SpecifierField, Value);
		}
	}
	for (const ::flatbuffers::String* FieldString : *Ev->fields())
	{
		if (!FieldString) continue;
		const pxr::TfToken Field(FieldString->str());
		pxr::VtValue Value;
		if (Kind == Wire::SdfSpecKind::Layer && Field == pxr::SdfFieldKeys->CustomLayerData)
		{
			static const std::string ReservedKey("openusdconnect");
			pxr::VtDictionary Data;
			if (Incoming->HasField(EventPath, Field, &Value) &&
				Value.IsHolding<pxr::VtDictionary>())
			{
				Data = Value.UncheckedGet<pxr::VtDictionary>();
			}
			Data.erase(ReservedKey);
			pxr::VtValue CurrentValue;
			if (TargetLayer->HasField(EventPath, Field, &CurrentValue) &&
				CurrentValue.IsHolding<pxr::VtDictionary>())
			{
				const pxr::VtDictionary& Current =
					CurrentValue.UncheckedGet<pxr::VtDictionary>();
				const auto Reserved = Current.find(ReservedKey);
				if (Reserved != Current.end()) Data[ReservedKey] = Reserved->second;
			}
			if (Data.empty()) TargetLayer->EraseField(EventPath, Field);
			else TargetLayer->SetField(EventPath, Field, pxr::VtValue(Data));
			continue;
		}
		if (Incoming->HasField(EventPath, Field, &Value))
		{
			TargetLayer->SetField(EventPath, Field, Value);
		}
		else
		{
			TargetLayer->EraseField(EventPath, Field);
		}
	}
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------
void DispatchEvent(pxr::UsdStageRefPtr& Stage, const Wire::EventWrapper* Wrapper)
{
	switch (Wrapper->event_type())
	{
	case Wire::EventPayload::EnsurePrim:           ApplyEnsurePrim(Stage, Wrapper->event_as_EnsurePrim());                     break;
	case Wire::EventPayload::EnsureXformOps:       ApplyEnsureXformOps(Stage, Wrapper->event_as_EnsureXformOps());             break;
	case Wire::EventPayload::SetXformTrs:          ApplySetXformTrs(Stage, Wrapper->event_as_SetXformTrs());                   break;
	case Wire::EventPayload::DeletePrim:           ApplyDeletePrim(Stage, Wrapper->event_as_DeletePrim());                     break;
	case Wire::EventPayload::DeactivatePrim:       ApplyDeactivatePrim(Stage, Wrapper->event_as_DeactivatePrim());             break;
	case Wire::EventPayload::RenamePrim:           ApplyRenamePrim(Stage, Wrapper->event_as_RenamePrim());                     break;
	case Wire::EventPayload::SetVisibility:        ApplySetVisibility(Stage, Wrapper->event_as_SetVisibility());               break;
	case Wire::EventPayload::SetGprimAttrs:        ApplySetGprimAttrs(Stage, Wrapper->event_as_SetGprimAttrs());               break;
	case Wire::EventPayload::SetInstanceable:      ApplySetInstanceable(Stage, Wrapper->event_as_SetInstanceable());           break;
	case Wire::EventPayload::SetPointInstancer:    ApplySetPointInstancer(Stage, Wrapper->event_as_SetPointInstancer());       break;
	case Wire::EventPayload::SetSdfSpecFields:     ApplySetSdfSpecFields(Stage, Wrapper->event_as_SetSdfSpecFields());         break;
	case Wire::EventPayload::SetReference:         ApplySetReference(Stage, Wrapper->event_as_SetReference());                 break;
	case Wire::EventPayload::SetPayload:           ApplySetPayload(Stage, Wrapper->event_as_SetPayload());                     break;
	case Wire::EventPayload::LoadPayload:          ApplyLoadPayload(Stage, Wrapper->event_as_LoadPayload());                   break;
	case Wire::EventPayload::UnloadPayload:        ApplyUnloadPayload(Stage, Wrapper->event_as_UnloadPayload());               break;
	case Wire::EventPayload::SetVariantSelections: ApplySetVariantSelections(Stage, Wrapper->event_as_SetVariantSelections()); break;
	case Wire::EventPayload::SetMaterialBinding:   ApplySetMaterialBinding(Stage, Wrapper->event_as_SetMaterialBinding());     break;
	case Wire::EventPayload::SetConnectableInput:      ApplySetConnectableInput(Stage, Wrapper->event_as_SetConnectableInput());           break;
	case Wire::EventPayload::SetConnectableConnection: ApplySetConnectableConnection(Stage, Wrapper->event_as_SetConnectableConnection()); break;
	case Wire::EventPayload::SetStageMetadata:     ApplySetStageMetadata(Stage, Wrapper->event_as_SetStageMetadata());         break;
	default:
		UE_LOG(LogUSDEventApplier, Warning, TEXT("Unknown event kind: %u"),
			static_cast<uint8>(Wrapper->event_type()));
		break;
	}
}

// The event's target prim path; empty for stage-scoped events.
FString GetEventPrim(const Wire::EventWrapper* Wrapper)
{
	switch (Wrapper->event_type())
	{
	case Wire::EventPayload::EnsurePrim:               return ToFString(Wrapper->event_as_EnsurePrim()->prim());
	case Wire::EventPayload::EnsureXformOps:           return ToFString(Wrapper->event_as_EnsureXformOps()->prim());
	case Wire::EventPayload::SetXformTrs:              return ToFString(Wrapper->event_as_SetXformTrs()->prim());
	case Wire::EventPayload::DeletePrim:               return ToFString(Wrapper->event_as_DeletePrim()->prim());
	case Wire::EventPayload::DeactivatePrim:           return ToFString(Wrapper->event_as_DeactivatePrim()->prim());
	case Wire::EventPayload::RenamePrim:               return ToFString(Wrapper->event_as_RenamePrim()->prim());
	case Wire::EventPayload::SetVisibility:            return ToFString(Wrapper->event_as_SetVisibility()->prim());
	case Wire::EventPayload::SetGprimAttrs:            return ToFString(Wrapper->event_as_SetGprimAttrs()->prim());
	case Wire::EventPayload::SetInstanceable:          return ToFString(Wrapper->event_as_SetInstanceable()->prim());
	case Wire::EventPayload::SetPointInstancer:        return ToFString(Wrapper->event_as_SetPointInstancer()->prim());
	case Wire::EventPayload::SetSdfSpecFields:         return ToFString(Wrapper->event_as_SetSdfSpecFields()->prim());
	case Wire::EventPayload::SetReference:             return ToFString(Wrapper->event_as_SetReference()->prim());
	case Wire::EventPayload::SetPayload:               return ToFString(Wrapper->event_as_SetPayload()->prim());
	case Wire::EventPayload::LoadPayload:              return ToFString(Wrapper->event_as_LoadPayload()->prim());
	case Wire::EventPayload::UnloadPayload:            return ToFString(Wrapper->event_as_UnloadPayload()->prim());
	case Wire::EventPayload::SetVariantSelections:     return ToFString(Wrapper->event_as_SetVariantSelections()->prim());
	case Wire::EventPayload::SetMaterialBinding:       return ToFString(Wrapper->event_as_SetMaterialBinding()->prim());
	case Wire::EventPayload::SetConnectableInput:      return ToFString(Wrapper->event_as_SetConnectableInput()->prim());
	case Wire::EventPayload::SetConnectableConnection: return ToFString(Wrapper->event_as_SetConnectableConnection()->prim());
	default:                                           return {};
	}
}

} // namespace
#endif // USE_USD_SDK

namespace
{

// Value writes on existing prims batch inside an SdfChangeBlock; every other
// kind mutates composition (DefinePrim, arcs, variants, schema application)
// and must see the stage recompose as it goes, so it runs outside. Mirrors
// the core applier's STRUCTURAL_EVENT_KINDS split.
bool UsesChangeBlock(OpenUSDConnect::EventPayload EventKind)
{
	switch (EventKind)
	{
	case OpenUSDConnect::EventPayload::SetXformTrs:
	case OpenUSDConnect::EventPayload::SetVisibility:
	case OpenUSDConnect::EventPayload::SetGprimAttrs:
	case OpenUSDConnect::EventPayload::SetPointInstancer:
	case OpenUSDConnect::EventPayload::DeactivatePrim:
		return true;
	default:
		return false;
	}
}

// BroadcastEvent's EventWrapper from a raw frame; nullptr on malformed input.
const OpenUSDConnect::EventWrapper* GetFrameEventWrapper(const TArray<uint8>& RawFrame)
{
	const OpenUSDConnect::Envelope* Env = OUC::GetEnvelopeFromFrame(RawFrame);
	const OpenUSDConnect::BroadcastEvent* BcEvent =
		Env ? Env->payload_as_BroadcastEvent() : nullptr;
	return BcEvent ? BcEvent->event() : nullptr;
}

} // namespace

// ---------------------------------------------------------------------------
// FUSDEventApplier::FrameUsesChangeBlock
// ---------------------------------------------------------------------------
bool FUSDEventApplier::FrameUsesChangeBlock(const TArray<uint8>& RawFrame)
{
	if (RawFrame.Num() < 8) return false;
	const OpenUSDConnect::EventWrapper* Wrapper = GetFrameEventWrapper(RawFrame);
	return Wrapper && UsesChangeBlock(Wrapper->event_type());
}

// ---------------------------------------------------------------------------
// FUSDEventApplier::ApplyFrame
// ---------------------------------------------------------------------------
void FUSDEventApplier::ApplyFrame(const TArray<uint8>& RawFrame, AUsdStageActor* StageActor,
                                  FString* OutTouchedPrim, OpenUSDConnect::EventPayload* OutEventKind)
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

	const OpenUSDConnect::EventWrapper* Wrapper = GetFrameEventWrapper(RawFrame);
	if (!Wrapper) return;

	if (OutEventKind)
	{
		*OutEventKind = Wrapper->event_type();
	}
	if (OutTouchedPrim)
	{
		*OutTouchedPrim = GetEventPrim(Wrapper);
	}

	// Structural events must apply outside an SdfChangeBlock: recomposition is
	// deferred until the block closes, so UsdStage::DefinePrim cannot return
	// the newly defined prim and arc edits act on a stale composed view. Value
	// writes on existing prims are ChangeBlock-safe and batch into one
	// consolidated ObjectsChanged notice, which the stage actor's FUsdListener
	// turns into a single scene refresh.
	if (UsesChangeBlock(Wrapper->event_type()))
	{
		pxr::SdfChangeBlock ChangeBlock;
		DispatchEvent(PxrStage, Wrapper);
	}
	else
	{
		DispatchEvent(PxrStage, Wrapper);
	}
#else
	UE_LOG(LogUSDEventApplier, Warning,
		TEXT("USD SDK not available — cannot apply USD events"));
#endif
}
