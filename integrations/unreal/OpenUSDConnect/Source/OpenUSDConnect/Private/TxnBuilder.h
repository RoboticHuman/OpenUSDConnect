// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"
#include "USDStageValues.h"

/**
 * Encode a batch of SetXformTrs events into a complete Envelope{Txn} FlatBuffers frame,
 * including the 4-byte big-endian length prefix. When bIncludeEnsureXformOps is true,
 * each value event is preceded by its structural xform-op prerequisite.
 */
TArray<uint8> BuildXformTxnFrame(
	const FString& ClientId,
	const TArray<FEmitXformTrs>& Xforms,
	bool bIncludeEnsureXformOps = false);

/**
 * Encode a batch of SetVisibility events into a complete Envelope{Txn} frame.
 */
TArray<uint8> BuildVisibilityTxnFrame(const FString& ClientId, const TArray<FEmitVisibility>& Visibilities);

/**
 * Encode a batch of SetConnectableInput events into a complete Envelope{Txn} frame.
 */
TArray<uint8> BuildConnectableInputTxnFrame(const FString& ClientId, const TArray<FEmitConnectableInput>& Events);
