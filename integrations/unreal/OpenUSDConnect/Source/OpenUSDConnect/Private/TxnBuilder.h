// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"
#include "USDConnectProtocol.h"

/**
 * Lightweight structs for USD events to be emitted to the server.
 * These are populated by UUSDConnectSubsystem when it detects a stage change,
 * then encoded into a FlatBuffers Txn frame by the builders below.
 */

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

struct FEmitConnectableValue
{
	FString Name;         // input name without the "inputs:" prefix
	FString TypeName;     // declared USD type (e.g. "color3f", "float", "asset")
	OpenUSDConnect::ConnectableInputValueType ValueType =
		OpenUSDConnect::ConnectableInputValueType::None;
	float   ScalarFloat = 0.f;
	int32   ScalarInt   = 0;
	bool    bScalarBool = false;
	FString ScalarString;
	TArray<float> Floats;  // vector/color payloads
};

struct FEmitConnectableInput
{
	FString PrimPath;
	FString InfoId;       // shader info:id; empty for Material/NodeGraph/light containers
	TArray<FEmitConnectableValue> Inputs;
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

/**
 * Encode a batch of SetConnectableInput events into a complete Envelope{Txn} frame.
 */
TArray<uint8> BuildConnectableInputTxnFrame(const FString& ClientId, const TArray<FEmitConnectableInput>& Events);
