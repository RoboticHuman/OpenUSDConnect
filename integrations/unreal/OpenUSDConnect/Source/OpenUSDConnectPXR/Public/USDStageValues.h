// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"
#include "USDConnectProtocol.h"

/**
 * Lightweight values read from a USD stage and encoded by the runtime module.
 *
 * The pxr-facing module populates these types. Keeping the values free of pxr
 * types lets the UObject-facing runtime module remain compiled without RTTI.
 */
struct FEmitXformTrs
{
	FString PrimPath;
	float T[3] = {0, 0, 0};
	float R[4] = {1, 0, 0, 0}; // [w, x, y, z]
	float S[3] = {1, 1, 1};
	uint8 Fields = 7; // 1=T, 2=R, 4=S; default: all three
};

struct FEmitVisibility
{
	FString PrimPath;
	bool bVisible = true;
};

struct FEmitConnectableValue
{
	FString Name;     // Input name without the "inputs:" prefix
	FString TypeName; // Declared USD type, such as "color3f", "float", or "asset"
	OpenUSDConnect::ConnectableInputValueType ValueType =
		OpenUSDConnect::ConnectableInputValueType::None;
	float ScalarFloat = 0.0f;
	int32 ScalarInt = 0;
	bool bScalarBool = false;
	FString ScalarString;
	TArray<float> Floats; // Vector and color payloads
};

struct FEmitConnectableInput
{
	FString PrimPath;
	FString InfoId; // Shader info:id; empty for Material, NodeGraph, and light containers
	TArray<FEmitConnectableValue> Inputs;
};

struct FUSDLiveOpenMetadata
{
	FString LayerIdentifier;
	FString Host;
	int32 Port = 0;
	int32 SnapshotSeq = 0;
	int32 ProtocolVersion = 0;
	int32 Epoch = 0;
	FString SceneId;
	FString VfsUrl;
	bool bRequiresToken = false;

	FString MakeKey() const
	{
		return FString::Printf(TEXT("%s|%s|%d|%d|%d"),
			*LayerIdentifier, *Host, Port, Epoch, SnapshotSeq);
	}
};
