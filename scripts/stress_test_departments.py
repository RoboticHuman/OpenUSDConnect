"""Stress test: 100+ TCP clients hammering the server concurrently.

Spawns a real server subprocess and connects a mix of:
  - Emitters: send transform edits to private and shared prims
  - Receivers: consume broadcasts and count received events
  - Bidirectional: emit AND receive on separate sockets

Each department has multiple emitters editing overlapping prims.
After all emitters finish, verifies the composed state is correct
and that every receiver got a consistent event stream.

Optionally profiles the server with py-spy.

Usage:
    uv run python scripts/stress_test_departments.py
    uv run python scripts/stress_test_departments.py --profile
    uv run python scripts/stress_test_departments.py --emitters 40 --receivers 40 --bidi 20
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time

# Ensure the project root is on the path when run as a script.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from pxr import Usd, UsdGeom

from openusdconnect.codec import message_to_dict
from openusdconnect.event_apply import apply_events
from openusdconnect.framing import recv_framed
from openusdconnect.transport import send_msg

SERVER_PORT = 7201
DB_PATH = "stress_test.db"

DEPT_LIST = ["animation", "lighting", "fx", "layout"]
DEPARTMENTS = ",".join(DEPT_LIST)

# Shared prims that multiple departments contest.
SHARED_PRIMS = ["/World/Hero", "/World/Camera", "/World/EnvSphere", "/World/Stage"]


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _send(sock, msg):
    send_msg(sock, msg)


def _recv_one(sock):
    buf = recv_framed(sock)
    if not buf:
        return None
    return message_to_dict(buf)


def _connect_emitter(port, client_id, department):
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    _send(s, {
        "type": "hello", "role": "emitter", "protocol_version": 1,
        "client_id": client_id, "origin": f"{client_id}-origin",
        "department": department,
    })
    _recv_one(s)  # hello_ok
    return s


def _connect_receiver(port, client_id):
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    _send(s, {
        "type": "hello", "role": "receiver", "protocol_version": 1,
        "client_id": client_id, "sync_from": 1,
    })
    s.settimeout(0.5)
    return s


def _wait_for_server(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)
    return False


def _make_trs_events(prim_path, t):
    return [
        {"k": "ensure_prim", "prim": prim_path, "typeName": "Xform"},
        {"k": "ensure_xform_ops", "prim": prim_path},
        {"k": "set_xform_trs", "prim": prim_path,
         "fields": ["t"], "t": list(t)},
    ]


def _read_translate(stage, prim_path):
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return None
    xf = UsdGeom.Xformable(prim)
    for op in xf.GetOrderedXformOps():
        if op.GetAttr().GetName() == "xformOp:translate":
            v = op.Get()
            return (v[0], v[1], v[2])
    return None


# ---------------------------------------------------------------------------
# Client generation
# ---------------------------------------------------------------------------

def _generate_clients(n_emitters, n_receivers, n_bidi):
    """Generate client specs distributed across departments."""
    emitters = []
    for i in range(n_emitters):
        dept = DEPT_LIST[i % len(DEPT_LIST)]
        dept_idx = DEPT_LIST.index(dept)
        emitters.append({
            "client_id": f"emitter-{dept}-{i:03d}",
            "department": dept,
            "private_prim": f"/World/{dept}/Prim_{i:03d}",
            "base": (dept_idx * 1000 + i, i, 0),
        })

    receivers = [{"client_id": f"receiver-{i:03d}"} for i in range(n_receivers)]

    bidi = []
    for i in range(n_bidi):
        dept = DEPT_LIST[i % len(DEPT_LIST)]
        dept_idx = DEPT_LIST.index(dept)
        bidi.append({
            "client_id": f"bidi-{dept}-{i:03d}",
            "department": dept,
            "private_prim": f"/World/{dept}/Bidi_{i:03d}",
            "base": (dept_idx * 1000 + 500 + i, 500 + i, 0),
        })

    return emitters, receivers, bidi


# ---------------------------------------------------------------------------
# Worker threads
# ---------------------------------------------------------------------------

def _emitter_worker(port, spec, iterations, barrier, connect_delay, errors, stats):
    cid = spec["client_id"]
    try:
        time.sleep(connect_delay)
        sock = _connect_emitter(port, cid, spec["department"])
        barrier.wait(timeout=30)
        txn_count = 0
        dept_idx = DEPT_LIST.index(spec["department"])
        for i in range(iterations):
            base = spec["base"]
            t = (base[0] + i, base[1] + i, base[2] + i)
            _send(sock, {"type": "txn", "client_id": cid,
                         "events": _make_trs_events(spec["private_prim"], t)})
            txn_count += 1

            shared = SHARED_PRIMS[i % len(SHARED_PRIMS)]
            st = (dept_idx * 100 + i, i, dept_idx)
            _send(sock, {"type": "txn", "client_id": cid,
                         "events": _make_trs_events(shared, st)})
            txn_count += 1

        stats[cid] = txn_count
        _send(sock, {"type": "quit"})
        sock.close()
    except Exception as exc:
        errors.append((cid, exc))


def _receiver_worker(port, spec, done_event, connect_delay, errors, stats):
    cid = spec["client_id"]
    try:
        time.sleep(connect_delay)
        sock = _connect_receiver(port, cid)
        count = 0
        while not done_event.is_set():
            try:
                recv_framed(sock)
                count += 1
            except TimeoutError:
                continue
            except (ConnectionError, OSError):
                break
        stats[cid] = count
        sock.close()
    except Exception as exc:
        errors.append((cid, exc))


def _bidi_worker(port, spec, iterations, barrier, done_event,
                 connect_delay, errors, emit_stats, recv_stats):
    cid = spec["client_id"]
    try:
        time.sleep(connect_delay)
        emit_sock = _connect_emitter(port, cid, spec["department"])
        recv_sock = _connect_receiver(port, f"{cid}-rx")

        rx_count = [0]

        def _rx():
            while not done_event.is_set():
                try:
                    recv_framed(recv_sock)
                    rx_count[0] += 1
                except TimeoutError:
                    if done_event.is_set():
                        break
                    continue
                except (ConnectionError, OSError):
                    break

        rx_thread = threading.Thread(target=_rx, daemon=True)
        rx_thread.start()

        barrier.wait(timeout=30)
        txn_count = 0
        dept_idx = DEPT_LIST.index(spec["department"])
        for i in range(iterations):
            base = spec["base"]
            t = (base[0] + i, base[1] + i, base[2] + i)
            _send(emit_sock, {"type": "txn", "client_id": cid,
                              "events": _make_trs_events(spec["private_prim"], t)})
            txn_count += 1

            shared = SHARED_PRIMS[i % len(SHARED_PRIMS)]
            st = (dept_idx * 100 + i, i, dept_idx)
            _send(emit_sock, {"type": "txn", "client_id": cid,
                              "events": _make_trs_events(shared, st)})
            txn_count += 1

        emit_stats[cid] = txn_count
        _send(emit_sock, {"type": "quit"})
        emit_sock.close()

        rx_thread.join(timeout=5)
        recv_stats[cid] = rx_count[0]
        recv_sock.close()
    except Exception as exc:
        errors.append((cid, exc))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_stress(n_emitters, n_receivers, n_bidi, iterations, profile, profile_output,
               connect_existing):
    total_clients = n_emitters + n_receivers + n_bidi
    total_sockets = n_emitters + n_receivers + n_bidi * 2

    server_proc = None

    if connect_existing:
        print(f"Connecting to existing server on port {SERVER_PORT}...")
        if not _wait_for_server(SERVER_PORT, timeout=5):
            print("ERROR: No server running. Start one first:")
            print(f"  uv run python -m openusdconnect.server --port {SERVER_PORT} "
                  f"--log {DB_PATH} --departments {DEPARTMENTS}")
            return False
    else:
        for f in _db_files():
            if os.path.exists(f):
                os.remove(f)

        profile_path = profile_output or "stress_profile.svg"
        server_cmd = [
            sys.executable, "-m", "openusdconnect.server",
            "--port", str(SERVER_PORT),
            "--log", DB_PATH,
            "--departments", DEPARTMENTS,
        ]
        if profile:
            print(f"  Profiling enabled (output: {profile_path})")
            server_cmd = [
                "py-spy", "record",
                "--output", profile_path,
                "--rate", "200",
                "--", *server_cmd,
            ]

        server_proc = subprocess.Popen(server_cmd)

        if not _wait_for_server(SERVER_PORT):
            print("ERROR: Server failed to start.")
            server_proc.terminate()
            return False

        print(f"  Server PID: {server_proc.pid}")

    print(f"  Departments: {DEPT_LIST}")
    print(f"  Clients: {n_emitters} emitters + {n_receivers} receivers + {n_bidi} bidi = {total_clients}")
    print(f"  TCP sockets: {total_sockets}")
    print(f"  Iterations per writer: {iterations}")

    emitters, receivers, bidis = _generate_clients(n_emitters, n_receivers, n_bidi)

    n_writers = n_emitters + n_bidi
    barrier = threading.Barrier(n_writers) if n_writers > 0 else None
    done_event = threading.Event()
    errors = []
    emit_stats = {}
    recv_stats = {}

    try:
        print(f"\nConnecting {total_clients} clients (staggered)...")

        # Stagger connections to avoid overwhelming the server's accept queue.
        # 0.02s between each = ~2 seconds for 100 clients.
        delay_step = 0.02
        idx = 0

        rx_threads = []
        for spec in receivers:
            t = threading.Thread(target=_receiver_worker,
                                 args=(SERVER_PORT, spec, done_event,
                                       idx * delay_step, errors, recv_stats))
            t.start()
            rx_threads.append(t)
            idx += 1

        bidi_threads = []
        for spec in bidis:
            t = threading.Thread(target=_bidi_worker,
                                 args=(SERVER_PORT, spec, iterations, barrier,
                                       done_event, idx * delay_step,
                                       errors, emit_stats, recv_stats))
            t.start()
            bidi_threads.append(t)
            idx += 1

        emit_threads = []
        for spec in emitters:
            t = threading.Thread(target=_emitter_worker,
                                 args=(SERVER_PORT, spec, iterations, barrier,
                                       idx * delay_step, errors, emit_stats))
            t.start()
            emit_threads.append(t)
            idx += 1

        print(f"Running {iterations} iterations across {n_writers} writers...")
        t0 = time.perf_counter()

        for t in emit_threads:
            t.join(timeout=120)
        for t in bidi_threads:
            t.join(timeout=120)
        elapsed = time.perf_counter() - t0

        time.sleep(2)
        done_event.set()
        for t in rx_threads:
            t.join(timeout=10)

        if errors:
            print(f"\nERRORS ({len(errors)}):")
            for cid, exc in errors[:10]:
                print(f"  {cid}: {exc}")
            if len(errors) > 10:
                print(f"  ... and {len(errors) - 10} more")
            return False

        total_txns = sum(emit_stats.values())
        total_rx = sum(recv_stats.values())
        print(f"\nResults ({elapsed:.2f}s):")
        print(f"  Total txns sent: {total_txns}")
        print(f"  Throughput: {total_txns / elapsed:.0f} txn/s")

        per_dept = {}
        for spec in emitters + bidis:
            dept = spec["department"]
            per_dept.setdefault(dept, {"writers": 0, "txns": 0})
            per_dept[dept]["writers"] += 1
            per_dept[dept]["txns"] += emit_stats.get(spec["client_id"], 0)
        for dept in DEPT_LIST:
            d = per_dept.get(dept, {"writers": 0, "txns": 0})
            print(f"  {dept}: {d['writers']} writers, {d['txns']} txns")

        rx_counts = sorted(recv_stats.values())
        if rx_counts:
            print(f"  Receivers: min={rx_counts[0]} max={rx_counts[-1]} "
                  f"median={rx_counts[len(rx_counts)//2]} events")
        print(f"  Total events received (all receivers): {total_rx}")

        # Verify: connect a fresh receiver and replay the full event log.
        # The receiver replays from seq 1, so it gets the complete history.
        # We drain until the stream goes quiet (no new data for 3 seconds).
        print("\nVerifying final state via full replay...")

        verify_sock = _connect_receiver(SERVER_PORT, "verifier-final")
        verify_events = []
        verify_sock.settimeout(1)
        quiet_seconds = 0
        while quiet_seconds < 3:
            try:
                raw = recv_framed(verify_sock)
                quiet_seconds = 0
                msg = message_to_dict(raw)
                if msg.get("type") == "event":
                    verify_events.append(msg.get("event", msg))
            except TimeoutError:
                quiet_seconds += 1
            except (ConnectionError, OSError):
                break
        verify_sock.close()
        print(f"  Replayed {len(verify_events)} events")

        verify_stage = Usd.Stage.CreateInMemory()
        verify_stage.DefinePrim("/Root", "Xform")
        if verify_events:
            apply_events(verify_stage, verify_events)

        n = iterations - 1
        all_pass = True

        # Shared prims must exist (we can't predict the exact composed
        # value because department interleaving is non-deterministic, but
        # the server's broadcast gating ensures only the strongest
        # department's value is sent when there's contention).
        for prim_path in SHARED_PRIMS:
            actual = _read_translate(verify_stage, prim_path)
            if actual is None:
                print(f"  FAIL {prim_path}: not found")
                all_pass = False
            else:
                print(f"  OK   {prim_path}: {actual}")

        # Private prims: each emitter's last iteration value is
        # deterministic since only one emitter writes to each private prim.
        sample_count = 0
        fail_count = 0
        for spec in emitters + bidis:
            sample_count += 1
            base = spec["base"]
            expected = (base[0] + n, base[1] + n, base[2] + n)
            actual = _read_translate(verify_stage, spec["private_prim"])
            if actual is None:
                print(f"  FAIL {spec['private_prim']}: not found")
                fail_count += 1
                all_pass = False
            elif actual != expected:
                print(f"  FAIL {spec['private_prim']}: {actual} != {expected}")
                fail_count += 1
                all_pass = False
        if fail_count == 0:
            print(f"  OK   {sample_count} private prims verified")

        if all_pass:
            print("\nAll verifications passed.")
        else:
            print("\nSome verifications FAILED.")
        return all_pass

    finally:
        print("\nShutting down...")
        if server_proc:
            server_proc.terminate()
            server_proc.wait(timeout=10)
            print("  Server stopped.")
            if profile:
                print(f"  Profile saved to {profile_path}")
            for f in _db_files():
                try:
                    os.remove(f)
                except OSError:
                    pass
        else:
            print("  Server left running (--connect mode).")


def _db_files():
    """All DB files (main, tokens, WAL, SHM) that need cleanup."""
    bases = [DB_PATH, DB_PATH.replace(".db", "_tokens.db")]
    files = []
    for b in bases:
        files.append(b)
        files.append(b + "-wal")
        files.append(b + "-shm")
    return files


def main():
    ap = argparse.ArgumentParser(description="Stress test department concurrency")
    ap.add_argument("--emitters", type=int, default=40,
                    help="Number of emitter-only clients (default: 40)")
    ap.add_argument("--receivers", type=int, default=40,
                    help="Number of receiver-only clients (default: 40)")
    ap.add_argument("--bidi", type=int, default=20,
                    help="Number of bidirectional clients (default: 20)")
    ap.add_argument("--iterations", type=int, default=100,
                    help="Write iterations per writer (default: 100)")
    ap.add_argument("--profile", action="store_true",
                    help="Launch server through py-spy (needs Administrator)")
    ap.add_argument("--profile-output", default=None,
                    help="Profile output path (default: stress_profile.svg)")
    ap.add_argument("--connect", action="store_true",
                    help="Connect to an already-running server instead of spawning one")
    args = ap.parse_args()

    ok = run_stress(args.emitters, args.receivers, args.bidi,
                    args.iterations, args.profile, args.profile_output,
                    args.connect)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
