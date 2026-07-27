// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"
#include "USDConnectProtocol.h"

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

		const auto Hello = OpenUSDConnect::CreateHello(
			Builder,
			Builder.CreateString(TCHAR_TO_UTF8(*Role)),
			/*protocol_version=*/kProtocolVersion,
			SyncFrom,
			Builder.CreateString(TCHAR_TO_UTF8(*ClientId)),
			Builder.CreateString(TCHAR_TO_UTF8(*SessionOrigin)),
			Builder.CreateString(TCHAR_TO_UTF8(*Department)),
			Builder.CreateString(TCHAR_TO_UTF8(*Token)));

		Builder.Finish(OpenUSDConnect::CreateEnvelope(
			Builder,
			OpenUSDConnect::Payload::Hello,
			Hello.Union(),
			kSchemaVersion));

		return FrameWithLengthPrefix(Builder);
	}
} // namespace OUC
