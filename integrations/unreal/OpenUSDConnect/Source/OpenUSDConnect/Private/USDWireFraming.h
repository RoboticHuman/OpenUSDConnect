// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"
#include "USDConnectProtocol.h"
#include "flatbuffers/flatbuffer_builder.h"

namespace OUC
{
	// Prepend a 4-byte big-endian length to a finished FlatBuffers buffer.
	// Matches the framing produced by the Python server (`struct.pack(">I", ...)`).
	inline TArray<uint8> FrameWithLengthPrefix(flatbuffers::FlatBufferBuilder& Builder)
	{
		const uint8_t* FBData = Builder.GetBufferPointer();
		const size_t   FBSize = Builder.GetSize();

		TArray<uint8> Frame;
		Frame.SetNumUninitialized(4 + static_cast<int32>(FBSize));
		Frame[0] = static_cast<uint8>((FBSize >> 24) & 0xFF);
		Frame[1] = static_cast<uint8>((FBSize >> 16) & 0xFF);
		Frame[2] = static_cast<uint8>((FBSize >>  8) & 0xFF);
		Frame[3] = static_cast<uint8>( FBSize        & 0xFF);
		FMemory::Memcpy(Frame.GetData() + 4, FBData, FBSize);
		return Frame;
	}

	// Decode Envelope.payload_type from a raw framed buffer. Returns 0 if the
	// buffer is too small or the field is absent.
	inline uint8 GetEnvelopePayloadType(const TArray<uint8>& Frame)
	{
		if (Frame.Num() < 8) return 0;
		const uint8* Root = FB::GetRoot(Frame);
		return Root ? FB::GetField<uint8>(Root, VT::Envelope_PayloadType, 0) : 0;
	}

	// Build a complete framed Envelope{Hello} message.
	//   Role     = "receiver" or "emitter"
	//   SyncFrom = receiver-side resume point (0 = full replay; emitters pass 0)
	//   Token    = saved TOFU auth token, or empty for first-connect issuance
	inline TArray<uint8> BuildHelloFrame(
		const FString& Role,
		int32          SyncFrom,
		const FString& ClientId,
		const FString& SessionOrigin,
		const FString& Department,
		const FString& Token = FString())
	{
		flatbuffers::FlatBufferBuilder Builder(512);

		auto RoleOff       = Builder.CreateString(TCHAR_TO_UTF8(*Role));
		auto ClientIdOff   = Builder.CreateString(TCHAR_TO_UTF8(*ClientId));
		auto OriginOff     = Builder.CreateString(TCHAR_TO_UTF8(*SessionOrigin));
		auto DepartmentOff = Builder.CreateString(TCHAR_TO_UTF8(*Department));
		auto TokenOff      = Builder.CreateString(TCHAR_TO_UTF8(*Token));

		const flatbuffers::uoffset_t HelloStart = Builder.StartTable();
		Builder.AddOffset(VT::Hello_Role,                      RoleOff);
		Builder.AddElement<int32_t>(VT::Hello_ProtocolVersion, 1,        0);
		Builder.AddElement<int32_t>(VT::Hello_SyncFrom,        SyncFrom, 0);
		Builder.AddOffset(VT::Hello_ClientId,                  ClientIdOff);
		Builder.AddOffset(VT::Hello_Origin,                    OriginOff);
		Builder.AddOffset(VT::Hello_Department,                DepartmentOff);
		Builder.AddOffset(VT::Hello_Token,                     TokenOff);
		const flatbuffers::uoffset_t HelloOff = Builder.EndTable(HelloStart);

		const flatbuffers::uoffset_t EnvStart = Builder.StartTable();
		Builder.AddElement<uint8_t>(VT::Envelope_PayloadType, kPayloadHello, 0);
		Builder.AddOffset(VT::Envelope_Payload, flatbuffers::Offset<void>(HelloOff));
		const flatbuffers::uoffset_t EnvOff = Builder.EndTable(EnvStart);
		Builder.Finish(flatbuffers::Offset<void>(EnvOff));

		return FrameWithLengthPrefix(Builder);
	}
} // namespace OUC
