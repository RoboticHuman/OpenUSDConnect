// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "TxnBuilder.h"
#include "USDConnectProtocol.h"
#include "USDWireFraming.h"

using namespace OUC;

// ---------------------------------------------------------------------------
// Shared: Envelope{Txn{events}} wrapping
// ---------------------------------------------------------------------------

static TArray<uint8> FinishTxnFrame(
	flatbuffers::FlatBufferBuilder& Builder,
	uint64 TxnId,
	const std::vector<flatbuffers::Offset<OpenUSDConnect::EventWrapper>>& Events)
{
	const auto Txn = OpenUSDConnect::CreateTxn(
		Builder,
		Builder.CreateVector(Events),
		0,
		0,
		TxnId);

	Builder.Finish(OpenUSDConnect::CreateEnvelope(
		Builder,
		OpenUSDConnect::Payload::Txn,
		Txn.Union(),
		kSchemaVersion));

	return FrameWithLengthPrefix(Builder);
}

// ---------------------------------------------------------------------------
// Build Envelope { Txn { events: [EventWrapper{SetXformTrs}, ...] } }
// ---------------------------------------------------------------------------
TArray<uint8> BuildXformTxnFrame(
	uint64 TxnId,
	const TArray<FEmitXformTrs>& Xforms,
	bool bIncludeEnsureXformOps)
{
	if (Xforms.IsEmpty()) return {};

	flatbuffers::FlatBufferBuilder Builder(
		512 + Xforms.Num() * (bIncludeEnsureXformOps ? 192 : 128));

	std::vector<flatbuffers::Offset<OpenUSDConnect::EventWrapper>> Events;
	Events.reserve(Xforms.Num() * (bIncludeEnsureXformOps ? 2 : 1));

	for (const FEmitXformTrs& X : Xforms)
	{
		const auto Prim = Builder.CreateString(TCHAR_TO_UTF8(*X.PrimPath));
		if (bIncludeEnsureXformOps)
		{
			const auto Ensure = OpenUSDConnect::CreateEnsureXformOps(Builder, Prim);
			Events.push_back(OpenUSDConnect::CreateEventWrapper(
				Builder,
				OpenUSDConnect::EventPayload::EnsureXformOps,
				Ensure.Union()));
		}

		const auto Trs = OpenUSDConnect::CreateSetXformTrs(
			Builder,
			Prim,
			X.Fields,
			Builder.CreateVector(X.T, 3),
			Builder.CreateVector(X.R, 4),
			Builder.CreateVector(X.S, 3));

		Events.push_back(OpenUSDConnect::CreateEventWrapper(
			Builder, OpenUSDConnect::EventPayload::SetXformTrs, Trs.Union()));
	}

	return FinishTxnFrame(Builder, TxnId, Events);
}

// ---------------------------------------------------------------------------
// Build Envelope { Txn { events: [EventWrapper{SetVisibility}, ...] } }
// ---------------------------------------------------------------------------
TArray<uint8> BuildVisibilityTxnFrame(
	uint64 TxnId,
	const TArray<FEmitVisibility>& Visibilities)
{
	if (Visibilities.IsEmpty()) return {};

	flatbuffers::FlatBufferBuilder Builder(256 + Visibilities.Num() * 64);

	std::vector<flatbuffers::Offset<OpenUSDConnect::EventWrapper>> Events;
	Events.reserve(Visibilities.Num());

	for (const FEmitVisibility& V : Visibilities)
	{
		const auto Vis = OpenUSDConnect::CreateSetVisibility(
			Builder,
			Builder.CreateString(TCHAR_TO_UTF8(*V.PrimPath)),
			V.bVisible);

		Events.push_back(OpenUSDConnect::CreateEventWrapper(
			Builder, OpenUSDConnect::EventPayload::SetVisibility, Vis.Union()));
	}

	return FinishTxnFrame(Builder, TxnId, Events);
}

// ---------------------------------------------------------------------------
// Build Envelope { Txn { events: [EventWrapper{SetConnectableInput}, ...] } }
// ---------------------------------------------------------------------------
TArray<uint8> BuildConnectableInputTxnFrame(
	uint64 TxnId,
	const TArray<FEmitConnectableInput>& InEvents)
{
	if (InEvents.IsEmpty()) return {};

	flatbuffers::FlatBufferBuilder Builder(512 + InEvents.Num() * 256);

	std::vector<flatbuffers::Offset<OpenUSDConnect::EventWrapper>> Events;
	Events.reserve(InEvents.Num());

	for (const FEmitConnectableInput& Ev : InEvents)
	{
		std::vector<flatbuffers::Offset<OpenUSDConnect::ConnectableInputValue>> Inputs;
		Inputs.reserve(Ev.Inputs.Num());

		for (const FEmitConnectableValue& In : Ev.Inputs)
		{
			using OpenUSDConnect::ConnectableInputValueType;

			const auto NameStr = Builder.CreateString(TCHAR_TO_UTF8(*In.Name));
			const auto TypeStr = Builder.CreateString(TCHAR_TO_UTF8(*In.TypeName));
			flatbuffers::Offset<flatbuffers::String> StrOff;
			flatbuffers::Offset<flatbuffers::Vector<float>> FloatsOff;
			if (In.ValueType == ConnectableInputValueType::ScalarString)
			{
				StrOff = Builder.CreateString(TCHAR_TO_UTF8(*In.ScalarString));
			}
			else if (In.ValueType == ConnectableInputValueType::FloatArray)
			{
				FloatsOff = Builder.CreateVector(In.Floats.GetData(), In.Floats.Num());
			}

			OpenUSDConnect::ConnectableInputValueBuilder VB(Builder);
			VB.add_name(NameStr);
			VB.add_type_name(TypeStr);
			VB.add_value_type(In.ValueType);
			switch (In.ValueType)
			{
				case ConnectableInputValueType::ScalarFloat:  VB.add_scalar_float(In.ScalarFloat); break;
				case ConnectableInputValueType::ScalarInt:    VB.add_scalar_int(In.ScalarInt);     break;
				case ConnectableInputValueType::ScalarBool:   VB.add_scalar_bool(In.bScalarBool);  break;
				case ConnectableInputValueType::ScalarString: VB.add_scalar_string(StrOff);        break;
				case ConnectableInputValueType::FloatArray:   VB.add_float_array(FloatsOff);       break;
				default:                                                                           break;
			}
			Inputs.push_back(VB.Finish());
		}

		const auto Ci = OpenUSDConnect::CreateSetConnectableInput(
			Builder,
			Builder.CreateString(TCHAR_TO_UTF8(*Ev.PrimPath)),
			Builder.CreateString(TCHAR_TO_UTF8(*Ev.InfoId)),
			Builder.CreateVector(Inputs));

		Events.push_back(OpenUSDConnect::CreateEventWrapper(
			Builder, OpenUSDConnect::EventPayload::SetConnectableInput, Ci.Union()));
	}

	return FinishTxnFrame(Builder, TxnId, Events);
}
