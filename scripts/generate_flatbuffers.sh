#!/usr/bin/env bash
# Generate FlatBuffers bindings from .fbs schemas.
# Requires: flatc (scoop install main/flatc)
#
# Usage: bash scripts/generate_flatbuffers.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
SCHEMA_DIR="$ROOT/openusdconnect/schema"
OUT_DIR="$ROOT/openusdconnect/generated"

# Python — single file, all types in one module
rm -f "$OUT_DIR/messages_generated.py"
rm -rf "$OUT_DIR/OpenUSDConnect"

flatc --python --gen-all --gen-onefile \
    -I "$SCHEMA_DIR" \
    -o "$OUT_DIR" \
    "$SCHEMA_DIR/messages.fbs"

echo "Python bindings generated: $OUT_DIR/messages_generated.py"

# Unreal C++ protocol header (hand-rolled vtable decoding — see script docstring)
python "$SCRIPT_DIR/generate_unreal_protocol.py"

# Uncomment to generate other languages:
# flatc --cpp --gen-all -I "$SCHEMA_DIR" -o "$OUT_DIR/cpp" "$SCHEMA_DIR/messages.fbs"
# flatc --csharp --gen-all -I "$SCHEMA_DIR" -o "$OUT_DIR/csharp" "$SCHEMA_DIR/messages.fbs"
# flatc --rust --gen-all -I "$SCHEMA_DIR" -o "$OUT_DIR/rust" "$SCHEMA_DIR/messages.fbs"
