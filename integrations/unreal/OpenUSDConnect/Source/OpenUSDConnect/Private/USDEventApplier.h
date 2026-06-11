// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"

class AUsdStageActor;

/**
 * Stateless helper that decodes a raw FlatBuffers BroadcastEvent frame
 * and applies the contained USD event to the pxr stage owned by the given
 * AUsdStageActor.
 *
 * The raw byte manipulation follows the FlatBuffers wire format directly
 * (no generated C++ code required). All pxr USD API calls are guarded by
 * #if USE_USD_SDK so the module compiles even without the USD SDK, though
 * event application will be a no-op in that case.
 *
 * Supported events (MVP):
 *   EnsurePrim, EnsureXformOps, SetXformTrs, DeletePrim, DeactivatePrim,
 *   RenamePrim, SetVisibility, SetStageMetadata, SetReference, SetPayload,
 *   LoadPayload, UnloadPayload, SetVariantSelections, SetMaterialBinding
 *
 * Deferred / TODO:
 *   SetGprimAttrs (bulk mesh data), SetConnectableInput, SetConnectableConnection
 */
class OPENUSDCONNECT_API FUSDEventApplier
{
public:
	/**
	 * Decode and apply a single BroadcastEvent frame to the given stage actor.
	 * @param RawFrame  Complete FlatBuffers Envelope bytes (no framing prefix).
	 * @param StageActor  The AUsdStageActor whose pxr stage to modify.
	 */
	static void ApplyFrame(const TArray<uint8>& RawFrame, AUsdStageActor* StageActor);
};
