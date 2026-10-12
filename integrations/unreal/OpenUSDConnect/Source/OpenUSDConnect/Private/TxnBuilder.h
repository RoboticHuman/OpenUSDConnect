// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"
#include "USDConnectProtocol.h"
#include "USDStageValues.h"

#include <vector>

/**
 * Encode a batch of SetXformTrs events into a complete Envelope{Txn} FlatBuffers frame,
 * including the 4-byte big-endian length prefix. When bIncludeEnsureXformOps is true,
 * each value event is preceded by its structural xform-op prerequisite.
 */
openusdconnect::client::ProtocolResult BuildXformTxnFrame(uint64 TxnId,
														  const TArray<FEmitXformTrs>& Xforms,
														  std::vector<uint8>& OutFrame,
														  bool bIncludeEnsureXformOps = false);

/**
 * Encode a batch of SetVisibility events into a complete Envelope{Txn} frame.
 */
openusdconnect::client::ProtocolResult
BuildVisibilityTxnFrame(uint64 TxnId, const TArray<FEmitVisibility>& Visibilities,
						std::vector<uint8>& OutFrame);

/**
 * Encode a batch of SetConnectableInput events into a complete Envelope{Txn} frame.
 */
openusdconnect::client::ProtocolResult
BuildConnectableInputTxnFrame(uint64 TxnId, const TArray<FEmitConnectableInput>& Events,
							  std::vector<uint8>& OutFrame);
