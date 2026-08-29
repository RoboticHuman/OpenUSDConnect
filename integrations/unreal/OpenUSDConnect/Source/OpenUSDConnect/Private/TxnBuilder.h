// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"
#include "USDStageValues.h"
#include "USDWireFraming.h"

/**
 * Encode a batch of SetXformTrs events into a complete Envelope{Txn} FlatBuffers frame,
 * including the 4-byte big-endian length prefix. When bIncludeEnsureXformOps is true,
 * each value event is preceded by its structural xform-op prerequisite.
 */
openusdconnect::client::FrameResult BuildXformTxnFrame(uint64 TxnId,
													   const TArray<FEmitXformTrs>& Xforms,
													   OUC::FWireFrame& OutFrame,
													   bool bIncludeEnsureXformOps = false);

/**
 * Encode a batch of SetVisibility events into a complete Envelope{Txn} frame.
 */
openusdconnect::client::FrameResult
BuildVisibilityTxnFrame(uint64 TxnId, const TArray<FEmitVisibility>& Visibilities,
						OUC::FWireFrame& OutFrame);

/**
 * Encode a batch of SetConnectableInput events into a complete Envelope{Txn} frame.
 */
openusdconnect::client::FrameResult
BuildConnectableInputTxnFrame(uint64 TxnId, const TArray<FEmitConnectableInput>& Events,
							  OUC::FWireFrame& OutFrame);
