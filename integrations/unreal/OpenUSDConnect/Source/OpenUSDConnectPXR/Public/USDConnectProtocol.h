// Copyright OpenUSDConnect Contributors. All Rights Reserved.
//
// Wire protocol access for the plugin. The message/event types are the
// flatc-generated bindings in the shared native core (regenerate with
// scripts/generate_flatbuffers.sh after schema changes); this header only
// adds small UE-flavored helpers on top.
//
// The generated header pins the FlatBuffers runtime version it was produced
// with (a static_assert fires on mismatch). setup_flatbuffers.py fetches the
// matching runtime headers into the plugin's ThirdParty folder.

#pragma once

#include "CoreMinimal.h"

THIRD_PARTY_INCLUDES_START
#include "openusdconnect/client/protocol_codec.h"
THIRD_PARTY_INCLUDES_END

#include <string>
#include <string_view>

namespace OUC
{
inline FString ToFString(const ::flatbuffers::String* S)
{
	return S ? FString(UTF8_TO_TCHAR(S->c_str())) : FString();
}

inline FString ToFString(std::string_view S)
{
	return FString::ConstructFromPtrSize(reinterpret_cast<const UTF8CHAR*>(S.data()),
										 static_cast<int32>(S.size()));
}

inline std::string ToUtf8(const FString& S)
{
	const FTCHARToUTF8 Converted(*S);
	return std::string(Converted.Get(), static_cast<size_t>(Converted.Length()));
}
} // namespace OUC
