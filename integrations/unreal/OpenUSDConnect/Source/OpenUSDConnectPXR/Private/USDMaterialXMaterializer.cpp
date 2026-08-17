// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "USDMaterialXMaterializer.h"

#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Misc/SecureHash.h"
#include "HAL/FileManager.h"
#include "Logging/LogMacros.h"
#include "Materials/MaterialInstance.h"
#include "Materials/MaterialInstanceConstant.h"
#include "MaterialShared.h"

#include "USDStageActor.h"
#include "USDPrimLinkCacheObject.h"
#include "Objects/USDPrimLinkCache.h"
#include "USDShadeConversion.h"
#include "UsdWrappers/SdfPath.h"

#if USE_USD_SDK
#include "USDIncludesStart.h"
#include "pxr/usd/usd/editContext.h"
#include "pxr/usd/usd/prim.h"
#include "pxr/usd/usd/primRange.h"
#include "pxr/usd/usd/stage.h"
#include "pxr/usd/usdShade/connectableAPI.h"
#include "pxr/usd/usdShade/material.h"
#include "pxr/usd/usdShade/nodeGraph.h"
#include "pxr/usd/usdShade/shader.h"
#include "pxr/usd/sdf/assetPath.h"
#include "pxr/usd/sdf/layer.h"
#include "pxr/usd/sdf/primSpec.h"
#include "pxr/usd/sdf/reference.h"
#include "pxr/base/gf/vec2f.h"
#include "pxr/base/gf/vec3f.h"
#include "pxr/base/gf/vec4f.h"
#include "pxr/base/vt/value.h"
#include "USDIncludesEnd.h"
#endif

DEFINE_LOG_CATEGORY_STATIC(LogUSDMaterializer, Log, All);

#if USE_USD_SDK

namespace
{
	// mtlx nodedef identifiers end in the node's output type: ND_<category>_<type>.
	// Mirrors the type-suffix whitelist and USD->mtlx type map of the Python
	// exporter so both produce equivalent documents.
	static bool ParseNodeDef(const FString& InfoId, FString& OutCategory, FString& OutType)
	{
		static const TSet<FString> TypeSuffixes = {
			TEXT("surfaceshader"), TEXT("displacementshader"), TEXT("volumeshader"),
			TEXT("float"), TEXT("color3"), TEXT("color4"), TEXT("vector2"),
			TEXT("vector3"), TEXT("vector4"), TEXT("integer"), TEXT("boolean"),
			TEXT("string"), TEXT("filename"), TEXT("matrix33"), TEXT("matrix44"),
		};

		if (!InfoId.StartsWith(TEXT("ND_")))
		{
			return false;
		}
		const FString Body = InfoId.RightChop(3);
		int32 LastUnderscore = INDEX_NONE;
		if (!Body.FindLastChar(TEXT('_'), LastUnderscore) || LastUnderscore == 0)
		{
			return false;
		}
		OutCategory = Body.Left(LastUnderscore);
		OutType     = Body.RightChop(LastUnderscore + 1);
		return TypeSuffixes.Contains(OutType);
	}

	static FString MtlxTypeForUsd(const FString& UsdType)
	{
		static const TMap<FString, FString> Map = {
			{TEXT("float"), TEXT("float")},     {TEXT("double"), TEXT("float")},
			{TEXT("color3f"), TEXT("color3")},  {TEXT("color3d"), TEXT("color3")},
			{TEXT("color4f"), TEXT("color4")},
			{TEXT("float2"), TEXT("vector2")},  {TEXT("texCoord2f"), TEXT("vector2")},
			{TEXT("float3"), TEXT("vector3")},  {TEXT("vector3f"), TEXT("vector3")},
			{TEXT("normal3f"), TEXT("vector3")},{TEXT("point3f"), TEXT("vector3")},
			{TEXT("float4"), TEXT("vector4")},
			{TEXT("int"), TEXT("integer")},     {TEXT("bool"), TEXT("boolean")},
			{TEXT("string"), TEXT("string")},   {TEXT("token"), TEXT("string")},
			{TEXT("asset"), TEXT("filename")},
		};
		const FString* Found = Map.Find(UsdType);
		return Found ? *Found : FString();
	}

	static FString F(float V)
	{
		return FString::Printf(TEXT("%.9g"), V);
	}

	static bool FormatValue(const pxr::VtValue& Value, const FString& MtlxType, FString& Out)
	{
		if (MtlxType == TEXT("boolean") && Value.IsHolding<bool>())
		{
			Out = Value.UncheckedGet<bool>() ? TEXT("true") : TEXT("false");
		}
		else if (Value.IsHolding<float>())
		{
			Out = F(Value.UncheckedGet<float>());
		}
		else if (Value.IsHolding<double>())
		{
			Out = F(static_cast<float>(Value.UncheckedGet<double>()));
		}
		else if (Value.IsHolding<int>())
		{
			Out = FString::Printf(TEXT("%d"), Value.UncheckedGet<int>());
		}
		else if (Value.IsHolding<pxr::GfVec2f>())
		{
			const pxr::GfVec2f V = Value.UncheckedGet<pxr::GfVec2f>();
			Out = FString::Printf(TEXT("%s, %s"), *F(V[0]), *F(V[1]));
		}
		else if (Value.IsHolding<pxr::GfVec3f>())
		{
			const pxr::GfVec3f V = Value.UncheckedGet<pxr::GfVec3f>();
			Out = FString::Printf(TEXT("%s, %s, %s"), *F(V[0]), *F(V[1]), *F(V[2]));
		}
		else if (Value.IsHolding<pxr::GfVec4f>())
		{
			const pxr::GfVec4f V = Value.UncheckedGet<pxr::GfVec4f>();
			Out = FString::Printf(TEXT("%s, %s, %s, %s"), *F(V[0]), *F(V[1]), *F(V[2]), *F(V[3]));
		}
		else if (Value.IsHolding<pxr::TfToken>())
		{
			Out = UTF8_TO_TCHAR(Value.UncheckedGet<pxr::TfToken>().GetText());
		}
		else if (Value.IsHolding<std::string>())
		{
			Out = UTF8_TO_TCHAR(Value.UncheckedGet<std::string>().c_str());
		}
		else if (Value.IsHolding<pxr::SdfAssetPath>())
		{
			Out = UTF8_TO_TCHAR(Value.UncheckedGet<pxr::SdfAssetPath>().GetAssetPath().c_str());
			Out.ReplaceInline(TEXT("\\"), TEXT("/"));
		}
		else
		{
			return false;
		}
		return true;
	}

	static FString EscapeAttr(const FString& In)
	{
		FString Out = In;
		Out.ReplaceInline(TEXT("&"), TEXT("&amp;"));
		Out.ReplaceInline(TEXT("<"), TEXT("&lt;"));
		Out.ReplaceInline(TEXT(">"), TEXT("&gt;"));
		Out.ReplaceInline(TEXT("\""), TEXT("&quot;"));
		return Out;
	}

	// True when every spec backing the prim comes from a .mtlx layer i.e.
	// the prim only exists because a document reference composed it. Such
	// shaders ARE the document; re-exporting them would duplicate every node
	// on regeneration.
	static bool ComposedOnlyFromMtlx(const pxr::UsdPrim& Prim)
	{
		bool bSawSpec = false;
		for (const pxr::SdfPrimSpecHandle& Spec : Prim.GetPrimStack())
		{
			bSawSpec = true;
			const FString LayerPath = UTF8_TO_TCHAR(Spec->GetLayer()->GetRealPath().c_str());
			if (!LayerPath.EndsWith(TEXT(".mtlx")))
			{
				return false;
			}
		}
		return bSawSpec;
	}

	static FString FinishDocument(
		const FString& Version, FString Body,
		const FString& MaterialName, const FString& SurfaceNodeName)
	{
		Body += FString::Printf(
			TEXT("  <surfacematerial name=\"%s\" type=\"material\">\n")
			TEXT("    <input name=\"surfaceshader\" type=\"surfaceshader\" nodename=\"%s\" />\n")
			TEXT("  </surfacematerial>\n"),
			*EscapeAttr(MaterialName), *EscapeAttr(SurfaceNodeName));
		return FString::Printf(
			TEXT("<?xml version=\"1.0\"?>\n<materialx version=\"%s\">\n%s</materialx>\n"),
			*Version, *Body);
	}

	static bool IsPreviewSurface(const pxr::UsdShadeShader& Shader)
	{
		pxr::TfToken Id;
		Shader.GetIdAttr().Get(&Id);
		return Id == pxr::TfToken("UsdPreviewSurface");
	}

	// Preview-surface materials translate to material INSTANCES whose
	// parameters update in place no document, no shader compile. The
	// engine's own update chain misses value edits under the mtlx render
	// context, so re-pull the instance parameters with its own converter.
	static bool RefreshPreviewSurfaceInstances(AUsdStageActor* StageActor, const pxr::UsdPrim& Prim)
	{
		UUsdPrimLinkCache* LinkCache = StageActor->PrimLinkCache;
		if (!LinkCache)
		{
			return false;
		}

		const UE::FSdfPath PrimPath{Prim.GetPath()};
		bool bRefreshed = false;
		for (UMaterialInstance* Instance :
			 LinkCache->GetInner().GetAssetsForPrim<UMaterialInstance>(PrimPath))
		{
#if WITH_EDITOR
			FMaterialUpdateContext UpdateContext;
			if (UMaterialInstanceConstant* Constant = Cast<UMaterialInstanceConstant>(Instance))
			{
				UpdateContext.AddMaterialInstance(Constant);
			}
#endif
			bRefreshed |= UsdToUnreal::ConvertMaterial(
				pxr::UsdShadeMaterial(Prim), *Instance, StageActor->AssetCache.Get(),
				/*RenderContext=*/nullptr, StageActor->bShareAssetsForIdenticalPrims);
		}
		if (bRefreshed)
		{
			UE_LOG(LogUSDMaterializer, Verbose,
				TEXT("Refreshed preview-surface instance parameters for %s"),
				UTF8_TO_TCHAR(Prim.GetPath().GetText()));
		}
		return bRefreshed;
	}

	// Serialize the material's inline network to a MaterialX document.
	// Returns empty when the network is not materializable (non-ND_* nodes,
	// unmapped input types, or no mtlx surface source).
	static FString BuildDocument(const pxr::UsdPrim& MaterialPrim)
	{
		const pxr::UsdShadeMaterial Material(MaterialPrim);
		const FString MaterialName = UTF8_TO_TCHAR(MaterialPrim.GetName().GetText());
		const auto NodeName = [&MaterialName](const pxr::UsdPrim& Prim)
		{
			return MaterialName + TEXT("_") + UTF8_TO_TCHAR(Prim.GetName().GetText());
		};

		const std::vector<pxr::TfToken> MtlxContext = {pxr::TfToken("mtlx")};
		pxr::UsdShadeShader Surface = Material.ComputeSurfaceSource(MtlxContext);
		if (!Surface || ComposedOnlyFromMtlx(Surface.GetPrim()))
		{
			return {};
		}

		FString Version = TEXT("1.38");
		FString Body;

		for (const pxr::UsdPrim& Prim : pxr::UsdPrimRange(MaterialPrim))
		{
			if (!Prim.IsA<pxr::UsdShadeShader>() || ComposedOnlyFromMtlx(Prim))
			{
				continue;
			}
			const pxr::UsdShadeShader Shader(Prim);

			pxr::TfToken IdToken;
			Shader.GetIdAttr().Get(&IdToken);
			FString Category, OutType;
			if (!ParseNodeDef(UTF8_TO_TCHAR(IdToken.GetText()), Category, OutType))
			{
				UE_LOG(LogUSDMaterializer, Verbose,
					TEXT("Skipping %s: shader %s is not a MaterialX nodedef (info:id=%s)"),
					*FString(UTF8_TO_TCHAR(MaterialPrim.GetPath().GetText())),
					UTF8_TO_TCHAR(Prim.GetName().GetText()),
					UTF8_TO_TCHAR(IdToken.GetText()));
				return {};
			}
			if (Category == TEXT("open_pbr_surface"))
			{
				Version = TEXT("1.39");
			}

			Body += FString::Printf(TEXT("  <%s name=\"%s\" type=\"%s\">\n"),
				*Category, *EscapeAttr(NodeName(Prim)), *OutType);

			const auto AppendValueInput = [&Body, &MaterialPrim](
				const FString& InputName, const pxr::UsdShadeInput& SourceInput)
			{
				const pxr::UsdAttribute SourceAttr = SourceInput.GetAttr();
				const FString UsdType =
					UTF8_TO_TCHAR(SourceInput.GetTypeName().GetAsToken().GetText());
				const FString MtlxType = MtlxTypeForUsd(UsdType);
				pxr::VtValue Value;
				FString ValueStr;
				if (MtlxType.IsEmpty()
					|| !SourceAttr.Get(&Value)
					|| !FormatValue(Value, MtlxType, ValueStr))
				{
					UE_LOG(LogUSDMaterializer, Verbose,
						TEXT("Skipping %s: unmapped input %s (%s)"),
						UTF8_TO_TCHAR(MaterialPrim.GetPath().GetText()), *InputName, *UsdType);
					return false;
				}
				FString ColorSpaceAttr;
				if (SourceAttr.HasColorSpace())
				{
					const FString ColorSpace = UTF8_TO_TCHAR(SourceAttr.GetColorSpace().GetText());
					if (!ColorSpace.IsEmpty())
					{
						ColorSpaceAttr = FString::Printf(
							TEXT(" colorspace=\"%s\""), *EscapeAttr(ColorSpace));
					}
				}
				Body += FString::Printf(
					TEXT("    <input name=\"%s\" type=\"%s\" value=\"%s\"%s />\n"),
					*EscapeAttr(InputName), *MtlxType, *EscapeAttr(ValueStr), *ColorSpaceAttr);
				return true;
			};

			// Two passes matching the Python exporter's shape: authored
			// values first, then connection entries.
			for (const pxr::UsdShadeInput& Input : Shader.GetInputs())
			{
				const FString BaseName = UTF8_TO_TCHAR(Input.GetBaseName().GetText());
				// Namespaced inputs are prim-side bookkeeping, never MaterialX
				// inputs one in the document makes the engine reject the
				// whole file.
				if (BaseName.Contains(TEXT(":")))
				{
					continue;
				}
				if (Input.HasConnectedSource() || !Input.GetAttr().HasAuthoredValue())
				{
					continue;
				}
				if (!AppendValueInput(BaseName, Input))
				{
					return {};
				}
			}
			for (const pxr::UsdShadeInput& Input : Shader.GetInputs())
			{
				if (FString(UTF8_TO_TCHAR(Input.GetBaseName().GetText())).Contains(TEXT(":")))
				{
					continue;
				}
				pxr::UsdShadeSourceInfoVector Sources = Input.GetConnectedSources();
				if (Sources.empty())
				{
					continue;
				}
				pxr::UsdPrim SourcePrim = Sources[0].source.GetPrim();
				pxr::TfToken SourceName = Sources[0].sourceName;
				const pxr::UsdShadeAttributeType SourceAttrType = Sources[0].sourceType;

				// Flattened .mtlx layers often wire shader inputs through a
				// material interface. If the interface authored a value, emit it
				// as a direct MaterialX input; if not, leave the nodedef default.
				if (SourcePrim.GetPath() == MaterialPrim.GetPath()
					&& SourceAttrType == pxr::UsdShadeAttributeType::Input)
				{
					const pxr::UsdShadeInput MaterialInput = Material.GetInput(SourceName);
					if (!MaterialInput || !MaterialInput.GetAttr().HasAuthoredValue())
					{
						continue;
					}
					if (!AppendValueInput(
							UTF8_TO_TCHAR(Input.GetBaseName().GetText()), MaterialInput))
					{
						return {};
					}
					continue;
				}

				// NodeGraph outputs are valid MaterialX topology. The local
				// document writer flattens graph-contained nodes to top-level
				// nodes, so follow the graph output to its driving shader.
				if (SourcePrim.GetPath() != MaterialPrim.GetPath())
				{
					const pxr::UsdShadeNodeGraph SourceGraph(SourcePrim);
					if (SourceGraph)
					{
						const pxr::UsdShadeOutput GraphOutput = SourceGraph.GetOutput(SourceName);
						if (!GraphOutput)
						{
							continue;
						}
						const pxr::UsdShadeSourceInfoVector GraphSources =
							GraphOutput.GetConnectedSources();
						if (GraphSources.empty())
						{
							continue;
						}
						SourcePrim = GraphSources[0].source.GetPrim();
						SourceName = GraphSources[0].sourceName;
					}
				}

				pxr::TfToken SourceId;
				if (pxr::UsdShadeShader SourceShader{SourcePrim})
				{
					SourceShader.GetIdAttr().Get(&SourceId);
				}
				FString SourceCategory, SourceType;
				if (!ParseNodeDef(UTF8_TO_TCHAR(SourceId.GetText()), SourceCategory, SourceType))
				{
					UE_LOG(LogUSDMaterializer, Verbose,
						TEXT("Skipping %s: connection source %s is not a MaterialX shader"),
						UTF8_TO_TCHAR(MaterialPrim.GetPath().GetText()),
						UTF8_TO_TCHAR(SourcePrim.GetPath().GetText()));
					return {};
				}
				const FString OutputAttr =
					SourceName.IsEmpty() || SourceName == pxr::TfToken("out")
						? FString()
						: FString::Printf(
							TEXT(" output=\"%s\""),
							*EscapeAttr(UTF8_TO_TCHAR(SourceName.GetText())));
				Body += FString::Printf(
					TEXT("    <input name=\"%s\" type=\"%s\" nodename=\"%s\"%s />\n"),
					UTF8_TO_TCHAR(Input.GetBaseName().GetText()), *SourceType,
					*EscapeAttr(NodeName(SourcePrim)), *OutputAttr);
			}
			Body += FString::Printf(TEXT("  </%s>\n"), *Category);
		}

		return FinishDocument(Version, MoveTemp(Body), MaterialName, NodeName(Surface.GetPrim()));
	}

	static FString DocDir()
	{
		const FString Dir = FPaths::ProjectSavedDir() / TEXT("OpenUSDConnect") / TEXT("MaterialX");
		return FPaths::ConvertRelativePathToFull(Dir).Replace(TEXT("\\"), TEXT("/"));
	}

	static FString FlatName(const FString& MaterialPrimPath)
	{
		FString Flat = MaterialPrimPath;
		Flat.RemoveFromStart(TEXT("/"));
		Flat.ReplaceInline(TEXT("/"), TEXT("_"));
		return Flat;
	}

	// Documents are content-addressed: the filename carries a hash of the
	// document, so every regeneration is a NEW asset path and the session
	// reference swap is a real composition change the stage actor must
	// retranslate. Metadata-only pokes (customData) are filtered as
	// insignificant by the engine's notice handling and never re-import.
	static FString DocFilePathFor(const FString& MaterialPrimPath, const FString& Doc)
	{
		const FString Hash = FMD5::HashAnsiString(*Doc).Left(8);
		return DocDir() / (FlatName(MaterialPrimPath) + TEXT(".") + Hash + TEXT(".mtlx"));
	}

	// Our session-layer document references for this material (any revision).
	static TArray<pxr::SdfReference> GetOurSessionReferences(
		const pxr::UsdStageRefPtr& Stage, const pxr::SdfPath& PrimPath, const FString& MaterialPrimPath)
	{
		TArray<pxr::SdfReference> Result;
		const pxr::SdfPrimSpecHandle Spec = Stage->GetSessionLayer()->GetPrimAtPath(PrimPath);
		if (!Spec)
		{
			return Result;
		}
		const FString OursPrefix = DocDir() / (FlatName(MaterialPrimPath) + TEXT("."));
		for (const pxr::SdfReference& Ref : Spec->GetReferenceList().GetAddedOrExplicitItems())
		{
			FString AssetPath = UTF8_TO_TCHAR(Ref.GetAssetPath().c_str());
			AssetPath.ReplaceInline(TEXT("\\"), TEXT("/"));
			if (AssetPath.StartsWith(OursPrefix))
			{
				Result.Add(Ref);
			}
		}
		return Result;
	}

	// A composed .mtlx spec on the material outside our document directory
	// means the material is backed by a foreign document leave it alone.
	static bool HasForeignDocument(const pxr::UsdPrim& Prim)
	{
		const FString OursPrefix = DocDir() + TEXT("/");
		for (const pxr::SdfPrimSpecHandle& Spec : Prim.GetPrimStack())
		{
			FString LayerPath = UTF8_TO_TCHAR(Spec->GetLayer()->GetRealPath().c_str());
			LayerPath.ReplaceInline(TEXT("\\"), TEXT("/"));
			if (LayerPath.EndsWith(TEXT(".mtlx")) && !LayerPath.StartsWith(OursPrefix))
			{
				return true;
			}
		}
		return false;
	}

	// Best-effort cleanup of superseded revisions for one material.
	static void DeleteStaleRevisions(const FString& MaterialPrimPath, const FString& KeepFilePath)
	{
		TArray<FString> Files;
		IFileManager::Get().FindFiles(Files, *(DocDir() / (FlatName(MaterialPrimPath) + TEXT(".*.mtlx"))), true, false);
		for (const FString& File : Files)
		{
			const FString Full = DocDir() / File;
			if (Full != KeepFilePath)
			{
				IFileManager::Get().Delete(*Full, false, false, true);
			}
		}
	}
} // namespace

#endif // USE_USD_SDK

bool FUSDMaterialXMaterializer::MaterializeMaterial(AUsdStageActor* StageActor, const FString& MaterialPrimPath)
{
#if USE_USD_SDK
	if (!StageActor)
	{
		return false;
	}
	pxr::UsdStageRefPtr Stage = static_cast<pxr::UsdStageRefPtr>(StageActor->GetOrOpenUsdStage());
	if (!Stage)
	{
		return false;
	}

	const pxr::SdfPath PrimPath(TCHAR_TO_UTF8(*MaterialPrimPath));
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(PrimPath);
	pxr::UsdShadeMaterial Material(Prim);
	if (!Material)
	{
		return false;
	}

	if (HasForeignDocument(Prim))
	{
		UE_LOG(LogUSDMaterializer, Verbose,
			TEXT("Skipping %s: material is already backed by a foreign .mtlx document"),
			*MaterialPrimPath);
		return false;
	}

	// Preview-surface materials refresh their generated material instances
	// in place; only MaterialX networks need the document treatment.
	pxr::UsdShadeShader Universal = Material.ComputeSurfaceSource();
	if (Universal && !ComposedOnlyFromMtlx(Universal.GetPrim()) && IsPreviewSurface(Universal))
	{
		return RefreshPreviewSurfaceInstances(StageActor, Prim);
	}
	if (!Material.GetSurfaceOutput(pxr::TfToken("mtlx")))
	{
		return false;
	}

	const FString Doc = BuildDocument(Prim);
	if (Doc.IsEmpty())
	{
		UE_LOG(LogUSDMaterializer, Warning,
			TEXT("Skipping %s: inline MaterialX network cannot be serialized to a local .mtlx document"),
			*MaterialPrimPath);
		return false;
	}

	const FString DocPath = DocFilePathFor(MaterialPrimPath, Doc);
	TArray<pxr::SdfReference> OurRefs = GetOurSessionReferences(Stage, PrimPath, MaterialPrimPath);
	const std::string DocPathUtf8 = TCHAR_TO_UTF8(*DocPath);
	const bool bUpToDate = OurRefs.Num() == 1 && OurRefs[0].GetAssetPath() == DocPathUtf8;
	if (bUpToDate && IFileManager::Get().FileExists(*DocPath))
	{
		return false;
	}

	if (!IFileManager::Get().FileExists(*DocPath))
	{
		IFileManager::Get().MakeDirectory(*FPaths::GetPath(DocPath), /*Tree=*/true);
		if (!FFileHelper::SaveStringToFile(Doc, *DocPath,
				FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
		{
			UE_LOG(LogUSDMaterializer, Warning,
				TEXT("Failed to write %s for %s"), *DocPath, *MaterialPrimPath);
			return false;
		}
	}

	// Local-only wiring in the session layer: never dirties the root layer,
	// never emits. Swapping to the content-addressed path is a real
	// composition change, so the stage actor retranslates the material and
	// imports the new document.
	{
		const FString MaterialName = UTF8_TO_TCHAR(Prim.GetName().GetText());
		const pxr::SdfPath TargetPath(
			TCHAR_TO_UTF8(*(TEXT("/MaterialX/Materials/") + MaterialName)));
		pxr::UsdEditContext EditContext(Stage, Stage->GetSessionLayer());
		pxr::SdfChangeBlock ChangeBlock;
		pxr::UsdReferences References = Prim.GetReferences();
		for (const pxr::SdfReference& Stale : OurRefs)
		{
			References.RemoveReference(Stale);
		}
		References.AddReference(DocPathUtf8, TargetPath);
	}
	DeleteStaleRevisions(MaterialPrimPath, DocPath);

	UE_LOG(LogUSDMaterializer, Log,
		TEXT("%s %s -> %s"),
		OurRefs.IsEmpty() ? TEXT("Materialized") : TEXT("Refreshed"),
		*MaterialPrimPath, *DocPath);
	return true;
#else
	return false;
#endif
}

bool FUSDMaterialXMaterializer::RerouteMaterialInterfaceEdit(
	AUsdStageActor* StageActor, const FString& PrimPath, const TSet<FString>& InputAttrNames)
{
#if USE_USD_SDK
	if (!StageActor)
	{
		return false;
	}
	pxr::UsdStageRefPtr Stage = static_cast<pxr::UsdStageRefPtr>(StageActor->GetOrOpenUsdStage());
	if (!Stage)
	{
		return false;
	}

	const pxr::SdfPath SdfPrimPath(TCHAR_TO_UTF8(*PrimPath));
	pxr::UsdPrim Prim = Stage->GetPrimAtPath(SdfPrimPath);
	pxr::UsdShadeMaterial Material(Prim);
	if (!Material || GetOurSessionReferences(Stage, SdfPrimPath, PrimPath).IsEmpty())
	{
		return false;
	}

	// Resolve the INLINE shader: for document-backed materials the mtlx
	// source resolves into the document's own node, so try universal first.
	pxr::UsdShadeShader Surface = Material.ComputeSurfaceSource();
	if (!Surface || ComposedOnlyFromMtlx(Surface.GetPrim()))
	{
		const std::vector<pxr::TfToken> MtlxContext = {pxr::TfToken("mtlx")};
		Surface = Material.ComputeSurfaceSource(MtlxContext);
	}
	if (!Surface || ComposedOnlyFromMtlx(Surface.GetPrim()))
	{
		return false;
	}

	bool bForwarded = false;
	for (const FString& AttrName : InputAttrNames)
	{
		const FString BaseName = AttrName.RightChop(7);	 // strip "inputs:"
		if (BaseName.Contains(TEXT(":")))
		{
			continue;
		}
		const pxr::UsdAttribute MaterialAttr =
			Prim.GetAttribute(pxr::TfToken(TCHAR_TO_UTF8(*AttrName)));
		pxr::VtValue Value;
		if (!MaterialAttr || !MaterialAttr.Get(&Value))
		{
			continue;
		}

		pxr::UsdShadeInput ShaderInput =
			Surface.GetInput(pxr::TfToken(TCHAR_TO_UTF8(*BaseName)));
		if (!ShaderInput)
		{
			ShaderInput = Surface.CreateInput(
				pxr::TfToken(TCHAR_TO_UTF8(*BaseName)), MaterialAttr.GetTypeName());
		}
		if (!ShaderInput)
		{
			continue;
		}
		ShaderInput.Set(Value);
		bForwarded = true;
	}
	if (bForwarded)
	{
		UE_LOG(LogUSDMaterializer, Verbose,
			TEXT("Rerouted material interface edit on %s to %s"),
			*PrimPath, UTF8_TO_TCHAR(Surface.GetPath().GetText()));
	}
	return bForwarded;
#else
	return false;
#endif
}

FString FUSDMaterialXMaterializer::FindOwningMaterial(AUsdStageActor* StageActor, const FString& PrimPath)
{
#if USE_USD_SDK
	if (!StageActor)
	{
		return {};
	}
	pxr::UsdStageRefPtr Stage = static_cast<pxr::UsdStageRefPtr>(StageActor->GetOrOpenUsdStage());
	if (!Stage)
	{
		return {};
	}

	pxr::SdfPath Path(TCHAR_TO_UTF8(*PrimPath));
	while (!Path.IsEmpty() && Path != pxr::SdfPath::AbsoluteRootPath())
	{
		const pxr::UsdPrim Prim = Stage->GetPrimAtPath(Path);
		if (Prim && Prim.IsA<pxr::UsdShadeMaterial>())
		{
			return UTF8_TO_TCHAR(Path.GetText());
		}
		Path = Path.GetParentPath();
	}
#endif
	return {};
}
