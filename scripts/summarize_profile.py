"""Summarize a py-spy raw profile into a text hotspot report.

Reads py-spy's collapsed-stacks output (one line per stack:
``frame1;frame2;... count``) and produces a sortable text summary suitable
for human review or for feeding to an agent that needs to identify
hotspots.

Usage:
    py-spy record --format raw --pid <PID> --duration 30 --output profile.raw
    uv run python scripts/summarize_profile.py profile.raw --output profile.txt

For automated profiling integrated with the stress test, see the
``--text-profile`` flag on ``scripts/stress_test_departments.py``.
"""

from __future__ import annotations

import argparse
import collections
import re
from pathlib import Path

# py-spy frame format: "function_name (file:line)" or "function_name (file)".
_FRAME_RE = re.compile(r"^(?P<name>.+?)\s+\((?P<loc>[^)]+)\)$")


def parse_collapsed(path: Path) -> list[tuple[list[str], int]]:
    """Return a list of (stack, sample_count) tuples from a py-spy raw file."""
    stacks: list[tuple[list[str], int]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Each line ends with a single integer sample count separated by space.
        head, _, tail = line.rpartition(" ")
        try:
            count = int(tail)
        except ValueError:
            continue
        if not head:
            continue
        frames = head.split(";")
        stacks.append((frames, count))
    return stacks


def _frame_file(frame: str) -> str | None:
    """Extract the source file path from a py-spy frame string."""
    m = _FRAME_RE.match(frame)
    if not m:
        return None
    loc = m.group("loc")
    # Trailing ":line" suffix (py-spy includes it for Python frames).
    if ":" in loc:
        return loc.rsplit(":", 1)[0]
    return loc


def aggregate(stacks: list[tuple[list[str], int]]):
    """Compute inclusive / self / per-file sample counts.

    - inclusive: each frame in the stack contributes its sample count
      (deduplicated against recursion within a single stack).
    - self: only the top of each stack contributes — pure CPU work in
      that function as opposed to its callees.
    - by_file: per-file inclusive counts (each file deduplicated per stack).
    """
    total = 0
    inclusive: collections.Counter[str] = collections.Counter()
    self_time: collections.Counter[str] = collections.Counter()
    by_file: collections.Counter[str] = collections.Counter()
    for frames, count in stacks:
        total += count
        if frames:
            self_time[frames[-1]] += count
        seen_frames: set[str] = set()
        for f in frames:
            if f in seen_frames:
                continue
            seen_frames.add(f)
            inclusive[f] += count
        seen_files: set[str] = set()
        for f in frames:
            file = _frame_file(f)
            if file and file not in seen_files:
                seen_files.add(file)
                by_file[file] += count
    return total, inclusive, self_time, by_file


def _filter_project(items, project: str | None):
    if not project:
        return items
    out = []
    for frame, count in items:
        file = _frame_file(frame) or ""
        if project in file:
            out.append((frame, count))
    return out


def render_report(
    total: int,
    inclusive: collections.Counter[str],
    self_time: collections.Counter[str],
    by_file: collections.Counter[str],
    *,
    top_n: int = 30,
    project: str | None = None,
    label: str | None = None,
) -> str:
    out: list[str] = []
    if label:
        out.append(f"=== {label} ===")
    out.append(f"Total samples: {total:,}")
    if project:
        out.append(f"Project filter: '{project}' (first-party code only)")
    out.append("")

    out.append(f"--- Top {top_n} functions by INCLUSIVE time ---")
    out.append("(percent of samples whose stack contains this function)")
    out.append("")
    items = inclusive.most_common(top_n * 4)
    items = _filter_project(items, project)
    for frame, count in items[:top_n]:
        pct = 100.0 * count / total if total else 0.0
        out.append(f"  {pct:5.1f}%  {frame}")
    out.append("")

    out.append(f"--- Top {top_n} functions by SELF time ---")
    out.append("(percent of samples where this function is on top of stack)")
    out.append("")
    items = self_time.most_common(top_n * 4)
    items = _filter_project(items, project)
    for frame, count in items[:top_n]:
        pct = 100.0 * count / total if total else 0.0
        out.append(f"  {pct:5.1f}%  {frame}")
    out.append("")

    out.append(f"--- Top {top_n} files by inclusive time ---")
    out.append("")
    file_items = by_file.most_common(top_n * 4)
    if project:
        file_items = [(f, c) for f, c in file_items if project in f]
    for file, count in file_items[:top_n]:
        pct = 100.0 * count / total if total else 0.0
        out.append(f"  {pct:5.1f}%  {file}")
    out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(
        description="Summarize a py-spy raw profile into a text hotspot report.",
    )
    ap.add_argument("input", type=Path, help="py-spy --format raw output file")
    ap.add_argument(
        "--output", type=Path,
        help="Write report to file (default: stdout)",
    )
    ap.add_argument(
        "--top", type=int, default=30,
        help="Top N entries per section (default 30)",
    )
    ap.add_argument(
        "--project", default=None,
        help="Filter functions by file-path substring (e.g. 'openusdconnect') "
             "to focus on first-party code; if omitted, includes everything.",
    )
    ap.add_argument(
        "--label", default=None,
        help="Optional title printed at the top of the report",
    )
    args = ap.parse_args()

    if not args.input.exists():
        raise SystemExit(f"input file not found: {args.input}")

    stacks = parse_collapsed(args.input)
    total, inclusive, self_time, by_file = aggregate(stacks)
    report = render_report(
        total, inclusive, self_time, by_file,
        top_n=args.top, project=args.project, label=args.label,
    )

    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"Report written to {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
