// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"
#include "USDStageValues.h"

class AUsdStageActor;

/**
 * pxr-backed stage queries used by the Unreal live-sync subsystem.
 *
 * This bridge keeps pxr headers and RTTI inside OpenUSDConnectPXR. Its public
 * interface contains only Unreal and wire-value types.
 */
class OPENUSDCONNECTPXR_API FUSDStageBridge
{
public:
	static bool ReadXformTrs(
		AUsdStageActor* StageActor,
		const FString& PrimPath,
		FEmitXformTrs& OutTrs,
		bool* bOutFromMatrixOp = nullptr);

	static void RestoreCanonicalXformOps(
		AUsdStageActor* StageActor,
		const FString& PrimPath,
		const FEmitXformTrs& Trs);

	static bool ReadConnectableInputs(
		AUsdStageActor* StageActor,
		const FString& PrimPath,
		const TSet<FString>& InputAttrNames,
		FEmitConnectableInput& Out);

	static bool ReadVisibility(
		AUsdStageActor* StageActor,
		const FString& PrimPath,
		FEmitVisibility& OutVisibility);

	static bool ReadLiveOpenMetadata(
		AUsdStageActor* StageActor,
		FUSDLiveOpenMetadata& OutMetadata);

	static bool ReadMaterialXMaterials(
		AUsdStageActor* StageActor,
		FString& OutRootLayerIdentifier,
		TArray<FString>& OutMaterialPrimPaths);
};
