"""Manage and run the local artifact retention scheduler."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_review_agent.artifact_retention import (  # noqa: E402
    ArtifactRetentionError,
    ArtifactRetentionLedger,
)


def _add_ledger_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register", help="register an artifact for retention")
    _add_ledger_arguments(register)
    register.add_argument("--artifact-id", required=True)
    register.add_argument("--relative-path", required=True)
    register.add_argument("--retention-deadline", type=float, required=True)

    hold = commands.add_parser("hold", help="place or replace a legal hold")
    _add_ledger_arguments(hold)
    hold.add_argument("--artifact-id", required=True)
    hold.add_argument("--reason", required=True)
    hold.add_argument("--now", type=float)

    release = commands.add_parser("release-hold", help="release an active legal hold")
    _add_ledger_arguments(release)
    release.add_argument("--artifact-id", required=True)

    run = commands.add_parser("run", help="execute one scheduled retention pass")
    _add_ledger_arguments(run)
    run.add_argument("--interval-seconds", type=float, required=True)
    run.add_argument("--now", type=float)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--limit", type=int, default=64)

    receipts = commands.add_parser("receipts", help="list hash-only deletion receipts")
    _add_ledger_arguments(receipts)
    return parser


def _result_for(args: argparse.Namespace, ledger: ArtifactRetentionLedger) -> dict[str, Any]:
    if args.command == "register":
        return ledger.register_artifact(
            args.artifact_id,
            args.relative_path,
            retention_deadline=args.retention_deadline,
        )
    if args.command == "hold":
        return ledger.place_legal_hold(args.artifact_id, args.reason, now=args.now)
    if args.command == "release-hold":
        return {
            "released": ledger.release_legal_hold(args.artifact_id),
        }
    if args.command == "run":
        return ledger.run_scheduled(
            interval_seconds=args.interval_seconds,
            now=args.now,
            dry_run=args.dry_run,
            limit=args.limit,
        ).as_dict()
    if args.command == "receipts":
        return {
            "schema_version": "crag.artifact-retention/v1",
            "receipts": [receipt.as_dict() for receipt in ledger.list_receipts()],
        }
    raise AssertionError("unknown retention command")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        ledger = ArtifactRetentionLedger(args.artifact_root, args.ledger)
        result = _result_for(args, ledger)
    except ArtifactRetentionError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
