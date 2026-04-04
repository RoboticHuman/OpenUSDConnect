"""Dump the server event log from SQLite."""

import os
import sqlite3
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from openusdconnect.codec import message_to_dict


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else "usd_events.db"
    conn = sqlite3.connect(db_path)
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
    conn.close()


if __name__ == "__main__":
    main()
