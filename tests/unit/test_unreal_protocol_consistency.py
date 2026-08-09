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


def test_native_unreal_emitter_keeps_outbox_across_reconnect_until_ack():
    root = Path(__file__).resolve().parents[2]
    source = (
        root
        / "integrations"
        / "unreal"
        / "OpenUSDConnect"
        / "Source"
        / "OpenUSDConnect"
        / "Private"
        / "EmitClient.cpp"
    ).read_text(encoding="utf-8")

    outbox = source.index("TArray<FQueuedProducerTxn> PendingTxns;")
    retire = source.index("auto RetireThrough", outbox)
    connect_loop = source.index("while (!bShouldStop.load", outbox)
    acknowledge = source.index("payload_as_TransactionResult", connect_loop)
    cumulative_remove = source.index("RetireThrough(AckId)", acknowledge)
    reconnect_reset = source.index("bSent = false", cumulative_remove)

    assert outbox < retire < connect_loop < acknowledge < cumulative_remove < reconnect_reset
    assert "PendingTxns.Add(MoveTemp(Queued))" in source[connect_loop:acknowledge]
    assert "PendingTxns[PendingHead].TxnId <= AckId" in source[retire:connect_loop]
    assert "HelloOk->committed_through()" in source[connect_loop:acknowledge]


def test_native_unreal_emitter_drains_results_and_exposes_recovery_status():
    root = Path(__file__).resolve().parents[2]
    plugin = root / "integrations" / "unreal" / "OpenUSDConnect" / "Source" / "OpenUSDConnect"
    source = (plugin / "Private" / "EmitClient.cpp").read_text(encoding="utf-8")
    header = (plugin / "Private" / "EmitClient.h").read_text(encoding="utf-8")
    subsystem = (plugin / "Public" / "USDConnectSubsystem.h").read_text(encoding="utf-8")

    assert "MaxResultsPerIteration = 256" in source
    assert "ResultIndex < MaxResultsPerIteration" in source
    assert "PendingTransactionCount.fetch_sub" in source
    assert "bRecoveryRequired.store(true" in source
    assert "GetPendingTransactionCount" in header
    assert "PendingTransactions" in subsystem
    assert "bRecoveryRequired" in subsystem


def test_native_unreal_emitter_exposes_bounded_durability_flush():
    root = Path(__file__).resolve().parents[2]
    plugin = root / "integrations" / "unreal" / "OpenUSDConnect" / "Source" / "OpenUSDConnect"
    source = (plugin / "Private" / "EmitClient.cpp").read_text(encoding="utf-8")
    header = (plugin / "Private" / "EmitClient.h").read_text(encoding="utf-8")
    subsystem_header = (plugin / "Public" / "USDConnectSubsystem.h").read_text(
        encoding="utf-8"
    )
    subsystem_source = (plugin / "Private" / "USDConnectSubsystem.cpp").read_text(
        encoding="utf-8"
    )

    assert "bool FEmitClient::FlushPending(double TimeoutSeconds) const" in source
    assert "PendingTransactionCount.load(std::memory_order_acquire)" in source
    assert "bool FlushPending(double TimeoutSeconds) const" in header
    assert "bool Flush(float TimeoutSeconds = 5.0f) const" in subsystem_header
    assert "EmitClient->FlushPending(2.0)" in subsystem_source


def test_native_unreal_department_receiver_fails_closed():
    root = Path(__file__).resolve().parents[2]
    source = (
        root
        / "integrations"
        / "unreal"
        / "OpenUSDConnect"
        / "Source"
        / "OpenUSDConnect"
        / "Private"
        / "USDConnectSubsystem.cpp"
    ).read_text(encoding="utf-8")

    guard = source.index("bStartReceiver && !Settings->Department.IsEmpty()")
    reject = source.index('TEXT("unsupported_configuration")', guard)
    start_receiver = source.index("if (bStartReceiver)", reject)
    assert guard < reject < start_receiver
