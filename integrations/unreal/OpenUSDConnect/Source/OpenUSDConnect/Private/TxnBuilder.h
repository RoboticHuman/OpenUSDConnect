// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"

/**
 * Lightweight structs for USD events to be emitted to the server.
 * These are populated by UUSDConnectSubsystem when it detects a stage change,
 * then encoded into a FlatBuffers Txn frame by BuildTxnFrame().
 */

enum class EEmitEventKind : uint8
{
	SetXformTrs,
	SetVisibility,
	EnsurePrim,
	DeletePrim,
};

struct FEmitXformTrs
{
	FString PrimPath;
	float T[3] = {0,0,0};
	float R[4] = {1,0,0,0};  // [w, x, y, z]
	float S[3] = {1,1,1};
	uint8 Fields = 7;         // 1=T, 2=R, 4=S — default: all three
};

struct FEmitVisibility
{
	FString PrimPath;
	bool bVisible = true;
};

struct FEmitEnsurePrim
{
	FString PrimPath;
	FString TypeName;
};

struct FEmitDeletePrim
{
	FString PrimPath;
};

/**
 * Encode a batch of SetXformTrs events into a complete Envelope{Txn} FlatBuffers frame,
 * including the 4-byte big-endian length prefix.
 */
TArray<uint8> BuildXformTxnFrame(const FString& ClientId, const TArray<FEmitXformTrs>& Xforms);

/**
 * Encode a batch of SetVisibility events into a complete Envelope{Txn} frame.
 */
TArray<uint8> BuildVisibilityTxnFrame(const FString& ClientId, const TArray<FEmitVisibility>& Visibilities);
