"""Keep Unreal's compile-time wire versions aligned with the Python core."""

from __future__ import annotations

import re
from pathlib import Path

from openusdconnect.codec import SCHEMA_VERSION
from openusdconnect.protocol_constants import PROTOCOL_VERSION


def test_unreal_wire_versions_match_python_core():
    root = Path(__file__).resolve().parents[2]
    header = (
        root
        / "integrations"
        / "unreal"
        / "OpenUSDConnect"
        / "Source"
        / "OpenUSDConnectPXR"
        / "Public"
        / "USDConnectProtocol.h"
    ).read_text(encoding="utf-8")

    schema = re.search(r"kSchemaVersion\s*=\s*(\d+)", header)
    protocol = re.search(r"kProtocolVersion\s*=\s*(\d+)", header)
    assert schema and int(schema.group(1)) == SCHEMA_VERSION
    assert protocol and int(protocol.group(1)) == PROTOCOL_VERSION
