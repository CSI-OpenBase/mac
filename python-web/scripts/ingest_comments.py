#!/usr/bin/env python3
"""Validate and merge comment batches into the active workspace archive."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from admin_app.archive_lock import archive_lock
from scripts.comment_data import (
    CommentDataError,
    load_records,
    merge_record_sets,
    utc_now,
    write_jsonl_atomic,
)


DEFAULT_STORE: Path | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate one or more JSONL batches, merge them into the canonical "
            "comment store, and keep the newest snapshot of each comment ID."
        )
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="JSONL batch file(s)")
    parser.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_STORE,
        help="canonical JSONL path (default: active workspace)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report merge counts without writing the store",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.store is None:
        try:
            from admin_app.workspace import load_active_workspace

            args.store = load_active_workspace().comments_dir / "comments.jsonl"
        except Exception as exc:
            print(f"cannot resolve active workspace: {exc}", file=sys.stderr)
            return 2
    collection_time = utc_now()

    try:
        with archive_lock(args.store.resolve().parent / ".archive.lock"):
            batches_dir = (args.store.resolve().parent / "batches").resolve()
            incoming: list[dict[str, object]] = []
            normalized_batches: list[tuple[Path, list[dict[str, object]]]] = []
            for input_path in args.inputs:
                records = load_records(
                    [input_path], default_collected_at=collection_time
                )
                resolved_input = input_path.resolve()
                if resolved_input.parent == batches_dir:
                    for record in records:
                        if not record["collection_batch"]:
                            record["collection_batch"] = resolved_input.stem
                    normalized_batches.append((resolved_input, records))
                incoming.extend(records)
            existing = (
                load_records([args.store], default_collected_at=collection_time)
                if args.store.exists()
                else []
            )
            merged, stats = merge_record_sets(existing, incoming)

            if not args.dry_run:
                for batch_path, records in normalized_batches:
                    write_jsonl_atomic(batch_path, records)
                write_jsonl_atomic(args.store, merged)
    except CommentDataError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    action = "checked" if args.dry_run else "written"
    print(f"Store {action}: {args.store}")
    print(f"Input records: {stats['input_records']}")
    print(f"New comments: {stats['new_records']}")
    print(f"Updated comments: {stats['updated_records']}")
    print(f"Unchanged comments: {stats['unchanged_records']}")
    print(f"Duplicate snapshots in input: {stats['duplicate_input_snapshots']}")
    print(f"Canonical comments: {stats['stored_records']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
