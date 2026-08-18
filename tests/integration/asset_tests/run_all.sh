#!/usr/bin/env bash
# Run the maintained asset integration suite. Extra arguments pass to pytest.

set -euo pipefail
cd "$(dirname "$0")/../../.."

uv run pytest tests/integration/asset_tests/ --asset-tests -v "$@"
