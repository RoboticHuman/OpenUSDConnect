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

if command -v uv >/dev/null 2>&1; then
    PYTHON=(uv run python)
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=(python3)
elif command -v python >/dev/null 2>&1; then
    PYTHON=(python)
else
    echo "error: Python 3 is required to read the FlatBuffers pin" >&2
    exit 1
fi

EXPECTED_FLATBUFFERS_VERSION="$("${PYTHON[@]}" - "$ROOT/pyproject.toml" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    dependencies = tomllib.load(stream)["project"]["dependencies"]
pins = [item.split("==", 1)[1] for item in dependencies if item.startswith("flatbuffers==")]
if len(pins) != 1:
    raise SystemExit("pyproject.toml must contain one exact flatbuffers pin")
print(pins[0])
PY
)"
ACTUAL_FLATBUFFERS_VERSION="$(flatc --version | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)"
if [[ "$ACTUAL_FLATBUFFERS_VERSION" != "$EXPECTED_FLATBUFFERS_VERSION" ]]; then
    echo "error: flatc $EXPECTED_FLATBUFFERS_VERSION is required; found $ACTUAL_FLATBUFFERS_VERSION" >&2
    exit 1
fi

# Python single file, all types in one module
rm -f "$OUT_DIR/messages_generated.py"
rm -rf "$OUT_DIR/OpenUSDConnect"

flatc --python --gen-all --gen-onefile \
    -I "$SCHEMA_DIR" \
    -o "$OUT_DIR" \
    "$SCHEMA_DIR/messages.fbs"

echo "Python bindings generated: $OUT_DIR/messages_generated.py"

# Unreal C++ bindings one self-contained header, committed alongside the
# plugin. The generated code pins the flatc runtime version (static_assert).
UE_SCHEMA_DIR="$ROOT/integrations/unreal/OpenUSDConnect/Source/OpenUSDConnectPXR/Public/Schema"
flatc --cpp --gen-all --cpp-std c++17 \
    -I "$SCHEMA_DIR" \
    -o "$UE_SCHEMA_DIR" \
    "$SCHEMA_DIR/messages.fbs"

echo "Unreal C++ bindings generated: $UE_SCHEMA_DIR/messages_generated.h (flatc $(flatc --version | grep -o '[0-9.]*'))"
