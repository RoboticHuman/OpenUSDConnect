// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "TxnBuilder.h"
#include "USDConnectProtocol.h"

#include "flatbuffers/flatbuffer_builder.h"

using namespace OUC;

// ---------------------------------------------------------------------------
// Internal helper: prepend 4-byte big-endian length to a finished FB buffer.
// ---------------------------------------------------------------------------
namespace
{
	TArray<uint8> FrameBuilder(flatbuffers::FlatBufferBuilder& Builder)
	{
		const uint8_t* FBData = Builder.GetBufferPointer();
		const size_t   FBSize = Builder.GetSize();

		TArray<uint8> Result;
		Result.SetNumUninitialized(4 + static_cast<int32>(FBSize));
		Result[0] = static_cast<uint8>((FBSize >> 24) & 0xFF);
		Result[1] = static_cast<uint8>((FBSize >> 16) & 0xFF);
		Result[2] = static_cast<uint8>((FBSize >>  8) & 0xFF);
		Result[3] = static_cast<uint8>( FBSize        & 0xFF);
		FMemory::Memcpy(Result.GetData() + 4, FBData, FBSize);
		return Result;
	}
} // namespace

// ---------------------------------------------------------------------------
// Build Envelope { Txn { events: [EventWrapper{SetXformTrs}, ...] } }
// ---------------------------------------------------------------------------
TArray<uint8> BuildXformTxnFrame(const FString& ClientId, const TArray<FEmitXformTrs>& Xforms)
{
	if (Xforms.IsEmpty()) return {};

	flatbuffers::FlatBufferBuilder Builder(512 + Xforms.Num() * 128);

	// Build all EventWrapper offsets first (depth-first / bottom-up)
	TArray<flatbuffers::Offset<void>> EwOffsets;
	EwOffsets.Reserve(Xforms.Num());

	for (const FEmitXformTrs& X : Xforms)
	{
		auto PrimStr = Builder.CreateString(TCHAR_TO_UTF8(*X.PrimPath));
		auto TOff    = Builder.CreateVector(X.T, 3);
		auto ROff    = Builder.CreateVector(X.R, 4);
		auto SOff    = Builder.CreateVector(X.S, 3);

		const flatbuffers::uoffset_t TrsStart = Builder.StartTable();
		Builder.AddOffset(VT::SetXformTrs_Prim,             PrimStr);
		Builder.AddElement<uint8_t>(VT::SetXformTrs_Fields, X.Fields, 0);
		Builder.AddOffset(VT::SetXformTrs_T,                TOff);
		Builder.AddOffset(VT::SetXformTrs_R,                ROff);
		Builder.AddOffset(VT::SetXformTrs_S,                SOff);
		const flatbuffers::uoffset_t TrsOff = Builder.EndTable(TrsStart);

		const flatbuffers::uoffset_t EwStart = Builder.StartTable();
		Builder.AddElement<uint8_t>(VT::EventWrapper_EventType, kEvSetXformTrs, 0);
		Builder.AddOffset(VT::EventWrapper_Event, flatbuffers::Offset<void>(TrsOff));
		const flatbuffers::uoffset_t EwOff = Builder.EndTable(EwStart);

		EwOffsets.Add(flatbuffers::Offset<void>(EwOff));
	}

	// CreateVector<Offset<void>> writes a vector of relative offsets to tables.
	// TArray<Offset<void>> is layout-compatible with the raw Offset<void>[] expected.
	auto EventsVec = Builder.CreateVector(EwOffsets.GetData(), EwOffsets.Num());

	auto ClientIdStr = Builder.CreateString(TCHAR_TO_UTF8(*ClientId));
	const flatbuffers::uoffset_t TxnStart = Builder.StartTable();
	Builder.AddOffset(VT::Txn_ClientId, ClientIdStr);
	Builder.AddOffset(VT::Txn_Events,   EventsVec);
	const flatbuffers::uoffset_t TxnOff = Builder.EndTable(TxnStart);

	const flatbuffers::uoffset_t EnvStart = Builder.StartTable();
	Builder.AddElement<uint8_t>(VT::Envelope_PayloadType, kPayloadTxn, 0);
	Builder.AddOffset(VT::Envelope_Payload, flatbuffers::Offset<void>(TxnOff));
	const flatbuffers::uoffset_t EnvOff = Builder.EndTable(EnvStart);
	Builder.Finish(flatbuffers::Offset<void>(EnvOff));

	return FrameBuilder(Builder);
}

// ---------------------------------------------------------------------------
// Build Envelope { Txn { events: [EventWrapper{SetVisibility}, ...] } }
// ---------------------------------------------------------------------------
TArray<uint8> BuildVisibilityTxnFrame(const FString& ClientId, const TArray<FEmitVisibility>& Visibilities)
{
	if (Visibilities.IsEmpty()) return {};

	flatbuffers::FlatBufferBuilder Builder(256 + Visibilities.Num() * 64);

	TArray<flatbuffers::Offset<void>> EwOffsets;
	EwOffsets.Reserve(Visibilities.Num());

	for (const FEmitVisibility& V : Visibilities)
	{
		auto PrimStr = Builder.CreateString(TCHAR_TO_UTF8(*V.PrimPath));

		const flatbuffers::uoffset_t VisStart = Builder.StartTable();
		Builder.AddOffset(VT::SetVisibility_Prim, PrimStr);
		Builder.AddElement<uint8_t>(VT::SetVisibility_Visible, V.bVisible ? 1 : 0, 0);
		const flatbuffers::uoffset_t VisOff = Builder.EndTable(VisStart);

		const flatbuffers::uoffset_t EwStart = Builder.StartTable();
		Builder.AddElement<uint8_t>(VT::EventWrapper_EventType, kEvSetVisibility, 0);
		Builder.AddOffset(VT::EventWrapper_Event, flatbuffers::Offset<void>(VisOff));
		const flatbuffers::uoffset_t EwOff = Builder.EndTable(EwStart);

		EwOffsets.Add(flatbuffers::Offset<void>(EwOff));
	}

	auto EventsVec = Builder.CreateVector(EwOffsets.GetData(), EwOffsets.Num());

	auto ClientIdStr = Builder.CreateString(TCHAR_TO_UTF8(*ClientId));
	const flatbuffers::uoffset_t TxnStart = Builder.StartTable();
	Builder.AddOffset(VT::Txn_ClientId, ClientIdStr);
	Builder.AddOffset(VT::Txn_Events,   EventsVec);
	const flatbuffers::uoffset_t TxnOff = Builder.EndTable(TxnStart);

	const flatbuffers::uoffset_t EnvStart = Builder.StartTable();
	Builder.AddElement<uint8_t>(VT::Envelope_PayloadType, kPayloadTxn, 0);
	Builder.AddOffset(VT::Envelope_Payload, flatbuffers::Offset<void>(TxnOff));
	const flatbuffers::uoffset_t EnvOff = Builder.EndTable(EnvStart);
	Builder.Finish(flatbuffers::Offset<void>(EnvOff));

	return FrameBuilder(Builder);
}
