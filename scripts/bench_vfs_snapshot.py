r"""Benchmark VFS snapshot generation.

Measures cold and cached reads for the flattened fallback plus the lightweight
composition root and manifest. This is intended for production sizing on real
show scenes:

    python scripts/bench_vfs_snapshot.py --base D:\show\shot\scene.usda
    python scripts/bench_vfs_snapshot.py --synthetic-prims 10000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openusdconnect.cli_common import nonnegative_int
from openusdconnect.server import UsdSyncServer
from openusdconnect.server.vfs import VirtualStageFileSet


def _send_synthetic_prims(server: UsdSyncServer, count: int, batch_size: int = 500) -> None:
    for start in range(0, count, batch_size):
        events = [
            {
                "k": "ensure_prim",
                "prim": f"/World/P_{i:06d}",
                "typeName": "Xform",
            }
            for i in range(start, min(start + batch_size, count))
        ]
        server._commit_events(events, client_id="bench", origin="bench")


def _measure(label: str, func) -> dict:
    start = time.perf_counter()
    data = func()
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return {"label": label, "bytes": len(data), "elapsed_ms": round(elapsed_ms, 3)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=None, help="Optional base USD file")
    parser.add_argument(
        "--synthetic-prims",
        type=nonnegative_int,
        default=0,
        help="Create this many synthetic Xform prims before benchmarking",
    )
    args = parser.parse_args(argv)

    server = UsdSyncServer(base_usd_path=args.base, log_path=":memory:")
    try:
        if args.synthetic_prims:
            _send_synthetic_prims(server, args.synthetic_prims)

        vfs = VirtualStageFileSet(
            server,
            flat_name="scene.usd",
            advertise_host="127.0.0.1",
            sync_port=7200,
            share="usd",
            vfs_base_url="http://127.0.0.1:7280/usd",
        )
        results = [
            _measure("composition_root_cold", lambda: vfs.get_file("scene.live.usda").read()),
            _measure("manifest_cold", lambda: vfs.get_file("openusdconnect.json").read()),
            _measure("flattened_cold", lambda: vfs.get_file("scene.usd").read()),
            _measure("flattened_cached", lambda: vfs.get_file("scene.usd").read()),
        ]
        print(json.dumps({"results": results}, indent=2))
    finally:
        server.shutdown()
        server.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
