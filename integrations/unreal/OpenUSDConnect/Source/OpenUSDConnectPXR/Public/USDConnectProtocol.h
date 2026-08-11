// Copyright OpenUSDConnect Contributors. All Rights Reserved.
//
// Wire protocol access for the plugin. The message/event types are the
// flatc-generated bindings under Schema/ (regenerate with
// scripts/generate_flatbuffers.sh after schema changes); this header only
// adds the framing limit and small UE-flavored helpers on top.
//
// The generated header pins the FlatBuffers runtime version it was produced
// with (a static_assert fires on mismatch). setup_flatbuffers.py fetches the
// matching runtime headers into the plugin's ThirdParty folder.

#pragma once

#include "CoreMinimal.h"

THIRD_PARTY_INCLUDES_START
#include "Schema/messages_generated.h"
THIRD_PARTY_INCLUDES_END

namespace OUC
{
	inline constexpr uint32 kMaxFrameSize = 16 * 1024 * 1024;  // 16 MiB
	inline constexpr uint16 kSchemaVersion = 9;
	inline constexpr int32 kProtocolVersion = 10;

	inline FString ToFString(const ::flatbuffers::String* S)
	{
		return S ? FString(UTF8_TO_TCHAR(S->c_str())) : FString();
	}

	// Root Envelope of a raw (already de-framed) buffer; nullptr when the
	// buffer is too small to hold one.
	inline const OpenUSDConnect::Envelope* GetEnvelopeFromFrame(const TArray<uint8>& Frame)
	{
		if (Frame.Num() < 8)
		{
			return nullptr;
		}
		::flatbuffers::Verifier Verifier(
			Frame.GetData(),
			static_cast<size_t>(Frame.Num()));
		if (!OpenUSDConnect::VerifyEnvelopeBuffer(Verifier))
		{
			return nullptr;
		}
		const OpenUSDConnect::Envelope* Envelope =
			OpenUSDConnect::GetEnvelope(Frame.GetData());
		return Envelope && Envelope->schema_version() == kSchemaVersion
			? Envelope
			: nullptr;
	}

	inline OpenUSDConnect::Payload GetEnvelopePayloadType(const TArray<uint8>& Frame)
	{
		const OpenUSDConnect::Envelope* Env = GetEnvelopeFromFrame(Frame);
		return Env ? Env->payload_type() : OpenUSDConnect::Payload::NONE;
	}
} // namespace OUC
