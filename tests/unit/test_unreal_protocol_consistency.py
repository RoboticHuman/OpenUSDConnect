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


def test_native_unreal_reliability_architecture_stays_explicit():
    root = Path(__file__).resolve().parents[2]
    plugin = root / "integrations" / "unreal" / "OpenUSDConnect" / "Source"
    emitter = (plugin / "OpenUSDConnect" / "Private" / "EmitClient.h").read_text(encoding="utf-8")
    emitter_source = (plugin / "OpenUSDConnect" / "Private" / "EmitClient.cpp").read_text(
        encoding="utf-8"
    )
    framing = (plugin / "OpenUSDConnect" / "Private" / "USDWireFraming.h").read_text(
        encoding="utf-8"
    )
    transaction_builder = (plugin / "OpenUSDConnect" / "Private" / "TxnBuilder.cpp").read_text(
        encoding="utf-8"
    )
    core = (
        root
        / "native"
        / "client_core"
        / "include"
        / "openusdconnect"
        / "client"
        / "producer_session.h"
    )
    protocol = (
        root
        / "native"
        / "client_core"
        / "include"
        / "openusdconnect"
        / "client"
        / "protocol_codec.h"
    )
    protocol_source = protocol.read_text(encoding="utf-8")
    receiver = (plugin / "OpenUSDConnect" / "Private" / "SyncClient.h").read_text(encoding="utf-8")
    subsystem = (plugin / "OpenUSDConnect" / "Private" / "USDConnectSubsystem.cpp").read_text(
        encoding="utf-8"
    )
    subsystem_header = (plugin / "OpenUSDConnect" / "Public" / "USDConnectSubsystem.h").read_text(
        encoding="utf-8"
    )
    applier = (plugin / "OpenUSDConnectPXR" / "Public" / "USDEventApplier.h").read_text(
        encoding="utf-8"
    )

    assert "class FProducerEndpointState" in emitter
    assert "OrderedProducerSession<FProducerFrame>" in emitter
    assert "TSharedPtr<const OUC::FWireFrame, ESPMode::ThreadSafe>" in emitter
    assert "std::vector<uint8>" not in emitter
    assert "std::shared_ptr" not in emitter
    assert "PendingTxns" not in emitter
    assert "Session.AcknowledgeThrough" in emitter_source
    assert "Session.ClaimNextUnsent" in emitter_source
    assert "MakeShared<FWireFrame, ESPMode::ThreadSafe>(MoveTemp(Frame))" in emitter_source
    assert "FinishEnvelopeFrame(Builder, RootOffset)" in framing
    assert "Builder.Release()" in framing
    assert "FinishSizePrefixedEnvelopeBuffer(builder, envelope)" in protocol_source
    assert "WriteFrameHeader(payload_size, builder.GetBufferPointer()" in protocol_source
    assert "EncodeFrameInto" not in framing
    assert core.is_file()
    assert protocol.is_file()
    assert "BuildHelloFrame(Builder, Parameters)" in framing
    assert "FinishTransactionFrame(" in transaction_builder
    assert "BuildXformTrsEvent(" in transaction_builder
    assert "BuildVisibilityEvent(" in transaction_builder
    assert "BuildConnectableInputValue(" in transaction_builder
    assert "HandshakeResponseView" in emitter_source
    assert "ControlMessageView" in emitter_source
    assert "std::vector" not in protocol_source
    assert "TSharedPtr<FProducerEndpointState> ProducerState" in subsystem_header
    assert "NextProducerTxnId" not in subsystem_header
    assert "struct FValidatedReceiverFrame" in receiver
    assert "OrderedReceiverSession<FValidatedReceiverFrame>" in receiver
    assert "FReceiverSession ReceiverSession" in receiver
    assert "FQueuedReceiverFrame" not in subsystem_header
    assert "OnReceiverReplayGenerationChanged" in subsystem
    assert "RequestReceiverReplay(" in subsystem
    assert "SyncClient->TryPopFrame(Frame)" in subsystem
    assert "DrainFrames" not in subsystem
    assert "bOwnEcho" not in subsystem
    assert "FUSDEventApplier::ApplyValidatedFrame(Frame.Bytes" in subsystem
    assert "FUSDEventApplier::FrameUsesChangeBlock(Frame" not in subsystem
    assert "static bool ApplyFrame" in applier
    assert "static bool ApplyValidatedFrame" in applier
    assert "WorkEvent->Trigger()" in emitter_source
    assert "WorkEvent->Wait(WaitMilliseconds)" in emitter_source


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
