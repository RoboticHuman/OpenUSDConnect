// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "USDStageBridge.h"

#include "Logging/LogMacros.h"
#include "USDStageActor.h"

#include <cstdint>

#if USE_USD_SDK
#include "USDIncludesStart.h"
#include "pxr/base/vt/dictionary.h"
#include "pxr/usd/sdf/assetPath.h"
#include "pxr/usd/sdf/changeBlock.h"
#include "pxr/usd/sdf/layer.h"
#include "pxr/usd/sdf/valueTypeName.h"
#include "pxr/usd/usd/prim.h"
#include "pxr/usd/usd/primRange.h"
#include "pxr/usd/usd/stage.h"
#include "pxr/usd/usd/timeCode.h"
#include "pxr/usd/usdGeom/imageable.h"
#include "pxr/usd/usdGeom/tokens.h"
#include "pxr/usd/usdGeom/xformable.h"
#include "pxr/usd/usdGeom/xformOp.h"
#include "pxr/usd/usdShade/connectableAPI.h"
#include "pxr/usd/usdShade/material.h"
#include "pxr/usd/usdShade/shader.h"
#include "pxr/base/gf/matrix4d.h"
#include "pxr/base/gf/quatd.h"
#include "pxr/base/gf/quatf.h"
#include "pxr/base/gf/transform.h"
#include "pxr/base/gf/vec2f.h"
#include "pxr/base/gf/vec3d.h"
#include "pxr/base/gf/vec3f.h"
#include "pxr/base/gf/vec4f.h"
#include "pxr/base/vt/array.h"
#include "pxr/base/vt/value.h"
#include "USDIncludesEnd.h"
#endif

DEFINE_LOG_CATEGORY_STATIC(LogUSDConnectStageBridge, Log, All);

#if USE_USD_SDK
namespace
{

pxr::UsdStageRefPtr GetStage(AUsdStageActor* StageActor)
{
	if (!StageActor || !IsValid(StageActor))
	{
		return {};
	}
	return static_cast<pxr::UsdStageRefPtr>(StageActor->GetOrOpenUsdStage());
}

bool TryGetLiveDictString(const pxr::VtDictionary& Dict, const char* Key, FString& Out)
{
	const auto It = Dict.find(Key);
	if (It == Dict.end())
	{
		return false;
	}

	const pxr::VtValue& Value = It->second;
	if (!Value.IsHolding<std::string>())
	{
		return false;
	}

	Out = UTF8_TO_TCHAR(Value.UncheckedGet<std::string>().c_str());
	return true;
}

bool TryGetLiveDictBool(const pxr::VtDictionary& Dict, const char* Key, bool& Out)
{
	const auto It = Dict.find(Key);
	if (It == Dict.end())
	{
		return false;
	}

	const pxr::VtValue& Value = It->second;
	if (!Value.IsHolding<bool>())
	{
		return false;
	}

	Out = Value.UncheckedGet<bool>();
	return true;
}

bool TryGetLiveDictInt(const pxr::VtDictionary& Dict, const char* Key, int32& Out)
{
	const auto It = Dict.find(Key);
	if (It == Dict.end())
	{
		return false;
	}

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

bool ConvertVtValueToWire(const pxr::VtValue& Value, FEmitConnectableValue& Out)
{
	using OpenUSDConnect::ConnectableInputValueType;

	if (Value.IsHolding<float>())
	{
		Out.ValueType = ConnectableInputValueType::ScalarFloat;
		Out.ScalarFloat = Value.UncheckedGet<float>();
	}
	else if (Value.IsHolding<double>())
	{
		Out.ValueType = ConnectableInputValueType::ScalarFloat;
		Out.ScalarFloat = static_cast<float>(Value.UncheckedGet<double>());
	}
	else if (Value.IsHolding<int>())
	{
		Out.ValueType = ConnectableInputValueType::ScalarInt;
		Out.ScalarInt = Value.UncheckedGet<int>();
	}
	else if (Value.IsHolding<bool>())
	{
		Out.ValueType = ConnectableInputValueType::ScalarBool;
		Out.bScalarBool = Value.UncheckedGet<bool>();
	}
	else if (Value.IsHolding<pxr::TfToken>())
	{
		Out.ValueType = ConnectableInputValueType::ScalarString;
		Out.ScalarString = UTF8_TO_TCHAR(Value.UncheckedGet<pxr::TfToken>().GetText());
	}
	else if (Value.IsHolding<std::string>())
	{
		Out.ValueType = ConnectableInputValueType::ScalarString;
		Out.ScalarString = UTF8_TO_TCHAR(Value.UncheckedGet<std::string>().c_str());
	}
	else if (Value.IsHolding<pxr::SdfAssetPath>())
	{
		Out.ValueType = ConnectableInputValueType::ScalarString;
		Out.ScalarString =
			UTF8_TO_TCHAR(Value.UncheckedGet<pxr::SdfAssetPath>().GetAssetPath().c_str());
	}
	else if (Value.IsHolding<pxr::GfVec2f>())
	{
		const pxr::GfVec2f V = Value.UncheckedGet<pxr::GfVec2f>();
		Out.ValueType = ConnectableInputValueType::FloatArray;
		Out.Floats = {V[0], V[1]};
	}
	else if (Value.IsHolding<pxr::GfVec3f>())
	{
		const pxr::GfVec3f V = Value.UncheckedGet<pxr::GfVec3f>();
		Out.ValueType = ConnectableInputValueType::FloatArray;
		Out.Floats = {V[0], V[1], V[2]};
	}
	else if (Value.IsHolding<pxr::GfVec4f>())
	{
		const pxr::GfVec4f V = Value.UncheckedGet<pxr::GfVec4f>();
		Out.ValueType = ConnectableInputValueType::FloatArray;
		Out.Floats = {V[0], V[1], V[2], V[3]};
	}
	else
	{
		return false;
	}

	return true;
}

} // namespace
#endif

bool FUSDStageBridge::ReadXformTrs(
	AUsdStageActor* StageActor,
	const FString& PrimPath,
	FEmitXformTrs& OutTrs,
	bool* bOutFromMatrixOp)
{
#if USE_USD_SDK
	pxr::UsdStageRefPtr Stage = GetStage(StageActor);
	if (!Stage)
	{
		return false;
	}

	pxr::UsdPrim Prim = Stage->GetPrimAtPath(pxr::SdfPath(TCHAR_TO_UTF8(*PrimPath)));
	if (!Prim)
	{
		UE_LOG(LogUSDConnectStageBridge, Verbose,
			TEXT("ReadXformTrs(%s): no prim at path"), *PrimPath);
		return false;
	}

	pxr::UsdGeomXformable Xformable(Prim);
	if (!Xformable)
	{
		UE_LOG(LogUSDConnectStageBridge, Verbose,
			TEXT("ReadXformTrs(%s): prim is not Xformable (typeName=%s)"),
			*PrimPath, UTF8_TO_TCHAR(Prim.GetTypeName().GetText()));
		return false;
	}

	bool bResetXformStack = false;
	std::vector<pxr::UsdGeomXformOp> Ops = Xformable.GetOrderedXformOps(&bResetXformStack);
	if (Ops.empty())
	{
		UE_LOG(LogUSDConnectStageBridge, Verbose,
			TEXT("ReadXformTrs(%s): Xformable has no ordered xform ops; nothing to emit"),
			*PrimPath);
		return false;
	}

	const pxr::UsdTimeCode Time = pxr::UsdTimeCode::Default();
	OutTrs.PrimPath = PrimPath;
	OutTrs.Fields = 0;

	for (pxr::UsdGeomXformOp& Op : Ops)
	{
		const auto OpType = Op.GetOpType();
		if (OpType == pxr::UsdGeomXformOp::TypeTranslate)
		{
			pxr::GfVec3d T(0, 0, 0);
			Op.Get(&T, Time);
			OutTrs.T[0] = static_cast<float>(T[0]);
			OutTrs.T[1] = static_cast<float>(T[1]);
			OutTrs.T[2] = static_cast<float>(T[2]);
			OutTrs.Fields |= 1;
		}
		else if (OpType == pxr::UsdGeomXformOp::TypeOrient)
		{
			pxr::GfQuatf R(1, 0, 0, 0);
			Op.Get(&R, Time);
			OutTrs.R[0] = R.GetReal();
			const pxr::GfVec3f Im = R.GetImaginary();
			OutTrs.R[1] = Im[0];
			OutTrs.R[2] = Im[1];
			OutTrs.R[3] = Im[2];
			OutTrs.Fields |= 2;
		}
		else if (OpType == pxr::UsdGeomXformOp::TypeScale)
		{
			pxr::GfVec3f S(1, 1, 1);
			Op.Get(&S, Time);
			OutTrs.S[0] = S[0];
			OutTrs.S[1] = S[1];
			OutTrs.S[2] = S[2];
			OutTrs.Fields |= 4;
		}
		else if (OpType == pxr::UsdGeomXformOp::TypeTransform)
		{
			pxr::GfMatrix4d Matrix(1.0);
			Op.Get(&Matrix, Time);
			const pxr::GfTransform Decomposed(Matrix);

			const pxr::GfVec3d T = Decomposed.GetTranslation();
			OutTrs.T[0] = static_cast<float>(T[0]);
			OutTrs.T[1] = static_cast<float>(T[1]);
			OutTrs.T[2] = static_cast<float>(T[2]);

			const pxr::GfQuatd R = Decomposed.GetRotation().GetQuat().GetNormalized();
			OutTrs.R[0] = static_cast<float>(R.GetReal());
			const pxr::GfVec3d Im = R.GetImaginary();
			OutTrs.R[1] = static_cast<float>(Im[0]);
			OutTrs.R[2] = static_cast<float>(Im[1]);
			OutTrs.R[3] = static_cast<float>(Im[2]);

			const pxr::GfVec3d S = Decomposed.GetScale();
			OutTrs.S[0] = static_cast<float>(S[0]);
			OutTrs.S[1] = static_cast<float>(S[1]);
			OutTrs.S[2] = static_cast<float>(S[2]);

			OutTrs.Fields |= 7;
			if (bOutFromMatrixOp)
			{
				*bOutFromMatrixOp = true;
			}
		}
	}

	return OutTrs.Fields != 0;
#else
	return false;
#endif
}

void FUSDStageBridge::RestoreCanonicalXformOps(
	AUsdStageActor* StageActor,
	const FString& PrimPath,
	const FEmitXformTrs& Trs)
{
#if USE_USD_SDK
	pxr::UsdStageRefPtr Stage = GetStage(StageActor);
	if (!Stage)
	{
		return;
	}

	pxr::UsdPrim Prim = Stage->GetPrimAtPath(pxr::SdfPath(TCHAR_TO_UTF8(*PrimPath)));
	if (!Prim)
	{
		return;
	}

	const pxr::UsdAttribute Translate = Prim.GetAttribute(pxr::TfToken("xformOp:translate"));
	const pxr::UsdAttribute Orient = Prim.GetAttribute(pxr::TfToken("xformOp:orient"));
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

	const pxr::VtTokenArray Order{
		pxr::TfToken("xformOp:translate"),
		pxr::TfToken("xformOp:orient"),
		pxr::TfToken("xformOp:scale"),
	};
	pxr::UsdGeomXformable(Prim).CreateXformOpOrderAttr().Set(Order);
#endif
}

bool FUSDStageBridge::ReadConnectableInputs(
	AUsdStageActor* StageActor,
	const FString& PrimPath,
	const TSet<FString>& InputAttrNames,
	FEmitConnectableInput& Out)
{
#if USE_USD_SDK
	pxr::UsdStageRefPtr Stage = GetStage(StageActor);
	if (!Stage)
	{
		return false;
	}

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
		Wire.Name = AttrName.RightChop(7);
		Wire.TypeName = UTF8_TO_TCHAR(Attr.GetTypeName().GetAsToken().GetText());
		if (!ConvertVtValueToWire(Value, Wire))
		{
			UE_LOG(LogUSDConnectStageBridge, Verbose,
				TEXT("ReadConnectableInputs(%s): skipping %s; unhandled value type %s"),
				*PrimPath, *AttrName, UTF8_TO_TCHAR(Value.GetTypeName().c_str()));
			continue;
		}
		Out.Inputs.Add(MoveTemp(Wire));
	}

	return Out.Inputs.Num() > 0;
#else
	return false;
#endif
}

bool FUSDStageBridge::ReadVisibility(
	AUsdStageActor* StageActor,
	const FString& PrimPath,
	FEmitVisibility& OutVisibility)
{
#if USE_USD_SDK
	pxr::UsdStageRefPtr Stage = GetStage(StageActor);
	if (!Stage)
	{
		return false;
	}

	pxr::UsdPrim Prim = Stage->GetPrimAtPath(pxr::SdfPath(TCHAR_TO_UTF8(*PrimPath)));
	if (!Prim)
	{
		UE_LOG(LogUSDConnectStageBridge, Verbose,
			TEXT("ReadVisibility(%s): no prim at path"), *PrimPath);
		return false;
	}

	pxr::UsdGeomImageable Imageable(Prim);
	if (!Imageable)
	{
		UE_LOG(LogUSDConnectStageBridge, Verbose,
			TEXT("ReadVisibility(%s): prim is not Imageable"), *PrimPath);
		return false;
	}

	pxr::TfToken Visibility;
	Imageable.GetVisibilityAttr().Get(&Visibility, pxr::UsdTimeCode::Default());
	OutVisibility.PrimPath = PrimPath;
	OutVisibility.bVisible = Visibility != pxr::UsdGeomTokens->invisible;
	return true;
#else
	return false;
#endif
}

bool FUSDStageBridge::ReadLiveOpenMetadata(
	AUsdStageActor* StageActor,
	FUSDLiveOpenMetadata& OutMetadata)
{
#if USE_USD_SDK
	pxr::UsdStageRefPtr Stage = GetStage(StageActor);
	if (!Stage)
	{
		return false;
	}

	pxr::SdfLayerHandle RootLayer = Stage->GetRootLayer();
	if (!RootLayer)
	{
		return false;
	}

	const pxr::VtDictionary CustomData = RootLayer->GetCustomLayerData();
	const auto MetaIt = CustomData.find("openusdconnect");
	if (MetaIt == CustomData.end())
	{
		return false;
	}

	const pxr::VtValue& MetaValue = MetaIt->second;
	if (!MetaValue.IsHolding<pxr::VtDictionary>())
	{
		return false;
	}

	const pxr::VtDictionary& Meta = MetaValue.UncheckedGet<pxr::VtDictionary>();
	bool bLive = false;
	if (!TryGetLiveDictBool(Meta, "live", bLive) || !bLive)
	{
		return false;
	}

	FUSDLiveOpenMetadata Candidate;
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
		UE_LOG(LogUSDConnectStageBridge, Warning,
			TEXT("OpenUSDConnect live metadata is present but has no valid host/port"));
		return false;
	}

	OutMetadata = MoveTemp(Candidate);
	return true;
#else
	return false;
#endif
}

bool FUSDStageBridge::ReadMaterialXMaterials(
	AUsdStageActor* StageActor,
	FString& OutRootLayerIdentifier,
	TArray<FString>& OutMaterialPrimPaths)
{
	OutRootLayerIdentifier.Reset();
	OutMaterialPrimPaths.Reset();

#if USE_USD_SDK
	pxr::UsdStageRefPtr Stage = GetStage(StageActor);
	if (!Stage)
	{
		return false;
	}

	pxr::SdfLayerHandle RootLayer = Stage->GetRootLayer();
	if (!RootLayer)
	{
		return false;
	}

	OutRootLayerIdentifier = UTF8_TO_TCHAR(RootLayer->GetIdentifier().c_str());
	for (const pxr::UsdPrim& Prim : Stage->Traverse())
	{
		const pxr::UsdShadeMaterial Material(Prim);
		if (Material && Material.GetSurfaceOutput(pxr::TfToken("mtlx")))
		{
			OutMaterialPrimPaths.Add(UTF8_TO_TCHAR(Prim.GetPath().GetText()));
		}
	}

	return true;
#else
	return false;
#endif
}
