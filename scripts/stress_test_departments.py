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
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# Ensure the project root is on the path when run as a script.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from pxr import Usd, UsdGeom

from integrations.server_process import start as start_server_process
from integrations.server_process import stop as stop_process
from integrations.server_process import wait_until_listening
from openusdconnect.cli_common import nonnegative_int, port_number, positive_int
from openusdconnect.codec import message_to_dict
from openusdconnect.event_apply import apply_events
from openusdconnect.framing import recv_framed
from openusdconnect.protocol import make_hello
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


def _wait_for_transaction_results(
    sock,
    expected,
    timeout=60,
    submitted_at=None,
    latency_sink=None,
):
    """Drain producer results until every submitted transaction is terminal."""
    sock.settimeout(timeout)
    acknowledged_through = 0
    while acknowledged_through < expected:
        message = _recv_one(sock)
        if message and message.get("type") == "transaction_result":
            if message.get("status") == 1:
                raise RuntimeError(f"transaction rejected: {message}")
            previous = acknowledged_through
            acknowledged_through = max(acknowledged_through, int(message.get("txn_id", 0)))
            if submitted_at is not None and latency_sink is not None:
                now = time.perf_counter()
                for txn_id in range(previous + 1, acknowledged_through + 1):
                    started = submitted_at.pop(txn_id, None)
                    if started is not None:
                        latency_sink.append(now - started)
    return acknowledged_through


def _start_result_reader(sock, expected, submitted_at=None, latency_sink=None):
    result = {}

    def _read():
        try:
            result["count"] = _wait_for_transaction_results(
                sock,
                expected,
                submitted_at=submitted_at,
                latency_sink=latency_sink,
            )
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    return thread, result


def _join_result_reader(thread, result, timeout=60):
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError("timed out waiting for durable transaction results")
    if "error" in result:
        raise result["error"]


def _connect_emitter(port, client_id, department, session_id):
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    _send(
        s,
        make_hello(
            "emitter",
            client_id=client_id,
            origin=f"{client_id}-origin",
            department=department,
            producer_session_id=session_id,
        ),
    )
    _recv_one(s)  # hello_ok
    return s


def _connect_receiver(port, client_id):
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    _send(
        s,
        make_hello(
            "receiver",
            sync_from=1,
            client_id=client_id,
            layered_replay=True,
        ),
    )
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


def _stop_py_spy_gracefully(proc, timeout=15):
    """Send Ctrl+Break / SIGINT so py-spy flushes its output before exiting.

    py-spy needs a controlled shutdown signal terminate() / TerminateProcess
    on Windows kills it without giving it a chance to write the recorded
    sample data to disk.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
    except OSError:
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def _make_trs_events(prim_path, t, first_encounter=False):
    """Build the events a real emitter would produce for one TRS update.

    Real emitters (``NoticeEmitter._build_dirty_prim_events``) only send
    ``ensure_prim`` + ``ensure_xform_ops`` on first encounter per session
    (gated by ``self._known_prims``); subsequent dirty events for the
    same prim are bare ``set_xform_trs``.  Defaulting *first_encounter*
    to ``False`` here lets writers send the structural prelude exactly
    once per writer-prim, which matches production load shape.
    """
    events = []
    if first_encounter:
        events.append({"k": "ensure_prim", "prim": prim_path, "typeName": "Xform"})
        events.append({"k": "ensure_xform_ops", "prim": prim_path})
    events.append({
        "k": "set_xform_trs", "prim": prim_path,
        "fields": ["t"], "t": list(t),
    })
    return events


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


def _shared_prims_to_verify(iterations, n_writers):
    if n_writers == 0:
        return []
    return SHARED_PRIMS[:min(iterations, len(SHARED_PRIMS))]


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

def _emitter_worker(
    port,
    spec,
    iterations,
    barrier,
    connect_delay,
    errors,
    stats,
    submission_finished_at,
    ack_latencies,
):
    cid = spec["client_id"]
    session_id = f"stress-{cid}"
    try:
        time.sleep(connect_delay)
        sock = _connect_emitter(port, cid, spec["department"], session_id)
        submitted_at = {}
        result_thread, result = _start_result_reader(
            sock,
            iterations * 2,
            submitted_at,
            ack_latencies,
        )
        barrier.wait(timeout=30)
        txn_count = 0
        dept_idx = DEPT_LIST.index(spec["department"])
        seen: set[str] = set()
        for i in range(iterations):
            base = spec["base"]
            t = (base[0] + i, base[1] + i, base[2] + i)
            private = spec["private_prim"]
            submitted_at[txn_count + 1] = time.perf_counter()
            _send(sock, {"type": "txn", "txn_id": txn_count + 1,
                         "events": _make_trs_events(
                             private, t, first_encounter=private not in seen)})
            seen.add(private)
            txn_count += 1

            shared = SHARED_PRIMS[i % len(SHARED_PRIMS)]
            st = (dept_idx * 100 + i, i, dept_idx)
            submitted_at[txn_count + 1] = time.perf_counter()
            _send(sock, {"type": "txn", "txn_id": txn_count + 1,
                         "events": _make_trs_events(
                             shared, st, first_encounter=shared not in seen)})
            seen.add(shared)
            txn_count += 1

        submission_finished_at[cid] = time.perf_counter()
        _join_result_reader(result_thread, result)
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_stress(n_emitters, n_receivers, n_bidi, iterations, profile, profile_output,
               connect_existing, text_profile=False, txn_batch_size=256,
               txn_batch_delay_ms=0.5, profile_rate=50,
               server_port=SERVER_PORT, db_path=DB_PATH, plugin_dll_dirs=()):
    db_path = Path(db_path)
    if not db_path.is_absolute():
        db_path = Path(_PROJECT_ROOT) / db_path
    total_clients = n_emitters + n_receivers + n_bidi
    total_sockets = n_emitters + n_receivers + n_bidi * 2

    server_proc = None
    py_spy_proc = None
    profile_path = None
    raw_profile_path = None
    text_profile_path = None

    if connect_existing:
        print(f"Connecting to existing server on port {server_port}...")
        if not _wait_for_server(server_port, timeout=5):
            print("ERROR: No server running. Start one first:")
            print(f"  uv run openusdconnect-server --port {server_port} "
                  f"--event-log {db_path} --departments {DEPARTMENTS}")
            return False
    else:
        for f in _db_files(db_path):
            if os.path.exists(f):
                os.remove(f)

        server_cmd = [
            "--port", str(server_port),
            "--event-log", str(db_path),
            "--departments", DEPARTMENTS,
            "--txn-batch-size", str(txn_batch_size),
            "--txn-batch-delay-ms", str(txn_batch_delay_ms),
        ]
        for directory in plugin_dll_dirs:
            server_cmd.extend(("--plugin-dll-dir", directory))
        server_proc = start_server_process(server_cmd, project_root=_PROJECT_ROOT)
        real_server_pid = server_proc.pid

        try:
            wait_until_listening(server_proc, "127.0.0.1", server_port, 15)
        except RuntimeError as error:
            print(f"ERROR: {error}")
            stop_process(server_proc)
            return False

        print(f"  Server PID: {real_server_pid}")

        # --text-profile records raw collapsed stacks and post-processes them
        # into a text hotspot report (LLM-friendly).  --profile records the
        # classic SVG flame graph (for human review).  They're mutually
        # exclusive text wins if both are set.
        # Spawn py-spy in a new process group on Windows so we can send
        # CTRL_BREAK_EVENT to it without also signaling ourselves; the same
        # flag is harmless on POSIX (we use signal.SIGINT there).
        py_spy_kwargs = {}
        if sys.platform == "win32":
            py_spy_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        if text_profile:
            text_profile_path = profile_output or "stress_profile.txt"
            raw_profile_path = str(Path(text_profile_path).with_suffix(".raw"))
            print(f"  Text profiling enabled at {profile_rate} Hz "
                  f"(output: {text_profile_path})")
            py_spy_proc = subprocess.Popen(
                [
                    "py-spy", "record",
                    "--format", "raw",
                    "--pid", str(real_server_pid),
                    "--output", raw_profile_path,
                    "--rate", str(profile_rate),
                ],
                **py_spy_kwargs,
            )
            time.sleep(0.5)
        elif profile:
            profile_path = profile_output or "stress_profile.svg"
            print(f"  SVG profiling enabled at {profile_rate} Hz "
                  f"(output: {profile_path})")
            py_spy_proc = subprocess.Popen(
                [
                    "py-spy", "record",
                    "--pid", str(real_server_pid),
                    "--output", profile_path,
                    "--rate", str(profile_rate),
                ],
                **py_spy_kwargs,
            )
            time.sleep(0.5)

    print(f"  Departments: {DEPT_LIST}")
    print(f"  Clients: {n_emitters} emitters + {n_receivers} receivers + {n_bidi} bidi = {total_clients}")
    print(f"  TCP sockets: {total_sockets}")
    print(f"  Iterations per writer: {iterations}")
    print(f"  Group commit: size={txn_batch_size}, delay={txn_batch_delay_ms:g} ms")

    emitters, receivers, bidis = _generate_clients(n_emitters, n_receivers, n_bidi)

    n_writers = n_emitters + n_bidi
    barrier = threading.Barrier(n_writers) if n_writers > 0 else None
    done_event = threading.Event()
    errors = []
    emit_stats = {}
    recv_stats = {}
    submission_finished_at = {}
    ack_latencies = []

    try:
        print(f"\nConnecting {total_clients} clients (staggered)...")

        # Stagger connections to avoid overwhelming the server's accept queue.
        # 0.02s between sockets avoids overwhelming the accept queue.
        delay_step = 0.02
        idx = 0

        rx_threads = []
        for spec in receivers:
            t = threading.Thread(target=_receiver_worker,
                                 args=(server_port, spec, done_event,
                                       idx * delay_step, errors, recv_stats))
            t.start()
            rx_threads.append(t)
            idx += 1

        bidi_threads = []
        for spec in bidis:
            # A product bidirectional client is two independent protocol
            # connections. Keep those lifecycles independent here too so the
            # durability timer never waits for receiver shutdown.
            receiver_spec = {"client_id": f"{spec['client_id']}-rx"}
            receiver_thread = threading.Thread(
                target=_receiver_worker,
                args=(server_port, receiver_spec, done_event,
                      idx * delay_step, errors, recv_stats),
            )
            receiver_thread.start()
            rx_threads.append(receiver_thread)
            idx += 1

            emitter_thread = threading.Thread(
                target=_emitter_worker,
                args=(server_port, spec, iterations, barrier,
                      idx * delay_step, errors, emit_stats,
                      submission_finished_at, ack_latencies),
            )
            emitter_thread.start()
            bidi_threads.append(emitter_thread)
            idx += 1

        emit_threads = []
        for spec in emitters:
            t = threading.Thread(target=_emitter_worker,
                                 args=(server_port, spec, iterations, barrier,
                                       idx * delay_step, errors, emit_stats,
                                       submission_finished_at,
                                       ack_latencies))
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

        # Stop py-spy now the actual workload is done, and the
        # verification block below is heavy log-replay work that we don't
        # want polluting the profile.  Graceful Ctrl+Break/SIGINT lets
        # py-spy flush its samples; null out the handle so the finally
        # cleanup doesn't double-stop it.
        if py_spy_proc is not None:
            _stop_py_spy_gracefully(py_spy_proc)
            py_spy_proc = None
            print("  Profiling stopped (workload complete; "
                  "verification follows but is not profiled).")

        if errors:
            print(f"\nERRORS ({len(errors)}):")
            for cid, exc in errors[:10]:
                print(f"  {cid}: {exc}")
            if len(errors) > 10:
                print(f"  ... and {len(errors) - 10} more")
            return False

        total_txns = sum(emit_stats.values())
        total_rx = sum(recv_stats.values())
        submitted_elapsed = (
            max(submission_finished_at.values()) - t0
            if submission_finished_at
            else elapsed
        )
        print(f"\nResults ({elapsed:.2f}s):")
        print(f"  Total txns sent: {total_txns}")
        print(f"  Submission throughput: {total_txns / submitted_elapsed:.0f} txn/s")
        print(f"  Durable throughput: {total_txns / elapsed:.0f} txn/s")
        if ack_latencies:
            ordered_latencies = sorted(ack_latencies)
            median_latency = ordered_latencies[len(ordered_latencies) // 2]
            p95_latency = ordered_latencies[int((len(ordered_latencies) - 1) * 0.95)]
            print(
                "  Ack latency: "
                f"median={median_latency * 1000:.1f} ms "
                f"p95={p95_latency * 1000:.1f} ms "
                f"max={ordered_latencies[-1] * 1000:.1f} ms"
            )

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

        verify_sock = _connect_receiver(server_port, "verifier-final")
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

        # Shared prims touched by this workload must exist. This lightweight
        # verifier applies the authored stream to one flat stage, so it does
        # not assert the department stack's final composed value.
        for prim_path in _shared_prims_to_verify(iterations, n_writers):
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
            # Safety net: under normal flow py-spy is stopped earlier
            # (after the workload, before verification).  This branch
            # catches the case where the workload raised before the
            # early stop ran.  Gracefully signal so py-spy still flushes
            # its samples.
            _stop_py_spy_gracefully(py_spy_proc)
            stop_process(server_proc, timeout=10)
            print("  Server stopped.")
            if profile and profile_path:
                print(f"  SVG profile saved to {profile_path}")
            if text_profile and raw_profile_path and text_profile_path:
                # py-spy has flushed the raw collapsed stacks; summarize them
                # into a text hotspot report for offline / agent analysis.
                summary_cmd = [
                    sys.executable,
                    os.path.join(_SCRIPT_DIR, "summarize_profile.py"),
                    raw_profile_path,
                    "--output", text_profile_path,
                    "--project", "openusdconnect",
                    "--label", (
                        f"stress: {n_emitters}E+{n_receivers}R+{n_bidi}B x "
                        f"{iterations} iter"
                    ),
                ]
                result = subprocess.run(summary_cmd, check=False)
                if result.returncode == 0:
                    print(f"  Text profile saved to {text_profile_path}")
                    print(f"  Raw stacks saved to {raw_profile_path}")
                else:
                    print("  WARN: failed to summarize raw profile")
            for f in _db_files(db_path):
                try:
                    os.remove(f)
                except OSError:
                    pass
        else:
            print("  Server left running (--connect mode).")


def _db_files(db_path=DB_PATH):
    """All DB files (main, tokens, WAL, SHM) that need cleanup."""
    db_path = str(db_path)
    base_path = Path(db_path)
    token_path = base_path.with_name(f"{base_path.stem}_tokens{base_path.suffix}")
    bases = [db_path, str(token_path)]
    files = []
    for b in bases:
        files.append(b)
        files.append(b + "-wal")
        files.append(b + "-shm")
    return files


def main():
    ap = argparse.ArgumentParser(description="Stress test department concurrency")
    ap.add_argument("--emitters", type=nonnegative_int, default=40,
                    help="Number of emitter-only clients (default: 40)")
    ap.add_argument("--receivers", type=nonnegative_int, default=40,
                    help="Number of receiver-only clients (default: 40)")
    ap.add_argument("--bidi", type=nonnegative_int, default=20,
                    help="Number of bidirectional clients (default: 20)")
    ap.add_argument("--iterations", type=positive_int, default=100,
                    help="Write iterations per writer (default: 100)")
    ap.add_argument("--txn-batch-size", type=positive_int, default=256,
                    help="Maximum transactions per durable commit (default: 256)")
    ap.add_argument("--txn-batch-delay-ms", type=float, default=0.5,
                    help="Maximum group collection delay in ms (default: 0.5)")
    ap.add_argument("--profile", action="store_true",
                    help="Launch server through py-spy producing an SVG flame "
                         "graph (needs Administrator on Windows)")
    ap.add_argument("--text-profile", action="store_true",
                    help="Launch server through py-spy and post-process into a "
                         "text hotspot report at stress_profile.txt "
                         "(LLM/agent-friendly; needs Administrator on Windows)")
    ap.add_argument("--profile-output", default=None,
                    help="Profile output path "
                         "(default: stress_profile.svg or stress_profile.txt)")
    ap.add_argument("--profile-rate", type=positive_int, default=50,
                    help="py-spy sampling rate in Hz (default: 50)")
    ap.add_argument("--port", type=port_number, default=SERVER_PORT,
                    help=f"Server port (default: {SERVER_PORT})")
    ap.add_argument("--event-log", type=Path, default=Path(DB_PATH),
                    help=f"Temporary stress event log (default: {DB_PATH})")
    ap.add_argument("--plugin-dll-dir", action="append", default=[], metavar="DIR",
                    help="Forward a USD plugin dependency directory; repeat as needed")
    ap.add_argument("--connect", action="store_true",
                    help="Connect to an already-running server instead of spawning one")
    args = ap.parse_args()

    if (args.profile or args.text_profile) and not args.connect and not shutil.which("py-spy"):
        ap.error("py-spy is unavailable; run through `uv run --group profile ...`")

    ok = run_stress(args.emitters, args.receivers, args.bidi,
                    args.iterations, args.profile, args.profile_output,
                    args.connect, text_profile=args.text_profile,
                    txn_batch_size=args.txn_batch_size,
                    txn_batch_delay_ms=args.txn_batch_delay_ms,
                    profile_rate=args.profile_rate,
                    server_port=args.port,
                    db_path=args.event_log,
                    plugin_dll_dirs=args.plugin_dll_dir)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
