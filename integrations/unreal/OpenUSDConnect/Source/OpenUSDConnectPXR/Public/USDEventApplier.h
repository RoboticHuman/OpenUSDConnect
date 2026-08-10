// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"
#include "USDConnectProtocol.h"

class AUsdStageActor;

/**
 * Stateless helper that decodes a raw FlatBuffers BroadcastEvent frame
 * and applies the contained USD event to the pxr stage owned by the given
 * AUsdStageActor.
 *
 * Decoding uses the flatc-generated bindings (Schema/messages_generated.h).
 * All pxr USD API calls are guarded by #if USE_USD_SDK so the module
 * compiles even without the USD SDK, though event application will be a
 * no-op in that case.
 *
 * All event kinds apply. Everything is stage-level authoring: what
 * renders from the result is up to UE's own USD translation (materials per
 * the stage actor's render context, PointInstancers as instanced meshes,
 * gprims and cameras per the USDImporter schema translators).
 */
class OPENUSDCONNECTPXR_API FUSDEventApplier
{
public:
	/**
	 * Decode and apply a single BroadcastEvent frame to the given stage actor.
	 * @param RawFrame  Complete FlatBuffers Envelope bytes (no framing prefix).
	 * @param StageActor  The AUsdStageActor whose pxr stage to modify.
	 * @param OutTouchedPrim  Optional: receives the event's target prim path
	 *        (empty for stage-scoped events).
	 * @param OutEventKind  Optional: receives the wire event kind.
	 */
	static bool ApplyFrame(const TArray<uint8>& RawFrame, AUsdStageActor* StageActor,
	                       FString* OutTouchedPrim = nullptr,
	                       OpenUSDConnect::EventPayload* OutEventKind = nullptr);

	/**
	 * Whether the frame's event kind is safe to apply inside an SdfChangeBlock.
	 * Value writes on existing prims are; structural events (prim definition,
	 * composition arcs, variants, schema application) are not — they need the
	 * stage to recompose as they apply. Callers batching multiple frames must
	 * close any open block before applying a frame this returns false for.
	 */
	static bool FrameUsesChangeBlock(const TArray<uint8>& RawFrame);
};

/**
 * Opaque owner for an SdfChangeBlock.
 *
 * Keeping this type in the pxr-facing module lets the Unreal subsystem batch
 * value events without enabling C++ RTTI for its UObject module.
 */
class OPENUSDCONNECTPXR_API FUSDEventChangeBlock
{
public:
	FUSDEventChangeBlock();
	~FUSDEventChangeBlock();

	FUSDEventChangeBlock(const FUSDEventChangeBlock&) = delete;
	FUSDEventChangeBlock& operator=(const FUSDEventChangeBlock&) = delete;

private:
	struct FImpl;
	TUniquePtr<FImpl> Impl;
};
