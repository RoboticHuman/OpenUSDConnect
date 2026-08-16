"""Run every bounded TLA+ scenario with a pinned TLC release."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TLA_DIR = ROOT / "verification" / "tla"
TLC_VERSION = "1.8.0"
TLC_URL = (
    "https://github.com/tlaplus/tlaplus/releases/download/"
    f"v{TLC_VERSION}/tla2tools.jar"
)
TLC_SHA256 = "ab323b79802aedc3203b3f9af37c6aca3ed43f4e0225b36f2aa77b26de46c05f"
DEFAULT_JAR = (
    Path.home() / ".cache" / "openusdconnect" / f"tla2tools-{TLC_VERSION}.jar"
)

SCENARIOS = (
    ("TransactionRecoveryFirst.cfg", "TransactionRecovery.tla", "recovery: reject 1"),
    ("TransactionRecovery.cfg", "TransactionRecovery.tla", "recovery: reject 3"),
    ("RecoverySessionRollover.cfg", "RecoverySessionRollover.tla", "session rollover"),
    ("ReceiverSynchronization.cfg", "ReceiverSynchronization.tla", "receiver: queue 3"),
    (
        "ReceiverSynchronizationTight.cfg",
        "ReceiverSynchronization.tla",
        "receiver: queue 1",
    ),
    ("TransactionCoordinator.cfg", "TransactionCoordinator.tla", "coordinator: valid"),
    (
        "TransactionCoordinatorInvalid.cfg",
        "TransactionCoordinator.tla",
        "coordinator: invalid",
    ),
    ("SharedLayerGraphRace.cfg", "SharedLayerGraphRace.tla", "shared graph race"),
    (
        "SharedLayerRestartRecovery.cfg",
        "SharedLayerRestartRecovery.tla",
        "shared graph restart",
    ),
    ("TwoClientConvergence.cfg", "TwoClientConvergence.tla", "two-client convergence"),
)

# These are the adversarial or split-boundary actions most likely to become
# accidentally unreachable while the models are edited.
REQUIRED_ACTIONS = {
    "RecoverySessionRollover.tla": {
        "ConcurrentAuthoritativeCommit",
        "RefreshCheckpoint",
        "UseServerAndStartNewSession",
    },
    "ReceiverSynchronization.tla": {
        "ApplyEventFailure",
        "ApplyCompleteSuccess",
        "InjectStaleComplete",
        "DiscardStaleFrame",
    },
    "TransactionCoordinator.tla": {
        "GroupApply",
        "GroupPersist",
        "GroupFailure",
        "GroupRollback",
        "FallbackInvalid",
        "FallbackUnexpected",
    },
    "SharedLayerRestartRecovery.tla": {"Crash", "Restart"},
    "TwoClientConvergence.tla": {"HandleRemoteNotice"},
}

SUMMARY_RE = re.compile(
    r"(?P<generated>[\d,]+) states generated, "
    r"(?P<distinct>[\d,]+) distinct states found"
)
DEPTH_RE = re.compile(r"depth of the complete state graph search is (?P<depth>\d+)")
COVERAGE_RE = re.compile(
    r"^<(?P<action>[A-Za-z][A-Za-z0-9_]*)"
    r"(?:\([^>]*\))? line .*?>: (?P<distinct>\d+):(?P<total>\d+)$",
    re.MULTILINE,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_jar(requested: Path | None, download: bool) -> Path:
    env_jar = os.environ.get("TLA2TOOLS_JAR")
    jar = requested or (Path(env_jar) if env_jar else DEFAULT_JAR)
    jar = jar.expanduser().resolve()

    if not jar.exists():
        if not download:
            raise RuntimeError(
                f"TLC JAR not found at {jar}. Pass --download, --jar, or set "
                "TLA2TOOLS_JAR."
            )
        jar.parent.mkdir(parents=True, exist_ok=True)
        temporary = jar.with_suffix(".download")
        print(f"Downloading official tla2tools.jar v{TLC_VERSION}...")
        urllib.request.urlretrieve(TLC_URL, temporary)
        temporary.replace(jar)

    actual_hash = sha256(jar)
    if actual_hash != TLC_SHA256:
        raise RuntimeError(
            f"Unexpected SHA-256 for {jar}: {actual_hash}; expected {TLC_SHA256}"
        )
    return jar


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, help="Path to tla2tools.jar")
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download the pinned official JAR when it is missing",
    )
    parser.add_argument("--workers", default="auto", help="TLC worker count")
    parser.add_argument(
        "--skip-coverage-check",
        action="store_true",
        help="Do not fail when a required adversarial action has zero coverage",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    java = shutil.which("java")
    if java is None:
        print("Java 11 or newer is required.", file=sys.stderr)
        return 2

    try:
        jar = resolve_jar(args.jar, args.download)
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 2

    action_totals: dict[str, dict[str, int]] = {}
    rows: list[tuple[str, str, str, str]] = []
    failed = False

    for cfg_name, model_name, label in SCENARIOS:
        print(f"Checking {label}...")
        with tempfile.TemporaryDirectory(prefix="openusdconnect-tlc-") as metadir:
            command = [
                java,
                "-XX:+UseParallelGC",
                "-cp",
                str(jar),
                "tlc2.TLC",
                "-deadlock",
                "-workers",
                str(args.workers),
                "-coverage",
                "1",
                "-metadir",
                metadir,
                "-config",
                str(TLA_DIR / cfg_name),
                str(TLA_DIR / model_name),
            ]
            result = subprocess.run(
                command,
                cwd=ROOT,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        output = result.stdout
        if result.returncode != 0 or "No error has been found" not in output:
            print(output, file=sys.stderr)
            failed = True
            continue

        summaries = list(SUMMARY_RE.finditer(output))
        depth = DEPTH_RE.search(output)
        if not summaries or depth is None:
            print(f"Could not parse TLC summary for {label}.\n{output}", file=sys.stderr)
            failed = True
            continue
        summary = summaries[-1]
        rows.append(
            (
                label,
                summary.group("generated"),
                summary.group("distinct"),
                depth.group("depth"),
            )
        )

        model_actions = action_totals.setdefault(model_name, {})
        for match in COVERAGE_RE.finditer(output):
            action = match.group("action")
            model_actions[action] = model_actions.get(action, 0) + int(
                match.group("total")
            )

    if rows:
        print("\nScenario | Generated | Distinct | Depth")
        print("--- | ---: | ---: | ---:")
        for label, generated, distinct, depth in rows:
            print(f"{label} | {generated} | {distinct} | {depth}")

    if not args.skip_coverage_check:
        uncovered = []
        for model, required in REQUIRED_ACTIONS.items():
            totals = action_totals.get(model, {})
            uncovered.extend(
                f"{model}:{action}"
                for action in sorted(required)
                if totals.get(action, 0) == 0
            )
        if uncovered:
            print(
                "Required actions had zero TLC coverage:\n  " + "\n  ".join(uncovered),
                file=sys.stderr,
            )
            failed = True

    if failed:
        return 1
    print("\nAll TLA+ scenarios and required adversarial actions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
