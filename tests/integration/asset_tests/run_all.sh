#!/bin/bash
# Run all asset integration tests sequentially.
# Requires: uv, Blender at .blender/blender-5.0.1-windows-x64/blender.exe
# Usage: bash tests/integration/asset_tests/run_all.sh

set -e
cd "$(dirname "$0")/../../.."

BLENDER=".blender/blender-5.0.1-windows-x64/blender.exe"
PORT=7202
DB="test_asset_suite.db"
RESULTS=""

for test in tests/integration/asset_tests/test_*.py; do
    name=$(basename "$test" .py)
    echo ""
    echo "====== Running: $name ======"

    # Clean and start server
    rm -f "$DB"*
    uv run python -m openusdconnect.server --host 127.0.0.1 --port $PORT --log "$DB" &
    SERVER_PID=$!
    sleep 3

    # Run test
    BLENDER_USER_RESOURCES=".blender/user_data" \
        "$BLENDER" --python "$test" 2>&1 | tee "/tmp/$name.log"

    # Extract result
    result=$(grep -E "SUCCESS|FAILED" "/tmp/$name.log" | tail -1)
    RESULTS="$RESULTS\n  $name: $result"

    # Kill server
    kill $SERVER_PID 2>/dev/null || true
    wait $SERVER_PID 2>/dev/null || true
    rm -f "$DB"*
done

echo ""
echo "====== SUMMARY ======"
echo -e "$RESULTS"
