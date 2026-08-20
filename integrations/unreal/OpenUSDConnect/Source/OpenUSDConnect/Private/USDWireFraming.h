// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"
#include "USDConnectProtocol.h"
#include "openusdconnect/client/frame_codec.h"
#include "openusdconnect/client/protocol_codec.h"
#include "flatbuffers/detached_buffer.h"

#include <utility>

namespace OUC
{
inline openusdconnect::client::FrameResult
ToFrameResult(openusdconnect::client::ProtocolResult Result) noexcept
{
	switch (Result)
	{
	case openusdconnect::client::ProtocolResult::Success:
		return openusdconnect::client::FrameResult::Success;
	case openusdconnect::client::ProtocolResult::InvalidMaxFrameSize:
		return openusdconnect::client::FrameResult::InvalidMaxFrameSize;
	case openusdconnect::client::ProtocolResult::PayloadTooLarge:
		return openusdconnect::client::FrameResult::PayloadTooLarge;
	default:
		return openusdconnect::client::FrameResult::EmptyPayload;
	}
}

inline std::string_view ToStringView(const FTCHARToUTF8& Value) noexcept
{
	return {Value.Get(), static_cast<size_t>(Value.Length())};
}

/**
 * Immutable contiguous wire frame backed directly by FlatBuffers' allocation.
 * The first four bytes are the network-order payload length; the remaining
 * bytes are the FlatBuffer. Moving this object transfers allocation ownership.
 */
class FWireFrame final
{
public:
	FWireFrame() = default;
	explicit FWireFrame(flatbuffers::DetachedBuffer&& InBuffer)
		: Buffer(std::move(InBuffer))
	{
	}

	FWireFrame(FWireFrame&&) = default;
	FWireFrame& operator=(FWireFrame&&) = default;
	FWireFrame(const FWireFrame&) = delete;
	FWireFrame& operator=(const FWireFrame&) = delete;

	const uint8* GetData() const
	{
		return Buffer.data();
	}
	int32 Num() const
	{
		return static_cast<int32>(Buffer.size());
	}
	bool IsEmpty() const
	{
		return Buffer.size() == 0;
	}

private:
	flatbuffers::DetachedBuffer Buffer;
};

/**
 * Finish with FlatBuffers' in-allocation size prefix, rewrite that prefix to
 * the protocol's big-endian form, and detach the original allocation.
 * No serialized payload bytes are copied.
 */
inline openusdconnect::client::FrameResult
FinishWireFrame(flatbuffers::FlatBufferBuilder& Builder,
				flatbuffers::Offset<OpenUSDConnect::Envelope> RootOffset, FWireFrame& OutFrame)
{
	OutFrame = FWireFrame();
	const openusdconnect::client::ProtocolResult Result =
		openusdconnect::client::FinishEnvelopeFrame(Builder, RootOffset);
	if (Result != openusdconnect::client::ProtocolResult::Success)
	{
		return ToFrameResult(Result);
	}
	OutFrame = FWireFrame(Builder.Release());
	return openusdconnect::client::FrameResult::Success;
}

// Build a complete framed Envelope{Hello} message.
//   Role     = "receiver" or "emitter"
//   SyncFrom = receiver-side resume point (0 = full replay; emitters pass 0)
//   Token    = saved TOFU auth token, or empty for first-connect issuance
inline openusdconnect::client::FrameResult
BuildHelloFrame(const FString& Role, int32 SyncFrom, const FString& ClientId,
				const FString& SessionOrigin, const FString& Department, FWireFrame& OutFrame,
				const FString& Token = FString(), const FString& ProducerSessionId = FString())
{
	flatbuffers::FlatBufferBuilder Builder(512);
	const FTCHARToUTF8 RoleUtf8(*Role);
	const FTCHARToUTF8 ClientIdUtf8(*ClientId);
	const FTCHARToUTF8 SessionOriginUtf8(*SessionOrigin);
	const FTCHARToUTF8 DepartmentUtf8(*Department);
	const FTCHARToUTF8 TokenUtf8(*Token);
	const FTCHARToUTF8 ProducerSessionIdUtf8(*ProducerSessionId);
	const openusdconnect::client::HelloParameters Parameters{
		ToStringView(RoleUtf8),
		SyncFrom,
		ToStringView(ClientIdUtf8),
		ToStringView(SessionOriginUtf8),
		ToStringView(DepartmentUtf8),
		ToStringView(TokenUtf8),
		false,
		OpenUSDConnect::LayerMode::Managed,
		ToStringView(ProducerSessionIdUtf8),
	};
	const openusdconnect::client::ProtocolResult Result =
		openusdconnect::client::BuildHelloFrame(Builder, Parameters);
	if (Result != openusdconnect::client::ProtocolResult::Success)
	{
		OutFrame = FWireFrame();
		return ToFrameResult(Result);
	}
	OutFrame = FWireFrame(Builder.Release());
	return openusdconnect::client::FrameResult::Success;
}
} // namespace OUC
