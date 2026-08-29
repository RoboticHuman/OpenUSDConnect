// Copyright OpenUSDConnect Contributors. All Rights Reserved.
//
// Wire protocol access for the plugin. The message/event types are the
// flatc-generated bindings in the shared native core (regenerate with
// scripts/generate_flatbuffers.sh after schema changes); this header only
// adds the framing limit and small UE-flavored helpers on top.
//
// The generated header pins the FlatBuffers runtime version it was produced
// with (a static_assert fires on mismatch). setup_flatbuffers.py fetches the
// matching runtime headers into the plugin's ThirdParty folder.

#pragma once

#include "CoreMinimal.h"

THIRD_PARTY_INCLUDES_START
#include "openusdconnect/client/protocol_codec.h"
THIRD_PARTY_INCLUDES_END

namespace OUC
{
inline constexpr uint32 kMaxFrameSize =
	static_cast<uint32>(openusdconnect::client::kDefaultMaxFrameSize);
inline constexpr uint16 kSchemaVersion = openusdconnect::client::kSchemaVersion;
inline constexpr int32 kProtocolVersion = openusdconnect::client::kProtocolVersion;

inline FString ToFString(const ::flatbuffers::String* S)
{
	return S ? FString(UTF8_TO_TCHAR(S->c_str())) : FString();
}

// Root Envelope of a raw (already de-framed) buffer; nullptr when the
// buffer is too small to hold one.
inline const OpenUSDConnect::Envelope* GetEnvelopeFromFrame(const TArray<uint8>& Frame)
{
	openusdconnect::client::EnvelopeView View;
	return openusdconnect::client::DecodeEnvelope(Frame.GetData(), static_cast<size_t>(Frame.Num()),
												  View) ==
				   openusdconnect::client::ProtocolResult::Success
			   ? View.Get()
			   : nullptr;
}

inline OpenUSDConnect::Payload GetEnvelopePayloadType(const TArray<uint8>& Frame)
{
	const OpenUSDConnect::Envelope* Env = GetEnvelopeFromFrame(Frame);
	return Env ? Env->payload_type() : OpenUSDConnect::Payload::NONE;
}
} // namespace OUC
