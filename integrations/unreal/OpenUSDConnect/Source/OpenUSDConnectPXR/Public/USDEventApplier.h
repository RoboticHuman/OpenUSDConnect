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
 * Decoding uses the flatc-generated bindings staged with the shared native core.
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
	 * Apply a frame already schema-verified at the network boundary. The caller
	 * owns any
	 * surrounding SdfChangeBlock according to EventUsesChangeBlock().
	 */
	static bool ApplyValidatedFrame(TConstArrayView<uint8> RawFrame, AUsdStageActor* StageActor,
									FString* OutTouchedPrim = nullptr,
									OpenUSDConnect::EventPayload* OutEventKind = nullptr);

	/** Whether a validated event kind can join an existing SdfChangeBlock. */
	static bool EventUsesChangeBlock(OpenUSDConnect::EventPayload EventKind);
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
