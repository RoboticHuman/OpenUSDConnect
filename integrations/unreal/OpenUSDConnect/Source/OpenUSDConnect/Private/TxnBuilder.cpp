// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "TxnBuilder.h"
#include "USDConnectProtocol.h"
#include "USDWireFraming.h"

using namespace OUC;

// ---------------------------------------------------------------------------
// Shared: Envelope{Txn{events}} wrapping
// ---------------------------------------------------------------------------

static openusdconnect::client::FrameResult
FinishTxnFrame(flatbuffers::FlatBufferBuilder& Builder, uint64 TxnId,
			   const TArray<flatbuffers::Offset<OpenUSDConnect::EventWrapper>>& Events,
			   FWireFrame& OutFrame)
{
	const openusdconnect::client::ProtocolResult Result =
		openusdconnect::client::FinishTransactionFrame(Builder, TxnId, Events.GetData(),
													   static_cast<size_t>(Events.Num()));
	if (Result != openusdconnect::client::ProtocolResult::Success)
	{
		OutFrame = FWireFrame();
		return ToFrameResult(Result);
	}
	OutFrame = FWireFrame(Builder.Release());
	return openusdconnect::client::FrameResult::Success;
}

// ---------------------------------------------------------------------------
// Build Envelope { Txn { events: [EventWrapper{SetXformTrs}, ...] } }
// ---------------------------------------------------------------------------
openusdconnect::client::FrameResult BuildXformTxnFrame(uint64 TxnId,
													   const TArray<FEmitXformTrs>& Xforms,
													   FWireFrame& OutFrame,
													   bool bIncludeEnsureXformOps)
{
	OutFrame = FWireFrame();
	if (Xforms.IsEmpty())
		return openusdconnect::client::FrameResult::EmptyPayload;

	flatbuffers::FlatBufferBuilder Builder(512 +
										   Xforms.Num() * (bIncludeEnsureXformOps ? 192 : 128));

	TArray<flatbuffers::Offset<OpenUSDConnect::EventWrapper>> Events;
	Events.Reserve(Xforms.Num() * (bIncludeEnsureXformOps ? 2 : 1));

	for (const FEmitXformTrs& X : Xforms)
	{
		const FTCHARToUTF8 PrimUtf8(*X.PrimPath);
		const auto Prim = openusdconnect::client::CreateString(Builder, ToStringView(PrimUtf8));
		if (bIncludeEnsureXformOps)
		{
			flatbuffers::Offset<OpenUSDConnect::EventWrapper> Ensure;
			const openusdconnect::client::ProtocolResult Result =
				openusdconnect::client::BuildEnsureXformOpsEvent(Builder, Prim, Ensure);
			if (Result != openusdconnect::client::ProtocolResult::Success)
			{
				return ToFrameResult(Result);
			}
			Events.Add(Ensure);
		}

		const openusdconnect::client::XformTrsEventView View{ToStringView(PrimUtf8), X.T, X.R, X.S,
															 X.Fields};
		flatbuffers::Offset<OpenUSDConnect::EventWrapper> Event;
		const openusdconnect::client::ProtocolResult Result =
			openusdconnect::client::BuildXformTrsEvent(Builder, View, Prim, Event);
		if (Result != openusdconnect::client::ProtocolResult::Success)
		{
			return ToFrameResult(Result);
		}
		Events.Add(Event);
	}

	return FinishTxnFrame(Builder, TxnId, Events, OutFrame);
}

// ---------------------------------------------------------------------------
// Build Envelope { Txn { events: [EventWrapper{SetVisibility}, ...] } }
// ---------------------------------------------------------------------------
openusdconnect::client::FrameResult
BuildVisibilityTxnFrame(uint64 TxnId, const TArray<FEmitVisibility>& Visibilities,
						FWireFrame& OutFrame)
{
	OutFrame = FWireFrame();
	if (Visibilities.IsEmpty())
		return openusdconnect::client::FrameResult::EmptyPayload;

	flatbuffers::FlatBufferBuilder Builder(256 + Visibilities.Num() * 64);

	TArray<flatbuffers::Offset<OpenUSDConnect::EventWrapper>> Events;
	Events.Reserve(Visibilities.Num());

	for (const FEmitVisibility& V : Visibilities)
	{
		const FTCHARToUTF8 PrimUtf8(*V.PrimPath);
		const openusdconnect::client::VisibilityEventView View{ToStringView(PrimUtf8), V.bVisible};
		flatbuffers::Offset<OpenUSDConnect::EventWrapper> Event;
		const openusdconnect::client::ProtocolResult Result =
			openusdconnect::client::BuildVisibilityEvent(Builder, View, Event);
		if (Result != openusdconnect::client::ProtocolResult::Success)
		{
			return ToFrameResult(Result);
		}
		Events.Add(Event);
	}

	return FinishTxnFrame(Builder, TxnId, Events, OutFrame);
}

// ---------------------------------------------------------------------------
// Build Envelope { Txn { events: [EventWrapper{SetConnectableInput}, ...] } }
// ---------------------------------------------------------------------------
openusdconnect::client::FrameResult
BuildConnectableInputTxnFrame(uint64 TxnId, const TArray<FEmitConnectableInput>& InEvents,
							  FWireFrame& OutFrame)
{
	OutFrame = FWireFrame();
	if (InEvents.IsEmpty())
		return openusdconnect::client::FrameResult::EmptyPayload;

	flatbuffers::FlatBufferBuilder Builder(512 + InEvents.Num() * 256);

	TArray<flatbuffers::Offset<OpenUSDConnect::EventWrapper>> Events;
	Events.Reserve(InEvents.Num());

	for (const FEmitConnectableInput& Ev : InEvents)
	{
		TArray<flatbuffers::Offset<OpenUSDConnect::ConnectableInputValue>> Inputs;
		Inputs.Reserve(Ev.Inputs.Num());

		for (const FEmitConnectableValue& In : Ev.Inputs)
		{
			const FTCHARToUTF8 NameUtf8(*In.Name);
			const FTCHARToUTF8 TypeUtf8(*In.TypeName);
			const FTCHARToUTF8 ScalarStringUtf8(*In.ScalarString);
			const openusdconnect::client::ConnectableInputValueView View{
				ToStringView(NameUtf8),
				ToStringView(TypeUtf8),
				In.ValueType,
				In.ScalarFloat,
				In.ScalarInt,
				In.bScalarBool,
				ToStringView(ScalarStringUtf8),
				In.Floats.GetData(),
				static_cast<size_t>(In.Floats.Num()),
			};
			flatbuffers::Offset<OpenUSDConnect::ConnectableInputValue> Input;
			const openusdconnect::client::ProtocolResult Result =
				openusdconnect::client::BuildConnectableInputValue(Builder, View, Input);
			if (Result != openusdconnect::client::ProtocolResult::Success)
			{
				return ToFrameResult(Result);
			}
			Inputs.Add(Input);
		}

		const FTCHARToUTF8 PrimUtf8(*Ev.PrimPath);
		const FTCHARToUTF8 InfoIdUtf8(*Ev.InfoId);
		flatbuffers::Offset<OpenUSDConnect::EventWrapper> Event;
		const openusdconnect::client::ProtocolResult Result =
			openusdconnect::client::BuildConnectableInputEvent(
				Builder, ToStringView(PrimUtf8), ToStringView(InfoIdUtf8), Inputs.GetData(),
				static_cast<size_t>(Inputs.Num()), Event);
		if (Result != openusdconnect::client::ProtocolResult::Success)
		{
			return ToFrameResult(Result);
		}
		Events.Add(Event);
	}

	return FinishTxnFrame(Builder, TxnId, Events, OutFrame);
}
