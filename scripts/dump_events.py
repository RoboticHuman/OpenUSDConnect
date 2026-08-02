"""Dump the server event log from SQLite."""

import argparse
import os
import sqlite3
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from openusdconnect.codec import message_to_dict
from openusdconnect.defaults import DEFAULT_EVENT_LOG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_log", nargs="?", default=DEFAULT_EVENT_LOG)
    args = parser.parse_args(argv)

    with sqlite3.connect(args.event_log) as conn:
        rows = conn.execute("SELECT seq, event_bin FROM events ORDER BY seq").fetchall()
    print(f"Events: {len(rows)}")
    for seq, event_bin in rows:
        rec = message_to_dict(event_bin)
        ev = rec.get("event", rec)
        k = ev.get("k", "?")
        prim = ev.get("prim", "?")
        extras = {key: val for key, val in ev.items() if key not in ("k", "prim")}
        extra_str = f"  {extras}" if extras else ""
        print(f"  seq={seq}: {k} {prim}{extra_str}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
