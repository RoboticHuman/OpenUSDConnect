#!/usr/bin/env python3
"""Generate the Unreal plugin's FlatBuffers protocol header from .fbs schemas.

The Unreal side decodes FlatBuffers wire frames by hand using vtable offsets
(`4 + 2 * field_index`, with union types taking two slots) and union
discriminant constants. Maintaining these by hand is brittle — any schema
edit silently desyncs the C++ from the wire format.

This script parses ``openusdconnect/schema/{messages,events}.fbs`` and emits
``integrations/unreal/OpenUSDConnect/Source/OpenUSDConnect/Private/USDConnectProtocol.h``
so the C++ stays in lockstep with the schema. The runtime read helpers
(``OUC::FB::*``) are schema-independent and embedded as a fixed prelude.

Usage:
    python scripts/generate_unreal_protocol.py
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# .fbs parsing  (sufficient subset — no codegen-quality parser needed)
# ---------------------------------------------------------------------------
@dataclass
class FbsField:
    name: str
    type_name: str  # raw, e.g. "string", "[float]", "Payload", "ubyte"


@dataclass
class FbsTable:
    name: str
    fields: list[FbsField] = field(default_factory=list)


@dataclass
class FbsUnion:
    name: str
    variants: list[str] = field(default_factory=list)  # in declaration order; index+1 = tag


def _strip_comments(text: str) -> str:
    # Strip line comments only — the .fbs files use // exclusively.
    return re.sub(r"//[^\n]*", "", text)


_TABLE_RE = re.compile(r"\btable\s+(\w+)\s*\{([^}]*)\}", re.DOTALL)
_UNION_RE = re.compile(r"\bunion\s+(\w+)\s*\{([^}]*)\}", re.DOTALL)
_FIELD_RE = re.compile(
    r"(\w+)\s*:\s*"  # field name + ':'
    r"([^;=]+?)"  # type (lazy — anything up to '=' or ';')
    r"(?:\s*=\s*[^;]+?)?"  # optional default
    r"\s*;",
    re.DOTALL,
)


def parse_fbs(path: Path) -> tuple[list[FbsTable], list[FbsUnion]]:
    text = _strip_comments(path.read_text(encoding="utf-8"))
    tables: list[FbsTable] = []
    unions: list[FbsUnion] = []

    for m in _UNION_RE.finditer(text):
        name = m.group(1)
        variants = [
            v.strip() for v in m.group(2).split(",") if v.strip()
        ]
        unions.append(FbsUnion(name=name, variants=variants))

    for m in _TABLE_RE.finditer(text):
        name = m.group(1)
        body = m.group(2)
        tbl = FbsTable(name=name)
        for fm in _FIELD_RE.finditer(body):
            tbl.fields.append(
                FbsField(name=fm.group(1), type_name=fm.group(2).strip())
            )
        tables.append(tbl)

    return tables, unions


# ---------------------------------------------------------------------------
# Codegen
# ---------------------------------------------------------------------------
def to_pascal(name: str) -> str:
    """Convert schema field name (snake_case or camelCase) to PascalCase."""
    if "_" in name:
        return "".join(p[:1].upper() + p[1:] for p in name.split("_") if p)
    return name[:1].upper() + name[1:]


def vtable_offsets(
    table: FbsTable, union_names: set[str]
) -> list[tuple[str, int]]:
    """Return [(ConstantName, VTableByteOffset)] for a table.

    Union-typed fields take two slots: ``<field>_type`` (uint8 discriminant)
    immediately followed by ``<field>`` (offset to the union value).
    """
    out: list[tuple[str, int]] = []
    slot = 0
    for fld in table.fields:
        if fld.type_name in union_names:
            # Discriminant slot
            out.append(
                (
                    f"{table.name}_{to_pascal(fld.name)}Type",
                    4 + 2 * slot,
                )
            )
            slot += 1
            # Value slot
            out.append(
                (f"{table.name}_{to_pascal(fld.name)}", 4 + 2 * slot)
            )
            slot += 1
        else:
            out.append(
                (f"{table.name}_{to_pascal(fld.name)}", 4 + 2 * slot)
            )
            slot += 1
    return out


FB_HELPERS = r"""	// ---------------------------------------------------------------------------
	// FlatBuffers raw-read helpers (header-only, inline).
	// Schema-independent — not regenerated; safe to edit.
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
"""


HEADER_TEMPLATE = """\
// Copyright OpenUSDConnect Contributors. All Rights Reserved.
//
// ============================================================================
//  AUTO-GENERATED FILE — DO NOT EDIT BY HAND.
//  Generated by scripts/generate_unreal_protocol.py from:
//    openusdconnect/schema/messages.fbs
//    openusdconnect/schema/events.fbs
//  Regenerate after schema changes: python scripts/generate_unreal_protocol.py
// ============================================================================
//
// Wire format is decoded by hand without generated FlatBuffers code:
// every table is accessed via its vtable byte offset (VT_*), computed as
// `4 + 2*field_index`, with union types taking two slots: discriminant byte
// first, then pointer.
//
// All identifiers live in the `OUC` namespace (OpenUSDConnect) and use
// `inline constexpr` / inline functions so this header is safe to include
// from multiple .cpp files under Unreal's Unity Build.

#pragma once

#include "CoreMinimal.h"

namespace OUC
{
\tinline constexpr uint32 kMaxFrameSize = 16 * 1024 * 1024;  // 16 MiB

__PAYLOAD_CONSTS__

__EVENT_CONSTS__

\t// ---------------------------------------------------------------------------
\t// Vtable offsets per table
\t// ---------------------------------------------------------------------------
\tnamespace VT
\t{
__VT_CONSTS__
\t} // namespace VT

__FB_HELPERS__
} // namespace OUC
"""


def render_union_consts(
    union: FbsUnion, prefix: str, banner: str
) -> str:
    """Emit `inline constexpr uint8 k{Prefix}{Variant} = N;` lines."""
    lines = [f"\t// ---------------------------------------------------------------------------",
             f"\t// {banner}",
             f"\t// ---------------------------------------------------------------------------"]
    width = max(len(v) for v in union.variants) if union.variants else 0
    for i, variant in enumerate(union.variants, start=1):
        name = f"k{prefix}{variant}".ljust(len("kPayload") + width)
        lines.append(f"\tinline constexpr uint8 {name} = {i};")
    return "\n".join(lines)


def render_vt_block(tables: list[FbsTable], union_names: set[str]) -> str:
    """Emit the full VT namespace body."""
    blocks: list[str] = []
    for tbl in tables:
        if not tbl.fields:
            continue
        # Skip tables that exist only as union-payload markers with no fields —
        # `Resync`, `Compact`, `Ping`, `Quit` (all empty).
        consts = vtable_offsets(tbl, union_names)
        # Pretty-align the names.
        width = max(len(n) for n, _ in consts) if consts else 0
        block = [
            f"\t\t// {tbl.name} {{ "
            + "; ".join(f"{f.name}:{f.type_name}" for f in tbl.fields)
            + " }"
        ]
        for name, off in consts:
            block.append(
                f"\t\tinline constexpr uint16 {name.ljust(width)} = {off};"
            )
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def render_header(schema_dir: Path) -> str:
    msgs_tables, msgs_unions = parse_fbs(schema_dir / "messages.fbs")
    events_tables, events_unions = parse_fbs(schema_dir / "events.fbs")
    all_tables = events_tables + msgs_tables  # events first (referenced by messages)
    all_unions = events_unions + msgs_unions
    union_names = {u.name for u in all_unions}

    # Discriminant constants — find the two top-level unions.
    payload_union = next(u for u in msgs_unions if u.name == "Payload")
    event_union = next(u for u in events_unions if u.name == "EventPayload")

    payload_consts = render_union_consts(
        payload_union, prefix="Payload",
        banner="Envelope.payload union discriminants (messages.fbs)"
    )
    event_consts = render_union_consts(
        event_union, prefix="Ev",
        banner="EventWrapper.event union discriminants (events.fbs)"
    )

    vt_consts = render_vt_block(all_tables, union_names)

    return (
        HEADER_TEMPLATE
        .replace("__PAYLOAD_CONSTS__", payload_consts)
        .replace("__EVENT_CONSTS__", event_consts)
        .replace("__VT_CONSTS__", vt_consts)
        .replace("__FB_HELPERS__", FB_HELPERS)
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    schema_dir = repo_root / "openusdconnect" / "schema"
    out_path = (
        repo_root
        / "integrations"
        / "unreal"
        / "OpenUSDConnect"
        / "Source"
        / "OpenUSDConnect"
        / "Private"
        / "USDConnectProtocol.h"
    )

    if not schema_dir.is_dir():
        print(f"error: schema directory not found: {schema_dir}", file=sys.stderr)
        return 1

    header = render_header(schema_dir)
    out_path.write_text(header, encoding="utf-8", newline="\n")
    print(f"wrote {out_path.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
