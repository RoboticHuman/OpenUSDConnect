// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"

class AUsdStageActor;

/**
 * Engine-local MaterialX document materializer.
 *
 * The engine renders MaterialX only from referenced .mtlx documents; inline
 * ND_* prim networks are recognized but their values are not translated.
 * This helper serializes a material's composed inline network to a document
 * under the project's Saved directory and references it on the material prim
 * in the stage's SESSION layer local-only state that never reaches the
 * sync server, so other clients keep consuming the inline network untouched.
 *
 * On later edits the document is rewritten only when its content actually
 * changed, and a session-layer customData revision is bumped so the stage
 * actor retranslates the material and the engine re-imports the new file.
 *
 * UsdPreviewSurface materials never get documents the engine translates
 * them to material instances whose parameters update in place (no shader
 * compile). Its update chain misses value edits under the mtlx render
 * context, so this helper re-pulls the linked instances' parameters with
 * the engine's own converter instead. Skipped and left as authored:
 * renderer-specific shaders (PxrSurface, Lama), networks with unmapped
 * input types, and materials already backed by a foreign .mtlx document
 * reference.
 */
class OPENUSDCONNECTPXR_API FUSDMaterialXMaterializer
{
public:
	/**
	 * Materialize (or refresh) the document backing one material.
	 * @return true when the document file or the session reference changed.
	 */
	static bool MaterializeMaterial(AUsdStageActor* StageActor, const FString& MaterialPrimPath);

	/**
	 * The owning Material prim path for a changed prim: the prim itself or
	 * its nearest Material ancestor. Empty when the prim is not part of a
	 * material subtree.
	 */
	static FString FindOwningMaterial(AUsdStageActor* StageActor, const FString& PrimPath);

	/**
	 * Local edits to a Material's interface inputs land on attributes that
	 * only exist because our session document projected them orphans to
	 * every other client. When the material is backed by one of our
	 * documents, copy the edited values onto the inline surface shader's
	 * same-named inputs (real shared-scene authoring; the generated document
	 * guarantees the 1:1 name mapping). The shader change then emits,
	 * regenerates the document, and re-imports through the normal paths.
	 *
	 * @param InputAttrNames  Changed property names ("inputs:base_color").
	 * @return true when the edit was rerouted (callers skip the
	 *         material-level emit).
	 */
	static bool RerouteMaterialInterfaceEdit(
		AUsdStageActor* StageActor, const FString& PrimPath, const TSet<FString>& InputAttrNames);
};
