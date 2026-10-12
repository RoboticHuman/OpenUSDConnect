// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "TxnBuilder.h"

using openusdconnect::client::ProtocolResult;

static std::string_view ToStringView(const FTCHARToUTF8& Value)
{
	return {Value.Get(), static_cast<size_t>(Value.Length())};
}

// ---------------------------------------------------------------------------
// Shared: Envelope{Txn{events}} wrapping
// ---------------------------------------------------------------------------

static ProtocolResult
FinishTxnFrame(flatbuffers::FlatBufferBuilder& Builder, uint64 TxnId,
			   const TArray<flatbuffers::Offset<OpenUSDConnect::EventWrapper>>& Events,
			   std::vector<uint8>& OutFrame)
{
	const ProtocolResult Result = openusdconnect::client::FinishTransactionFrame(
		Builder, TxnId, Events.GetData(), static_cast<size_t>(Events.Num()));
	if (Result == ProtocolResult::Success)
	{
		const uint8* Bytes = Builder.GetBufferPointer();
		OutFrame.assign(Bytes, Bytes + Builder.GetSize());
	}
	return Result;
}

// ---------------------------------------------------------------------------
// Build Envelope { Txn { events: [EventWrapper{SetXformTrs}, ...] } }
// ---------------------------------------------------------------------------
ProtocolResult BuildXformTxnFrame(uint64 TxnId, const TArray<FEmitXformTrs>& Xforms,
								  std::vector<uint8>& OutFrame, bool bIncludeEnsureXformOps)
{
	if (Xforms.IsEmpty())
		return ProtocolResult::EmptyTransaction;

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
			const ProtocolResult Result =
				openusdconnect::client::BuildEnsureXformOpsEvent(Builder, Prim, Ensure);
			if (Result != ProtocolResult::Success)
			{
				return Result;
			}
			Events.Add(Ensure);
		}

		const openusdconnect::client::XformTrsEventView View{ToStringView(PrimUtf8), X.T, X.R, X.S,
															 X.Fields};
		flatbuffers::Offset<OpenUSDConnect::EventWrapper> Event;
		const ProtocolResult Result =
			openusdconnect::client::BuildXformTrsEvent(Builder, View, Prim, Event);
		if (Result != ProtocolResult::Success)
		{
			return Result;
		}
		Events.Add(Event);
	}

	return FinishTxnFrame(Builder, TxnId, Events, OutFrame);
}

// ---------------------------------------------------------------------------
// Build Envelope { Txn { events: [EventWrapper{SetVisibility}, ...] } }
// ---------------------------------------------------------------------------
ProtocolResult BuildVisibilityTxnFrame(uint64 TxnId, const TArray<FEmitVisibility>& Visibilities,
									   std::vector<uint8>& OutFrame)
{
	if (Visibilities.IsEmpty())
		return ProtocolResult::EmptyTransaction;

	flatbuffers::FlatBufferBuilder Builder(256 + Visibilities.Num() * 64);

	TArray<flatbuffers::Offset<OpenUSDConnect::EventWrapper>> Events;
	Events.Reserve(Visibilities.Num());

	for (const FEmitVisibility& V : Visibilities)
	{
		const FTCHARToUTF8 PrimUtf8(*V.PrimPath);
		const openusdconnect::client::VisibilityEventView View{ToStringView(PrimUtf8), V.bVisible};
		flatbuffers::Offset<OpenUSDConnect::EventWrapper> Event;
		const ProtocolResult Result =
			openusdconnect::client::BuildVisibilityEvent(Builder, View, Event);
		if (Result != ProtocolResult::Success)
		{
			return Result;
		}
		Events.Add(Event);
	}

	return FinishTxnFrame(Builder, TxnId, Events, OutFrame);
}

// ---------------------------------------------------------------------------
// Build Envelope { Txn { events: [EventWrapper{SetConnectableInput}, ...] } }
// ---------------------------------------------------------------------------
ProtocolResult BuildConnectableInputTxnFrame(uint64 TxnId,
											 const TArray<FEmitConnectableInput>& InEvents,
											 std::vector<uint8>& OutFrame)
{
	if (InEvents.IsEmpty())
		return ProtocolResult::EmptyTransaction;

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
			const ProtocolResult Result =
				openusdconnect::client::BuildConnectableInputValue(Builder, View, Input);
			if (Result != ProtocolResult::Success)
			{
				return Result;
			}
			Inputs.Add(Input);
		}

		const FTCHARToUTF8 PrimUtf8(*Ev.PrimPath);
		const FTCHARToUTF8 InfoIdUtf8(*Ev.InfoId);
		flatbuffers::Offset<OpenUSDConnect::EventWrapper> Event;
		const ProtocolResult Result = openusdconnect::client::BuildConnectableInputEvent(
			Builder, ToStringView(PrimUtf8), ToStringView(InfoIdUtf8), Inputs.GetData(),
			static_cast<size_t>(Inputs.Num()), Event);
		if (Result != ProtocolResult::Success)
		{
			return Result;
		}
		Events.Add(Event);
	}

	return FinishTxnFrame(Builder, TxnId, Events, OutFrame);
}
