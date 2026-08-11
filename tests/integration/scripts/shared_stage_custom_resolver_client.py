"""One process in the custom-resolver shared-stage integration test."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from pxr import Ar, Plug, Usd

from openusdconnect import ClientPhase, SharedStageClient


def _pump_until(client: SharedStageClient, predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client.update()
        if predicate():
            return
        time.sleep(0.01)
    client.update()
    if not predicate():
        raise TimeoutError("shared-stage custom-resolver client timed out")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("first", "second"), required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--plugin-resources", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    Plug.Registry().RegisterPlugins(str(args.plugin_resources))
    context = Ar.GetResolver().CreateContextFromString("asset", str(args.mapping))
    if context.IsEmpty():
        raise RuntimeError("usdResolverExample did not create a resolver context")
    stage = Usd.Stage.Open(str(args.stage), context)
    if stage is None:
        raise RuntimeError(f"could not open {args.stage}")
    content = stage.GetLayerStack(includeSessionLayers=False)[1]
    authored_path = stage.GetRootLayer().subLayerPaths[0]
    resolved_path = str(content.resolvedPath)

    with SharedStageClient(
        stage,
        app_name=f"custom-resolver-{args.role}",
        port=args.port,
        persist_token=False,
        reconnect=False,
    ) as client:
        if not client.connect(timeout=5):
            raise ConnectionError("shared-stage server is unavailable")
        _pump_until(client, lambda: client.status.phase is ClientPhase.READY)
        stage.SetEditTarget(Usd.EditTarget(content))

        value = stage.GetAttributeAtPath("/World.value")
        if args.role == "first":
            value.Set(7)
            update = client.update()
            if update.submitted_events != 1 or not client.flush(timeout=5):
                content_spec = content.GetAttributeAtPath("/World.value")
                raise RuntimeError(
                    "first custom-resolver edit was not committed: "
                    f"update={update!r}, mapped={client.is_layer_mapped(content)}, "
                    f"prepared={client.prepared_event_count}, value={value.Get()!r}, "
                    f"content_default={content_spec.default if content_spec else None!r}, "
                    f"dirty={content.dirty}, editable={content.permissionToEdit}, "
                    f"tracker={type(client._tracker).__name__}, "
                    f"target={stage.GetEditTarget().GetLayer().identifier!r}"
                )
            _pump_until(client, lambda: value.Get() == 11)
        else:
            _pump_until(client, lambda: value.Get() == 7)
            value.Set(11)
            update = client.update()
            if update.submitted_events != 1 or not client.flush(timeout=5):
                raise RuntimeError(
                    "second custom-resolver edit was not committed: "
                    f"update={update!r}, mapped={client.is_layer_mapped(content)}, "
                    f"prepared={client.prepared_event_count}, value={value.Get()!r}"
                )

        print(
            "RESULT:"
            + json.dumps(
                {
                    "role": args.role,
                    "authored_path": authored_path,
                    "resolved_path": resolved_path,
                    "phase": client.status.phase.value,
                    "value": value.Get(),
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
