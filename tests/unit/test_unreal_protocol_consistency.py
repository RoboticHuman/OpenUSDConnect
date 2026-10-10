"""Keep Unreal's compile-time wire versions aligned with the Python core."""

from __future__ import annotations

import re
from pathlib import Path

from integrations.unreal.test_scenario import _preview_surface_events
from openusdconnect.codec import SCHEMA_VERSION
from openusdconnect.protocol_constants import PROTOCOL_VERSION


def test_native_wire_versions_match_python_core():
    root = Path(__file__).resolve().parents[2]
    header = (
        root
        / "native"
        / "client_core"
        / "include"
        / "openusdconnect"
        / "client"
        / "protocol_codec.h"
    ).read_text(encoding="utf-8")

    schema = re.search(r"kSchemaVersion\s*=\s*(\d+)", header)
    protocol = re.search(r"kProtocolVersion\s*=\s*(\d+)", header)
    assert schema and int(schema.group(1)) == SCHEMA_VERSION
    assert protocol and int(protocol.group(1)) == PROTOCOL_VERSION


def test_native_unreal_plugin_runs_on_the_client_engine():
    root = Path(__file__).resolve().parents[2]
    plugin = root / "integrations" / "unreal" / "OpenUSDConnect" / "Source"
    private = plugin / "OpenUSDConnect" / "Private"
    client = root / "native" / "client_core" / "include" / "openusdconnect" / "client"
    runner = (private / "EndpointRunner.h").read_text(encoding="utf-8")
    subsystem = (private / "USDConnectSubsystem.cpp").read_text(encoding="utf-8")
    subsystem_header = (plugin / "OpenUSDConnect" / "Public" / "USDConnectSubsystem.h").read_text(
        encoding="utf-8"
    )
    transaction_builder = (private / "TxnBuilder.cpp").read_text(encoding="utf-8")
    protocol_source = (client / "protocol_codec.h").read_text(encoding="utf-8")
    applier = (plugin / "OpenUSDConnectPXR" / "Public" / "USDEventApplier.h").read_text(
        encoding="utf-8"
    )
    plugin_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for module in ("OpenUSDConnect", "OpenUSDConnectPXR")
        for path in (plugin / module).rglob("*")
        if path.suffix in {".h", ".cpp"}
    )

    # The connection protocol lives in the client core's endpoints; the plugin
    # only moves bytes, keeps no replay or outbox state, and builds no handshake.
    assert "template <typename Endpoint>\nclass FEndpointRunner final : public FRunnable" in runner
    assert "Target.TakeActions()" in runner
    assert "Target.OnReadTimeout()" in runner
    for retired in (
        "ReceiverReplayIdentity",
        "OrderedReceiverSession",
        "OrderedProducerSession",
        "BuildHelloFrame",
        "HandshakeResponseView",
        "AsyncTask",
    ):
        assert retired not in plugin_sources, retired
    for retired in ("EmitClient.h", "SyncClient.h", "USDWireFraming.h"):
        assert not (private / retired).exists()

    assert "TSharedPtr<openusdconnect::client::ReceiverEndpoint> Receiver" in subsystem_header
    assert "TSharedPtr<openusdconnect::client::ProducerEndpoint> Producer" in subsystem_header
    for queue in ("ReceiverNotifications", "ProducerNotifications"):
        assert f"TSharedPtr<openusdconnect::client::NotificationQueue> {queue}" in subsystem_header
    # The receive path follows the engine's drain contract.
    for call in (
        "Receiver->Generation()",
        "Receiver->DrainFrames(1)",
        "Receiver->MarkAppliedThrough(Generation, Seq)",
        "Receiver->ResetAppliedProgress()",
        "Receiver->MarkReplayApplied()",
        "Receiver->RequestReplayFrom(",
        "FUSDEventApplier::ApplyValidatedFrame(",
        "Queue.Drain()",
    ):
        assert call in subsystem, call
    # A transaction ID is paired with the frame that encodes it under one lock.
    submit = subsystem[subsystem.index("bool UUSDConnectSubsystem::SubmitTransaction") :]
    lock = submit.index("FScopeLock Lock(&SubmitCS)")
    assert lock < submit.index("Producer->NextTransactionId()") < submit.index("Producer->Append(")
    assert "bOwnEcho" not in subsystem

    assert "FinishTransactionFrame(" in transaction_builder
    assert "BuildXformTrsEvent(" in transaction_builder
    assert "BuildVisibilityEvent(" in transaction_builder
    assert "BuildConnectableInputValue(" in transaction_builder
    assert "FinishEnvelopeBuffer(builder, envelope)" in protocol_source
    assert "FinishSizePrefixedEnvelopeBuffer(builder, envelope)" not in protocol_source
    assert "builder.PushBytes(header, kFrameHeaderSize)" in protocol_source
    assert "WriteFrameHeader(payload_size, header, max_frame_size)" in protocol_source
    assert "std::vector" not in protocol_source
    assert "static bool ApplyFrame" in applier
    assert "static bool ApplyValidatedFrame" in applier


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


def test_real_editor_scenario_exercises_compiled_instancing_appliers():
    _baseline, initial, updates = _preview_surface_events("color.jpg", "roughness.jpg")

    instanceable = [event for event in initial if event["k"] == "set_instanceable"]
    point_instancers = [
        event for event in (*initial, *updates) if event["k"] == "set_point_instancer"
    ]

    assert instanceable == [
        {"k": "set_instanceable", "prim": "/World/InstanceBall", "instanceable": True}
    ]
    assert len(point_instancers) == 2
    assert point_instancers[0]["fields"] == ["prototypes", "proto_indices", "positions"]
    assert point_instancers[1]["fields"] == ["positions"]
