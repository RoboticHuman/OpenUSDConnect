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

# Unreal C++ bindings — one self-contained header, committed alongside the
# plugin. The generated code pins the flatc runtime version (static_assert);
# keep setup_flatbuffers.py's DEFAULT_VERSION in lockstep with the flatc
# used here.
UE_SCHEMA_DIR="$ROOT/integrations/unreal/OpenUSDConnect/Source/OpenUSDConnectPXR/Public/Schema"
flatc --cpp --gen-all --cpp-std c++17 \
    -I "$SCHEMA_DIR" \
    -o "$UE_SCHEMA_DIR" \
    "$SCHEMA_DIR/messages.fbs"

echo "Unreal C++ bindings generated: $UE_SCHEMA_DIR/messages_generated.h (flatc $(flatc --version | grep -o '[0-9.]*'))"
