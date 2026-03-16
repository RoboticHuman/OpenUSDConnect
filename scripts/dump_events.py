"""Dump the server event log from SQLite."""

import json
import sqlite3
import sys


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else "usd_events.db"
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT seq, event FROM events ORDER BY seq").fetchall()
    print(f"Events: {len(rows)}")
    for seq, event_json in rows:
        rec = json.loads(event_json)
        ev = rec.get("event", rec)
        k = ev.get("k", "?")
        prim = ev.get("prim", "?")
        extras = {key: val for key, val in ev.items() if key not in ("k", "prim")}
        extra_str = f"  {extras}" if extras else ""
        print(f"  seq={seq}: {k} {prim}{extra_str}")
    conn.close()


if __name__ == "__main__":
    main()
