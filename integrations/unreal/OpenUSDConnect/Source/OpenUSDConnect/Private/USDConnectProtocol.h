// Copyright OpenUSDConnect Contributors. All Rights Reserved.
//
// Shared protocol constants and FlatBuffers raw-read helpers.
//
// All identifiers live in the `OUC` namespace (OpenUSDConnect) and use
// `inline constexpr` / inline functions so this header is safe to include
// from multiple .cpp files even under Unreal's Unity Build (which concatenates
// translation units and would otherwise produce redefinition errors).
//
// The FlatBuffers wire-format is decoded by hand without generated code:
// every event/message table is accessed via its vtable byte offset (VT_*).
// Each VT_* is the position of that field's offset in the table's vtable,
// computed as `4 + 2*field_index` (with union types taking two slots:
// type discriminant byte first, then pointer).

#pragma once

#include "CoreMinimal.h"

namespace OUC
{
	// ---------------------------------------------------------------------------
	// Wire constants
	// ---------------------------------------------------------------------------
	inline constexpr uint32 kMaxFrameSize = 16 * 1024 * 1024;  // 16 MiB

	// ---------------------------------------------------------------------------
	// Envelope.payload_type union discriminant values (messages.fbs)
	// ---------------------------------------------------------------------------
	inline constexpr uint8 kPayloadHello          = 1;
	inline constexpr uint8 kPayloadHelloOk        = 2;
	inline constexpr uint8 kPayloadAuthRejected   = 3;
	inline constexpr uint8 kPayloadTxn            = 4;
	inline constexpr uint8 kPayloadBroadcastEvent = 5;
	inline constexpr uint8 kPayloadResync         = 6;
	inline constexpr uint8 kPayloadCompact        = 7;
	inline constexpr uint8 kPayloadPing           = 8;
	inline constexpr uint8 kPayloadQuit           = 9;
	inline constexpr uint8 kPayloadRateLimited    = 12;

	// ---------------------------------------------------------------------------
	// EventPayload union discriminant values (events.fbs)
	// ---------------------------------------------------------------------------
	inline constexpr uint8 kEvEnsurePrim           = 1;
	inline constexpr uint8 kEvEnsureXformOps       = 2;
	inline constexpr uint8 kEvSetXformTrs          = 3;
	inline constexpr uint8 kEvDeletePrim           = 4;
	inline constexpr uint8 kEvDeactivatePrim       = 5;
	inline constexpr uint8 kEvRenamePrim           = 6;
	inline constexpr uint8 kEvSetVisibility        = 7;
	inline constexpr uint8 kEvSetGprimAttrs        = 8;
	inline constexpr uint8 kEvSetReference         = 9;
	inline constexpr uint8 kEvSetPayload           = 10;
	inline constexpr uint8 kEvLoadPayload          = 11;
	inline constexpr uint8 kEvUnloadPayload        = 12;
	inline constexpr uint8 kEvSetVariantSelections = 13;
	inline constexpr uint8 kEvSetMaterialBinding   = 14;
	inline constexpr uint8 kEvSetConnectableInput  = 15;
	inline constexpr uint8 kEvSetConnectableConn   = 16;
	inline constexpr uint8 kEvSetStageMetadata     = 17;

	// ---------------------------------------------------------------------------
	// Vtable offsets (per table)
	// ---------------------------------------------------------------------------
	namespace VT
	{
		// Envelope {payload:Payload union; schema_version:uint16=1}
		inline constexpr uint16 EnvPayloadType  = 4;
		inline constexpr uint16 EnvPayload      = 6;
		inline constexpr uint16 EnvSchemaVer    = 8;

		// Hello {role; protocol_version; sync_from; client_id; origin; department; token}
		inline constexpr uint16 HelloRole       = 4;
		inline constexpr uint16 HelloProtoVer   = 6;
		inline constexpr uint16 HelloSyncFrom   = 8;
		inline constexpr uint16 HelloClientId   = 10;
		inline constexpr uint16 HelloOrigin     = 12;
		inline constexpr uint16 HelloDepartment = 14;
		inline constexpr uint16 HelloToken      = 16;

		// HelloOk {token; stage_metadata}
		inline constexpr uint16 HelloOkToken    = 4;
		inline constexpr uint16 HelloOkStageMd  = 6;

		// BroadcastEvent {seq; event; origin; client_id; client}
		inline constexpr uint16 BcSeq           = 4;
		inline constexpr uint16 BcEvent         = 6;
		inline constexpr uint16 BcOrigin        = 8;
		inline constexpr uint16 BcClientId      = 10;
		inline constexpr uint16 BcClient        = 12;

		// Txn {client_id; events:[EventWrapper]}
		inline constexpr uint16 TxnClientId     = 4;
		inline constexpr uint16 TxnEvents       = 6;

		// EventWrapper {event:EventPayload union}
		inline constexpr uint16 EwType          = 4;
		inline constexpr uint16 EwEvent         = 6;

		// RateLimited {retry_after:float}
		inline constexpr uint16 RLRetryAfter    = 4;

		// EnsurePrim {prim; type_name; api_schemas:[string]}
		inline constexpr uint16 EnsurePrim_Prim     = 4;
		inline constexpr uint16 EnsurePrim_TypeName = 6;
		inline constexpr uint16 EnsurePrim_ApiSchemas = 8;

		// Single-string-field tables: EnsureXformOps, DeletePrim, LoadPayload, UnloadPayload
		inline constexpr uint16 SingleField_Prim = 4;

		// SetXformTrs {prim; fields:uint8(1=T,2=R,4=S); t[3]; r[4]; s[3]; time=null}
		inline constexpr uint16 Trs_Prim   = 4;
		inline constexpr uint16 Trs_Fields = 6;
		inline constexpr uint16 Trs_T      = 8;
		inline constexpr uint16 Trs_R      = 10;
		inline constexpr uint16 Trs_S      = 12;
		inline constexpr uint16 Trs_Time   = 14;

		// DeactivatePrim {prim; active=true}
		inline constexpr uint16 Da_Prim   = 4;
		inline constexpr uint16 Da_Active = 6;

		// RenamePrim {prim; new_name}
		inline constexpr uint16 Rn_Prim    = 4;
		inline constexpr uint16 Rn_NewName = 6;

		// SetVisibility {prim; visible; time=null}
		inline constexpr uint16 Vis_Prim    = 4;
		inline constexpr uint16 Vis_Visible = 6;
		inline constexpr uint16 Vis_Time    = 8;

		// SetStageMetadata {timeCodesPerSecond; framesPerSecond; startTimeCode; endTimeCode; metersPerUnit; upAxis}
		inline constexpr uint16 Sm_TCPS   = 4;
		inline constexpr uint16 Sm_FPS    = 6;
		inline constexpr uint16 Sm_Start  = 8;
		inline constexpr uint16 Sm_End    = 10;
		inline constexpr uint16 Sm_MPU    = 12;
		inline constexpr uint16 Sm_UpAxis = 14;

		// SetReference {prim; refs:[ArcEntry]} / SetPayload {prim; payloads:[ArcEntry]}
		inline constexpr uint16 Ref_Prim = 4;
		inline constexpr uint16 Ref_Refs = 6;

		// ArcEntry {asset_path; prim_path}
		inline constexpr uint16 Arc_AssetPath = 4;
		inline constexpr uint16 Arc_PrimPath  = 6;

		// SetVariantSelections {prim; selections:[StringPair]}
		inline constexpr uint16 Vs_Prim       = 4;
		inline constexpr uint16 Vs_Selections = 6;

		// StringPair {key; value}
		inline constexpr uint16 Sp_Key   = 4;
		inline constexpr uint16 Sp_Value = 6;

		// SetMaterialBinding {prim; material_path}
		inline constexpr uint16 Mb_Prim         = 4;
		inline constexpr uint16 Mb_MaterialPath = 6;
	}

	// ---------------------------------------------------------------------------
	// FlatBuffers raw-read helpers (header-only, inline)
	// ---------------------------------------------------------------------------
	namespace FB
	{
		// Read a little-endian scalar of type T from byte pointer P.
		// FlatBuffers stores all data little-endian; x86/ARM64 are LE so memcpy works.
		template<typename T>
		FORCEINLINE T ReadLE(const uint8* P)
		{
			T V;
			FMemory::Memcpy(&V, P, sizeof(T));
			return V;
		}

		// Returns the byte offset of field VtOff in Table's vtable (or 0 if field absent).
		FORCEINLINE uint16 FieldOff(const uint8* Table, uint16 VtOff)
		{
			const int32  Soff   = ReadLE<int32>(Table);
			const uint8* Vtable = Table - Soff;
			const uint16 VtSize = ReadLE<uint16>(Vtable);
			if (VtOff >= VtSize) return 0;
			return ReadLE<uint16>(Vtable + VtOff);
		}

		// True iff the field is present in the table's vtable.
		FORCEINLINE bool HasField(const uint8* Table, uint16 VtOff)
		{
			return FieldOff(Table, VtOff) != 0;
		}

		// Read a scalar field; returns Def if field is absent.
		template<typename T>
		FORCEINLINE T GetField(const uint8* Table, uint16 VtOff, T Def = T{})
		{
			const uint16 Off = FieldOff(Table, VtOff);
			return Off ? ReadLE<T>(Table + Off) : Def;
		}

		// Dereference an offset field (string / table / vector pointer).
		// FlatBuffers offsets are relative to the position of the offset itself.
		FORCEINLINE const uint8* GetPtr(const uint8* Table, uint16 VtOff)
		{
			const uint16 Off = FieldOff(Table, VtOff);
			if (!Off) return nullptr;
			const uint8* FldPtr = Table + Off;
			const uint32 Ref    = ReadLE<uint32>(FldPtr);
			return FldPtr + Ref;
		}

		// Decode a FlatBuffers string field into an FString (UTF-8 → TCHAR).
		FORCEINLINE FString GetStr(const uint8* Table, uint16 VtOff)
		{
			const uint8* Str = GetPtr(Table, VtOff);
			if (!Str) return {};
			const uint32 Len = ReadLE<uint32>(Str);
			return FString(static_cast<int32>(Len),
			               reinterpret_cast<const ANSICHAR*>(Str + 4));
		}

		// Read a [float] vector field into a TArray<float>.
		FORCEINLINE TArray<float> GetFloatVec(const uint8* Table, uint16 VtOff)
		{
			const uint8* Vec = GetPtr(Table, VtOff);
			if (!Vec) return {};
			const uint32 Size = ReadLE<uint32>(Vec);
			TArray<float> Out;
			Out.SetNumUninitialized(static_cast<int32>(Size));
			FMemory::Memcpy(Out.GetData(), Vec + 4, Size * sizeof(float));
			return Out;
		}

		// Number of elements in a vector field (0 if absent).
		FORCEINLINE uint32 GetVecSize(const uint8* Table, uint16 VtOff)
		{
			const uint8* Vec = GetPtr(Table, VtOff);
			return Vec ? ReadLE<uint32>(Vec) : 0;
		}

		// Get the ith element of a vector-of-offsets (vector of tables/strings).
		FORCEINLINE const uint8* GetVecElem(const uint8* Table, uint16 VtOff, uint32 Index)
		{
			const uint8* Vec = GetPtr(Table, VtOff);
			if (!Vec) return nullptr;
			const uint8* ElemPtr = Vec + 4 + Index * 4;
			const uint32 Off     = ReadLE<uint32>(ElemPtr);
			return ElemPtr + Off;
		}

		// Convenience: read the ith string from a [string] vector field.
		FORCEINLINE FString GetStrVecElem(const uint8* Table, uint16 VtOff, uint32 Index)
		{
			const uint8* Str = GetVecElem(Table, VtOff, Index);
			if (!Str) return {};
			const uint32 Len = ReadLE<uint32>(Str);
			return FString(static_cast<int32>(Len),
			               reinterpret_cast<const ANSICHAR*>(Str + 4));
		}

		// Decode the root table pointer of an Envelope from raw frame bytes.
		FORCEINLINE const uint8* GetRoot(const TArray<uint8>& Buf)
		{
			if (Buf.Num() < 4) return nullptr;
			const uint32 Off = ReadLE<uint32>(Buf.GetData());
			return Buf.GetData() + Off;
		}
	} // namespace FB
} // namespace OUC
