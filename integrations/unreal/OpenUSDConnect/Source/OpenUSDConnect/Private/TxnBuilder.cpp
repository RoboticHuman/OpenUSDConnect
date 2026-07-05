// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "TxnBuilder.h"
#include "USDConnectProtocol.h"
#include "USDWireFraming.h"

#include "flatbuffers/flatbuffer_builder.h"

using namespace OUC;

// ---------------------------------------------------------------------------
// Shared table plumbing: EventWrapper{event} and Envelope{Txn{events}}
// ---------------------------------------------------------------------------

static flatbuffers::Offset<void> WrapEvent(
	flatbuffers::FlatBufferBuilder& Builder, uint8 EventType, flatbuffers::uoffset_t EventOff)
{
	const flatbuffers::uoffset_t EwStart = Builder.StartTable();
	Builder.AddElement<uint8_t>(VT::EventWrapper_EventType, EventType, 0);
	Builder.AddOffset(VT::EventWrapper_Event, flatbuffers::Offset<void>(EventOff));
	return flatbuffers::Offset<void>(Builder.EndTable(EwStart));
}

static TArray<uint8> FinishTxnFrame(
	flatbuffers::FlatBufferBuilder& Builder,
	const FString& ClientId,
	const TArray<flatbuffers::Offset<void>>& EwOffsets)
{
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

	return FrameWithLengthPrefix(Builder);
}

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

		EwOffsets.Add(WrapEvent(Builder, kEvSetXformTrs, TrsOff));
	}

	return FinishTxnFrame(Builder, ClientId, EwOffsets);
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

		EwOffsets.Add(WrapEvent(Builder, kEvSetVisibility, VisOff));
	}

	return FinishTxnFrame(Builder, ClientId, EwOffsets);
}

// ---------------------------------------------------------------------------
// Build Envelope { Txn { events: [EventWrapper{SetConnectableInput}, ...] } }
// ---------------------------------------------------------------------------
TArray<uint8> BuildConnectableInputTxnFrame(const FString& ClientId, const TArray<FEmitConnectableInput>& Events)
{
	if (Events.IsEmpty()) return {};

	flatbuffers::FlatBufferBuilder Builder(512 + Events.Num() * 256);

	TArray<flatbuffers::Offset<void>> EwOffsets;
	EwOffsets.Reserve(Events.Num());

	for (const FEmitConnectableInput& Ev : Events)
	{
		TArray<flatbuffers::Offset<void>> InputOffsets;
		InputOffsets.Reserve(Ev.Inputs.Num());

		for (const FEmitConnectableValue& In : Ev.Inputs)
		{
			auto NameStr = Builder.CreateString(TCHAR_TO_UTF8(*In.Name));
			auto TypeStr = Builder.CreateString(TCHAR_TO_UTF8(*In.TypeName));
			flatbuffers::Offset<flatbuffers::String> StrOff;
			flatbuffers::Offset<flatbuffers::Vector<float>> FloatsOff;
			if (In.ValueType == ECivType::String)
			{
				StrOff = Builder.CreateString(TCHAR_TO_UTF8(*In.ScalarString));
			}
			else if (In.ValueType == ECivType::FloatArray)
			{
				FloatsOff = Builder.CreateVector(In.Floats.GetData(), In.Floats.Num());
			}

			const flatbuffers::uoffset_t ValStart = Builder.StartTable();
			Builder.AddOffset(VT::ConnectableInputValue_Name,     NameStr);
			Builder.AddOffset(VT::ConnectableInputValue_TypeName, TypeStr);
			Builder.AddElement<uint8_t>(
				VT::ConnectableInputValue_ValueType, static_cast<uint8>(In.ValueType), 0);
			switch (In.ValueType)
			{
				case ECivType::Float:
					Builder.AddElement<float>(VT::ConnectableInputValue_ScalarFloat, In.ScalarFloat, 0.f);
					break;
				case ECivType::Int:
					Builder.AddElement<int32_t>(VT::ConnectableInputValue_ScalarInt, In.ScalarInt, 0);
					break;
				case ECivType::Bool:
					Builder.AddElement<uint8_t>(VT::ConnectableInputValue_ScalarBool, In.bScalarBool ? 1 : 0, 0);
					break;
				case ECivType::String:
					Builder.AddOffset(VT::ConnectableInputValue_ScalarString, StrOff);
					break;
				case ECivType::FloatArray:
					Builder.AddOffset(VT::ConnectableInputValue_FloatArray, FloatsOff);
					break;
				default:
					break;
			}
			InputOffsets.Add(flatbuffers::Offset<void>(Builder.EndTable(ValStart)));
		}

		auto InputsVec = Builder.CreateVector(InputOffsets.GetData(), InputOffsets.Num());
		auto PrimStr   = Builder.CreateString(TCHAR_TO_UTF8(*Ev.PrimPath));
		auto InfoIdStr = Builder.CreateString(TCHAR_TO_UTF8(*Ev.InfoId));

		const flatbuffers::uoffset_t CiStart = Builder.StartTable();
		Builder.AddOffset(VT::SetConnectableInput_Prim,   PrimStr);
		Builder.AddOffset(VT::SetConnectableInput_InfoId, InfoIdStr);
		Builder.AddOffset(VT::SetConnectableInput_Inputs, InputsVec);
		const flatbuffers::uoffset_t CiOff = Builder.EndTable(CiStart);

		EwOffsets.Add(WrapEvent(Builder, kEvSetConnectableInput, CiOff));
	}

	return FinishTxnFrame(Builder, ClientId, EwOffsets);
}
